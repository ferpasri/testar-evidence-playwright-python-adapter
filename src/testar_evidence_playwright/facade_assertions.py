"""Delegate synchronous Playwright assertions and record their final outcomes."""

from __future__ import annotations

from datetime import timedelta
from functools import wraps
import inspect
import math
import re
from typing import Any, Callable

from playwright.sync_api import Locator, Page, expect as native_expect
from testar_evidence import CheckReading, CheckRule, UNSPECIFIED


class EvidenceExpect:
    """Use Playwright's assertion behavior, including retries and global options."""

    def __call__(self, actual, message: str | None = None):
        assertions = native_expect(actual, message)
        runtime = getattr(actual, "_evidence_runtime", None)
        if runtime is None or runtime.is_suspended or not isinstance(actual, (Locator, Page)):
            return assertions
        return _Assertions(assertions, actual, runtime, message)

    def soft(self, actual, message: str | None = None):
        # Soft failures belong to the pytest plugin's collector and are not raised here.
        return native_expect.soft(actual, message)

    def __getattr__(self, name):
        return getattr(native_expect, name)


expect = EvidenceExpect()


class _Assertions:
    def __init__(self, assertions, subject, runtime, message):
        self._assertions = assertions
        self._subject = subject
        self._runtime = runtime
        self._message = message

    def __getattr__(self, name):
        member = getattr(self._assertions, name)
        if not callable(member) or not name.startswith(("to_", "not_to_")):
            return member

        @wraps(member)
        def assertion(*arguments, **options):
            if self._runtime.is_suspended:
                return member(*arguments, **options)
            recorder = self._runtime.recorder_for(self._subject)
            if recorder is None:
                return member(*arguments, **options)
            # Binding errors belong to the native API and must not become test results.
            supplied = inspect.signature(member).bind(*arguments, **options).arguments
            with self._runtime.suspended():
                metadata = _AssertionDetails(self._subject, name, supplied)
                result = None

                def operation():
                    nonlocal result
                    result = member(*arguments, **options)

                recorder.checks.evaluate(self._message or "Playwright " + name,
                    rule=metadata.rule, locator=metadata.locator,
                    property=metadata.property, expected=metadata.expected,
                    assertion=operation, reading=metadata.reading, redact_error=metadata.redacted)
                return result

        return assertion

    def __dir__(self):
        return sorted(set(dir(type(self))) | set(dir(self._assertions)))


class _AssertionDetails:
    def __init__(self, subject, method: str, arguments: dict[str, Any]):
        self.subject = subject
        self.method = method.removeprefix("not_")
        self.arguments = arguments
        self.locator = subject if isinstance(subject, Locator) else None
        self.property = None
        self.expected = UNSPECIFIED
        self.redacted = False
        self.reading: Callable[[], CheckReading] = CheckReading.unknown
        parameters = {"comparison": method, "negated": method.startswith("not_")}
        parameters["arguments"] = _json_value(arguments)
        self._select_reading(parameters)
        self._redact_password_value(parameters)
        self.rule = CheckRule("playwright:" + method, "1", parameters)

    def _redact_password_value(self, parameters):
        if not isinstance(self.subject, Locator):
            return
        value_check = self.method in {"to_have_value", "to_have_values"} or \
            self.method in {"to_have_attribute", "to_have_js_property"} and self.arguments.get("name") == "value"
        if not value_check:
            return
        try:
            secret = self.subject.count() != 1 or self.subject.evaluate(
                "element => element instanceof HTMLInputElement && element.type === 'password'", timeout=100)
        except Exception:
            secret = True
        if secret:
            self.expected = UNSPECIFIED
            self.reading = CheckReading.unknown
            self.redacted = True
            parameters["arguments"] = {"redacted": True}
            parameters["redaction"] = "Password or unresolved value target; expected and actual values are omitted."

    def _select_reading(self, parameters):
        if isinstance(self.subject, Page):
            self._page_reading(parameters)
            return
        # Collection assertions describe the collection, rather than one arbitrary widget.
        if self.method == "to_have_count":
            self.locator = None
            self.expected = _json_value(self.arguments["count"])
            self.reading = lambda: CheckReading.value(self.subject.count())
            parameters["target_scope"] = "locator_collection"
            return
        if self.method in {"to_have_text", "to_contain_text", "to_have_class", "to_contain_class"}:
            expected = self.arguments.get("expected", UNSPECIFIED)
            if isinstance(expected, (list, tuple)):
                self.locator = None
                self.expected = _json_value(expected)
                parameters["target_scope"] = "locator_collection"
                if self.method in {"to_have_text", "to_contain_text"}:
                    method = self.subject.all_inner_texts if self.arguments.get("use_inner_text") \
                        else self.subject.all_text_contents
                    self.reading = lambda: CheckReading.value(method())
                else:
                    self.reading = lambda: CheckReading.value(self.subject.evaluate_all(
                        "elements => elements.map(element => element.getAttribute('class'))"))
                return
        self._locator_reading(parameters)

    def _page_reading(self, parameters):
        if self.method == "to_have_title":
            self.property = "ui.title"
            self.expected = _json_value(self.arguments["title_or_reg_exp"])
            self.reading = lambda: CheckReading.value(self.subject.title())
        elif self.method == "to_have_url":
            self.property = "ui.url"
            self.expected = _json_value(self.arguments["url_or_reg_exp"])
            self.reading = lambda: CheckReading.value(self.subject.url)
        else:
            self._unmapped(parameters)

    def _locator_reading(self, parameters):
        method = self.method
        arguments = self.arguments
        if method in {"to_have_text", "to_contain_text"}:
            self.expected = _json_value(arguments["expected"])
            inner = arguments.get("use_inner_text", False)
            self.property = "playwright.dom.innerText" if inner else "playwright.dom.textContent"
            self.reading = lambda: self._element_reading(
                lambda: self.subject.inner_text(timeout=100) if inner else self.subject.text_content(timeout=100))
        elif method == "to_have_value":
            self.expected = _json_value(arguments["value"])
            self.property = "playwright.dom.value"
            self.reading = lambda: self._element_reading(lambda: self.subject.input_value(timeout=100))
        elif method in {"to_have_attribute", "to_have_id", "to_have_class", "to_contain_class"}:
            self._attribute_reading()
        elif method == "to_have_css":
            name = arguments["name"]
            pseudo = arguments.get("pseudo")
            self.expected = _json_value(arguments["value"])
            self.property = "playwright.style." + ("::" + pseudo + "." if pseudo else "") + name
            self.reading = lambda: self._element_reading(lambda: self.subject.evaluate(
                "(element, options) => getComputedStyle(element, options.pseudo).getPropertyValue(options.name)",
                {"name": name, "pseudo": "::" + pseudo if pseudo else None}, timeout=100))
        elif method == "to_have_js_property":
            self.expected = _json_value(arguments["value"])
            self.property = "playwright.dom." + arguments["name"]
            self.reading = lambda: self._element_reading(lambda: self.subject.evaluate(
                "(element, name) => element[name]", arguments["name"], timeout=100))
        elif method in {"to_be_checked", "to_be_enabled", "to_be_disabled", "to_be_editable",
                         "to_be_visible", "to_be_hidden", "to_be_attached", "to_be_focused"}:
            self._boolean_reading()
        else:
            self._unmapped(parameters)

    def _attribute_reading(self):
        if self.method == "to_have_attribute":
            name = self.arguments["name"]
            value = self.arguments.get("value", UNSPECIFIED)
            # A missing value means Playwright checks presence, rather than equality with null.
            if value is not UNSPECIFIED and value is not None:
                self.expected = _json_value(value)
        elif self.method == "to_have_id":
            name = "id"
            self.expected = _json_value(self.arguments["id"])
        else:
            name = "class"
            self.expected = _json_value(self.arguments["expected"])
        self.property = "playwright.attribute." + name

        def reading():
            if self.subject.count() != 1:
                return CheckReading.unknown()
            value = self.subject.get_attribute(name, timeout=100)
            return CheckReading.absent() if value is None else CheckReading.value(value)

        self.reading = reading

    def _boolean_reading(self):
        name = self.method.removeprefix("to_be_")
        self.expected = self.arguments.get(name, True)
        if self.expected is None:
            self.expected = True
        if name == "checked":
            indeterminate = self.arguments.get("indeterminate")
            self.property = "playwright.dom.indeterminate" if indeterminate else "playwright.dom.checked"
            self.reading = lambda: self._element_reading(lambda: self.subject.evaluate(
                "element => element.indeterminate", timeout=100) if indeterminate
                else self.subject.is_checked(timeout=100))
        elif name in {"enabled", "disabled", "editable", "visible", "hidden"}:
            self.property = "playwright." + name
            self.reading = lambda: self._element_reading(lambda: getattr(self.subject, "is_" + name)(timeout=100))
        elif name == "attached":
            self.property = "playwright.dom.isConnected"
            self.reading = lambda: CheckReading.value(False) if self.subject.count() == 0 \
                else self._element_reading(lambda: self.subject.evaluate("element => element.isConnected", timeout=100))
        else:
            self.property = "playwright.focused"
            self.reading = lambda: self._element_reading(lambda: self.subject.evaluate(
                "element => element === element.ownerDocument.activeElement", timeout=100))

    def _element_reading(self, supplier):
        if self.subject.count() != 1:
            return CheckReading.unknown()
        return CheckReading.value(supplier())

    def _unmapped(self, parameters):
        parameters["actual_reading"] = "The assertion outcome is recorded; no property reading is mapped."
        for name in ("expected", "values", "name", "description", "error_message", "role"):
            if name in self.arguments:
                self.expected = _json_value(self.arguments[name])
                break


def _json_value(value, depth=0):
    """Describe assertion inputs without invoking arbitrary object representations."""
    if depth > 32:
        return {"kind": "unsupported", "type": "nested_value"}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"kind": "nonfinite_number"}
    if isinstance(value, re.Pattern):
        if not isinstance(value.pattern, str):
            return {"kind": "unsupported", "type": "bytes_pattern"}
        return {"kind": "regular_expression", "pattern": value.pattern, "flags": value.flags}
    if isinstance(value, timedelta):
        return {"kind": "duration", "milliseconds": value.total_seconds() * 1000}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {name: _json_value(item, depth + 1) for name, item in value.items() if isinstance(name, str)}
    return {"kind": "unsupported", "type": type(value).__name__}

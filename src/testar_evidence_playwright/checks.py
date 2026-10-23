"""Report the final outcome of Playwright assertions and custom test oracles."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Callable

from playwright.sync_api import Locator, expect
from testar_evidence import CheckReading, CheckResult, CheckRule, CheckTarget, EvidenceEvent, UNSPECIFIED

if TYPE_CHECKING:
    from .evidence import PlaywrightEvidence


class PlaywrightChecks:
    """Run each assertion once, preserving that assertion's own retry behavior."""

    def __init__(self, evidence: PlaywrightEvidence):
        self._evidence = evidence
        self._timeout_millis = 5_000.0
        self._recording_error = None

    @property
    def recording_error(self) -> Exception | None:
        """A recording failure from the latest evaluation, including one attached to an oracle failure."""
        return self._recording_error

    @property
    def timeout_millis(self) -> float:
        return self._timeout_millis

    @timeout_millis.setter
    def timeout_millis(self, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Assertion timeout must be positive and finite")
        self._timeout_millis = float(value)

    def has_text(self, rule_id: str, name: str, locator: Locator, expected: str) -> None:
        self._require_string(expected, "Expected text")
        self.evaluate(name, rule=self._rule(rule_id, "playwright.hasText"), locator=locator,
            property="playwright.dom.textContent", expected=expected,
            assertion=lambda: expect(locator).to_have_text(expected, timeout=self.timeout_millis),
            reading=lambda: CheckReading.value(locator.text_content(timeout=self.timeout_millis)))

    def record_result(self, rule_id: str, name: str, status: str, error: str | None = None, *,
                      details: dict[str, Any] | None = None) -> None:
        """Record an already executed check against the latest captured observation."""
        self._recording_error = None
        self._evidence._require_active()
        details = {} if details is None else dict(details)
        if error is not None:
            details["playwright.assertion_error"] = error
        rule = CheckRule(rule_id)
        EvidenceEvent("validation:event", "checked", CheckResult(self._evidence.run_id, name, rule,
            CheckTarget.run(), status, actual=CheckReading.unknown(), details=details)).to_dict()
        self._evidence._busy = True
        try:
            self._evidence._record_check(name, rule, None, None, UNSPECIFIED, status,
                CheckReading.unknown(), details, reuse_capture=True)
        except Exception as recording:
            self._recording_error = recording
            raise
        finally:
            self._evidence._busy = False

    def has_value(self, rule_id: str, name: str, locator: Locator, expected: str) -> None:
        self._require_string(expected, "Expected value")
        self.evaluate(name, rule=self._rule(rule_id, "playwright.hasValue"), locator=locator,
            property="playwright.dom.value", expected=expected,
            assertion=lambda: expect(locator).to_have_value(expected, timeout=self.timeout_millis),
            reading=lambda: CheckReading.value(locator.input_value(timeout=self.timeout_millis)))

    def has_attribute(self, rule_id: str, name: str, locator: Locator, attribute: str, expected: str) -> None:
        self._require_string(attribute, "Attribute name")
        self._require_string(expected, "Expected attribute value")
        if not attribute.strip():
            raise ValueError("Attribute name must be nonblank")
        rule = self._rule(rule_id, "playwright.hasAttribute", {"attribute": attribute})

        def reading() -> CheckReading:
            value = locator.get_attribute(attribute, timeout=self.timeout_millis)
            return CheckReading.absent() if value is None else CheckReading.value(value)

        self.evaluate(name, rule=rule, locator=locator, property="playwright.attribute." + attribute,
            expected=expected,
            assertion=lambda: expect(locator).to_have_attribute(attribute, expected, timeout=self.timeout_millis),
            reading=reading)

    def evaluate(self, name: str, *, rule: CheckRule, assertion: Callable[[], Any],
                 reading: Callable[[], CheckReading], locator: Locator | None = None,
                 property: str | None = None, expected: Any = UNSPECIFIED,
                 redact_error: bool = False) -> None:
        """Record one oracle result and rethrow its original failure after recording."""
        self._recording_error = None
        self._evidence._require_active()
        if type(redact_error) is not bool:
            raise TypeError("redact_error must be a boolean")
        if not callable(assertion) or not callable(reading):
            raise TypeError("Assertion and reading must be callable")
        if property is not None:
            self._require_string(property, "Property name")
            if not property.strip():
                raise ValueError("Property name must be nonblank")
        # Reject invalid reporting data before executing the supplied test oracle.
        EvidenceEvent("validation:event", "checked", CheckResult(self._evidence.run_id, name, rule,
            CheckTarget.run(), "passed", expected=expected)).to_dict()
        failure = None
        status = "passed"
        details = {}
        self._evidence._busy = True
        try:
            with self._evidence._recording_scope():
                try:
                    assertion()
                except AssertionError as error:
                    status = "failed"
                    failure = error
                except Exception as error:
                    status = "error"
                    failure = error
                if failure is not None:
                    details["playwright.assertion_error"] = self._error_text(failure, redact_error)
                try:
                    actual = reading()
                    if not isinstance(actual, CheckReading):
                        raise TypeError("A reading must return CheckReading")
                    actual.to_dict()
                except Exception as error:
                    actual = CheckReading.unknown()
                    details["playwright.reading_error"] = self._error_text(error, redact_error)
                try:
                    self._evidence._record_check(name, rule, locator, property, expected, status, actual, details)
                except Exception as recording:
                    self._recording_error = recording
                    if failure is None:
                        raise
                    failure.add_note("Evidence recording also failed: " + str(recording))
                if failure is not None:
                    raise failure
        finally:
            self._evidence._busy = False

    @staticmethod
    def _error_text(error: Exception, redact: bool) -> str:
        if redact:
            return type(error).__name__ + ": details omitted because the assertion concerns a secret value"
        return str(error)

    def _rule(self, identity: str, comparison: str, parameters: dict | None = None) -> CheckRule:
        return CheckRule(identity, "1", {"comparison": comparison,
            "timeout_millis": self.timeout_millis, **(parameters or {})})

    @staticmethod
    def _require_string(value: Any, name: str) -> None:
        if not isinstance(value, str):
            raise TypeError(name + " must be a string")

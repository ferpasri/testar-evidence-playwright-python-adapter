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
                 property: str | None = None, expected: Any = UNSPECIFIED) -> None:
        """Record one oracle result and rethrow its original failure after recording."""
        self._evidence._require_active()
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
            try:
                assertion()
            except AssertionError as error:
                status = "failed"
                failure = error
            except Exception as error:
                status = "error"
                failure = error
            if failure is not None:
                details["playwright.assertion_error"] = str(failure)
            try:
                actual = reading()
                if not isinstance(actual, CheckReading):
                    raise TypeError("A reading must return CheckReading")
                actual.to_dict()
            except Exception as error:
                actual = CheckReading.unknown()
                details["playwright.reading_error"] = str(error)
            try:
                self._evidence._record_check(name, rule, locator, property, expected, status, actual, details)
            except Exception as recording:
                if failure is None:
                    raise
                failure.add_note("Evidence recording also failed: " + str(recording))
            if failure is not None:
                raise failure
        finally:
            self._evidence._busy = False

    def _rule(self, identity: str, comparison: str, parameters: dict | None = None) -> CheckRule:
        return CheckRule(identity, "1", {"comparison": comparison,
            "timeout_millis": self.timeout_millis, **(parameters or {})})

    @staticmethod
    def _require_string(value: Any, name: str) -> None:
        if not isinstance(value, str):
            raise TypeError(name + " must be a string")

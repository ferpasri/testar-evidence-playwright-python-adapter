"""Record native assertion outcomes and caller-supplied test-oracle results."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Callable

from playwright.sync_api import Locator, expect
from testar_evidence import EvidenceEvent, ObservedValue, Oracle, OracleResult, OracleSeverity, OracleTarget, UNSPECIFIED

if TYPE_CHECKING:
    from .evidence import PlaywrightEvidence


class PlaywrightChecks:
    """Run an assertion once while retaining its own retries and original exception."""

    def __init__(self, evidence: PlaywrightEvidence):
        self._evidence = evidence
        self._timeout_millis = 5_000.0
        self._recording_error = None

    @property
    def recording_error(self) -> Exception | None:
        """A recording failure from the latest oracle evaluation."""
        return self._recording_error

    @property
    def timeout_millis(self) -> float:
        return self._timeout_millis

    @timeout_millis.setter
    def timeout_millis(self, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Assertion timeout must be positive and finite")
        self._timeout_millis = float(value)

    def has_text(self, oracle_id: str, name: str, locator: Locator, expected_value: str) -> None:
        self._require_string(expected_value, "Expected text")
        self.evaluate(name, oracle=self._oracle(oracle_id, "playwright.hasText"), locator=locator,
            property="playwright.dom.textContent", expected_value=expected_value,
            assertion=lambda: expect(locator).to_have_text(expected_value, timeout=self.timeout_millis),
            observed_value=lambda: ObservedValue.value(locator.text_content(timeout=self.timeout_millis)))

    def has_value(self, oracle_id: str, name: str, locator: Locator, expected_value: str) -> None:
        self._require_string(expected_value, "Expected value")
        self.evaluate(name, oracle=self._oracle(oracle_id, "playwright.hasValue"), locator=locator,
            property="playwright.dom.value", expected_value=expected_value,
            assertion=lambda: expect(locator).to_have_value(expected_value, timeout=self.timeout_millis),
            observed_value=lambda: ObservedValue.value(locator.input_value(timeout=self.timeout_millis)))

    def has_attribute(self, oracle_id: str, name: str, locator: Locator, attribute: str, expected_value: str) -> None:
        self._require_string(attribute, "Attribute name")
        self._require_string(expected_value, "Expected attribute value")
        if not attribute.strip():
            raise ValueError("Attribute name must be nonblank")

        def observed_value() -> ObservedValue:
            value = locator.get_attribute(attribute, timeout=self.timeout_millis)
            return ObservedValue.absent() if value is None else ObservedValue.value(value)

        self.evaluate(name, oracle=self._oracle(oracle_id, "playwright.hasAttribute", {"attribute": attribute}),
            locator=locator, property="playwright.attribute." + attribute, expected_value=expected_value,
            assertion=lambda: expect(locator).to_have_attribute(attribute, expected_value, timeout=self.timeout_millis),
            observed_value=observed_value)

    def record_result(self, oracle: Oracle, name: str, oracle_status: str, error: str | None = None, *,
                      oracle_details: dict[str, Any] | None = None, locator: Locator | None = None,
                      property: str | None = None, expected_value: Any = UNSPECIFIED,
                      observed_value: ObservedValue = ObservedValue.unknown(),
                      oracle_severity: OracleSeverity | Any = UNSPECIFIED) -> None:
        """Report an already obtained result at the latest capture, without running the oracle again."""
        self._recording_error = None
        self._evidence._require_active()
        details = {} if oracle_details is None else dict(oracle_details)
        if error is not None:
            details["playwright.assertion_error"] = error
        self._validate_result(name, oracle, oracle_status, expected_value, details, oracle_severity, observed_value)
        self._evidence._busy = True
        try:
            self._evidence._record_oracle_result(name, oracle, locator, property, expected_value, oracle_status,
                observed_value, details, reuse_capture=True, oracle_severity=oracle_severity)
        except Exception as recording:
            self._recording_error = recording
            raise
        finally:
            self._evidence._busy = False

    def evaluate(self, name: str, *, oracle: Oracle, assertion: Callable[[], Any],
                 observed_value: Callable[[], ObservedValue], locator: Locator | None = None,
                 property: str | None = None, expected_value: Any = UNSPECIFIED,
                 oracle_severity: OracleSeverity | Any = UNSPECIFIED, redact_error: bool = False) -> None:
        """Record one final oracle result and rethrow the original assertion or execution failure."""
        self._recording_error = None
        self._evidence._require_active()
        if type(redact_error) is not bool:
            raise TypeError("redact_error must be a boolean")
        if not callable(assertion) or not callable(observed_value):
            raise TypeError("Assertion and observed-value supplier must be callable")
        if property is not None:
            self._require_string(property, "Property name")
            if not property.strip():
                raise ValueError("Property name must be nonblank")
        self._validate_result(name, oracle, "passed", expected_value, {}, oracle_severity)
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
                    actual = observed_value()
                    if not isinstance(actual, ObservedValue):
                        raise TypeError("An observed-value supplier must return ObservedValue")
                    actual.to_dict()
                except Exception as error:
                    actual = ObservedValue.unknown()
                    details["playwright.reading_error"] = self._error_text(error, redact_error)
                try:
                    self._evidence._record_oracle_result(name, oracle, locator, property, expected_value,
                        status, actual, details, oracle_severity=oracle_severity)
                except Exception as recording:
                    self._recording_error = recording
                    if failure is None:
                        raise
                    failure.add_note("Evidence recording also failed: " + str(recording))
                if failure is not None:
                    raise failure
        finally:
            self._evidence._busy = False

    def _validate_result(self, name, oracle, status, expected_value, details, severity, observed_value=UNSPECIFIED):
        # Invalid reporting data must be rejected before a supplied oracle can run.
        result = OracleResult(self._evidence.run_id, name, oracle, OracleTarget.test_run(), status,
            expected_value=expected_value, observed_value=observed_value, oracle_details=details, oracle_severity=severity)
        EvidenceEvent("validation:event", "oracle_result_recorded", result).to_dict()

    @staticmethod
    def _error_text(error: Exception, redact: bool) -> str:
        if redact:
            return type(error).__name__ + ": details omitted because the assertion concerns a secret value"
        return str(error)

    def _oracle(self, identity: str, comparison: str, parameters: dict | None = None) -> Oracle:
        return Oracle(identity, "1", {"comparison": comparison,
            "timeout_millis": self.timeout_millis, **(parameters or {})})

    @staticmethod
    def _require_string(value: Any, name: str) -> None:
        if not isinstance(value, str):
            raise TypeError(name + " must be a string")

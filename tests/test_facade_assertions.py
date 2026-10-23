"""Check assertion delegation, result recording, and objective target descriptions."""

from contextlib import contextmanager
import re
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Locator, Page
from testar_evidence import UNSPECIFIED

from testar_evidence_playwright.facade_assertions import expect


class RecordingChecks:
    def __init__(self):
        self.results = []

    def evaluate(self, name, **arguments):
        result = {"name": name, **arguments}
        try:
            arguments["assertion"]()
        except BaseException:
            result["status"] = "failed"
            result["actual"] = arguments["reading"]().to_dict()
            self.results.append(result)
            raise
        result["status"] = "passed"
        result["actual"] = arguments["reading"]().to_dict()
        self.results.append(result)


class Runtime:
    def __init__(self, checks):
        self.recorder = Mock(checks=checks)
        self.suspension_depth = 0

    def recorder_for(self, subject):
        return self.recorder

    @property
    def is_suspended(self):
        return self.suspension_depth > 0

    @contextmanager
    def suspended(self):
        self.suspension_depth += 1
        try:
            yield
        finally:
            self.suspension_depth -= 1


class FacadeAssertionsTests(unittest.TestCase):
    def setUp(self):
        self.checks = RecordingChecks()
        self.runtime = Runtime(self.checks)
        self.locator = Mock(spec=Locator)
        self.locator._evidence_runtime = self.runtime
        self.locator.count.return_value = 1
        self.locator.text_content.return_value = "Ready"
        self.native = Mock()
        self.native_call = patch("testar_evidence_playwright.facade_assertions.native_expect",
                                 return_value=self.native).start()
        self.addCleanup(patch.stopall)

    def test_native_assertion_receives_original_arguments_once_and_preserves_return(self):
        expected = re.compile("ready", re.IGNORECASE)
        marker = object()
        calls = []

        def assertion(expected, *, timeout=None, ignore_case=None, use_inner_text=None):
            self.assertEqual(1, self.runtime.suspension_depth)
            calls.append((expected, timeout, ignore_case))
            return marker

        self.native.to_have_text = assertion
        result = expect(self.locator, "Ready text").to_have_text(expected, timeout=120, ignore_case=True)

        self.assertIs(marker, result)
        self.assertEqual([(expected, 120, True)], calls)
        self.assertEqual(1, len(self.checks.results))
        recorded = self.checks.results[0]
        self.assertEqual("Ready text", recorded["name"])
        self.assertEqual("playwright.dom.textContent", recorded["property"])
        self.assertEqual({"kind": "value", "value": "Ready"}, recorded["actual"])
        self.assertEqual("regular_expression", recorded["expected"]["kind"])
        self.assertEqual(0, self.runtime.suspension_depth)

    def test_negated_failure_keeps_original_exception_and_records_once(self):
        original = AssertionError("Expected different text")

        def assertion(expected, *, timeout=None):
            raise original

        self.native.not_to_have_text = assertion
        with self.assertRaises(AssertionError) as raised:
            expect(self.locator).not_to_have_text("Ready", timeout=10)

        self.assertIs(original, raised.exception)
        self.assertEqual(1, len(self.checks.results))
        recorded = self.checks.results[0]
        self.assertEqual("failed", recorded["status"])
        self.assertTrue(recorded["rule"].parameters["negated"])
        self.assertEqual(0, self.runtime.suspension_depth)

    def test_collection_text_targets_observation_and_retains_list_expectation(self):
        self.native.to_have_text = lambda expected: None
        self.locator.all_text_contents.return_value = ["First", "Second"]
        expect(self.locator).to_have_text(["First", re.compile("Second")])

        recorded = self.checks.results[0]
        self.assertIsNone(recorded["locator"])
        self.assertIsNone(recorded["property"])
        self.assertEqual({"kind": "value", "value": ["First", "Second"]}, recorded["actual"])
        self.assertEqual("regular_expression", recorded["expected"][1]["kind"])

    def test_page_title_and_url_are_state_properties(self):
        page = Mock(spec=Page)
        page._evidence_runtime = self.runtime
        page.title.return_value = "Loan service"
        page.url = "https://example.test/loan"
        self.native.to_have_title = lambda title_or_reg_exp: None
        self.native.to_have_url = lambda url_or_reg_exp: None

        expect(page).to_have_title("Loan service")
        expect(page).to_have_url(re.compile("/loan$"))

        self.assertEqual(["ui.title", "ui.url"], [item["property"] for item in self.checks.results])
        self.assertEqual({"kind": "value", "value": page.url}, self.checks.results[-1]["actual"])

    def test_attribute_presence_retains_absence_and_omits_value_expectation(self):
        self.native.to_have_attribute = lambda name, value=None: None
        self.locator.get_attribute.return_value = None
        expect(self.locator).to_have_attribute("alt")

        recorded = self.checks.results[0]
        self.assertEqual("playwright.attribute.alt", recorded["property"])
        self.assertIs(UNSPECIFIED, recorded["expected"])
        self.assertEqual({"kind": "absent"}, recorded["actual"])

    def test_count_is_collection_evidence_without_arbitrary_widget_target(self):
        self.native.to_have_count = lambda count: None
        self.locator.count.return_value = 3
        expect(self.locator).to_have_count(3)

        recorded = self.checks.results[0]
        self.assertIsNone(recorded["locator"])
        self.assertIsNone(recorded["property"])
        self.assertEqual({"kind": "value", "value": 3}, recorded["actual"])

    def test_invalid_call_arguments_raise_before_any_result_is_recorded(self):
        self.native.to_have_text = lambda expected: None
        with self.assertRaises(TypeError):
            expect(self.locator).to_have_text("Ready", unsupported=True)
        self.assertEqual([], self.checks.results)

    def test_unmapped_assertion_keeps_outcome_without_inventing_property_reading(self):
        self.native.to_have_accessible_name = lambda name: None
        expect(self.locator).to_have_accessible_name("Continue")

        recorded = self.checks.results[0]
        self.assertIsNone(recorded["property"])
        self.assertEqual({"kind": "unknown"}, recorded["actual"])
        self.assertEqual("Continue", recorded["expected"])

    def test_plain_playwright_objects_and_soft_assertions_delegate_unchanged(self):
        plain_locator = Mock(spec=Locator)
        self.assertIs(self.native, expect(plain_locator))
        self.assertEqual([], self.checks.results)
        marker = object()
        self.native_call.soft.return_value = marker
        self.assertIs(marker, expect.soft(self.locator, "Soft condition"))
        self.native_call.soft.assert_called_once_with(self.locator, "Soft condition")
        self.assertEqual([], self.checks.results)

    def test_global_expect_options_are_forwarded_to_native_singleton(self):
        expect.set_options(timeout=250)
        self.native_call.set_options.assert_called_once_with(timeout=250)

    def test_password_value_assertion_omits_values_and_requests_error_redaction(self):
        self.native.to_have_value = lambda value: None
        self.locator.evaluate.return_value = True
        expect(self.locator).to_have_value("private-password")

        recorded = self.checks.results[0]
        self.assertIs(UNSPECIFIED, recorded["expected"])
        self.assertEqual({"kind": "unknown"}, recorded["actual"])
        self.assertTrue(recorded["redact_error"])
        self.assertNotIn("private-password", str(recorded["rule"].to_dict()))

    def test_explicit_recording_suspension_uses_native_expect_without_duplicate_results(self):
        with self.runtime.suspended():
            self.assertIs(self.native, expect(self.locator))
        self.assertEqual([], self.checks.results)


if __name__ == "__main__":
    unittest.main()

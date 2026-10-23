"""Verify recorder boundaries used by the automatic browser facade."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import ElementHandle
from testar_evidence import CheckReading, CheckRule, EvidenceReader, Widget
from testar_evidence_playwright import CaptureOptions, PlaywrightEvidence
from testar_evidence_playwright.capture import BrowserSnapshot
from testar_evidence_playwright.facade_actions import describe_action


class _Runtime:
    def __init__(self):
        self.depth = 0

    @property
    def is_suspended(self):
        return self.depth > 0

    @contextmanager
    def suspended(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1


class FacadeRecorderTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "trace"
        self.runtime = _Runtime()
        self.page = SimpleNamespace(url="about:blank", _evidence_runtime=self.runtime)

        def capture(*_):
            self.assertTrue(self.runtime.is_suspended)
            return BrowserSnapshot(1, {"ui.title": "Fixture"}, [Widget("root")], None)

        patcher = patch("testar_evidence_playwright.evidence.capture_page", side_effect=capture)
        self.capture = patcher.start()
        self.addCleanup(patcher.stop)

    def start(self):
        return PlaywrightEvidence.start(self.directory, self.page, CaptureOptions(screenshots=False))

    def test_explicit_action_does_not_trigger_automatic_nested_recording(self):
        calls = []

        def operation():
            self.assertTrue(self.runtime.is_suspended)
            calls.append("executed")
            return "native result"

        with self.start() as evidence:
            self.assertEqual("native result", evidence.perform("click", "Click", None, {}, operation))
            self.assertFalse(self.runtime.is_suspended)
        self.assertEqual(["executed"], calls)
        self.assertEqual(1, len(EvidenceReader.read(self.directory).executions))

    def test_explicit_assertion_and_reading_run_inside_one_suppressed_scope(self):
        def assertion():
            self.assertTrue(self.runtime.is_suspended)

        def reading():
            self.assertTrue(self.runtime.is_suspended)
            return CheckReading.value("Ready")

        with self.start() as evidence:
            evidence.checks.evaluate("Ready", rule=CheckRule("fixture:ready"),
                assertion=assertion, reading=reading)
            self.assertFalse(self.runtime.is_suspended)
        self.assertEqual("passed", EvidenceReader.read(self.directory).checks[0]["status"])

    def test_finishing_after_page_close_flushes_a_cached_result_without_capturing(self):
        with self.start() as evidence:
            evidence.perform("click", "Click", None, {}, lambda: None)
            evidence.capture()
            captures = self.capture.call_count
            with patch("testar_evidence_playwright.evidence.capture_page",
                    side_effect=AssertionError("The page has already closed")):
                evidence.finish("page_closed", capture_final=False)
            self.assertEqual(captures, self.capture.call_count)
        model = EvidenceReader.read(self.directory)
        self.assertEqual(1, len(model.links()))
        self.assertEqual("page_closed", next(iter(model.runs.values()))["reason"])

    def test_finishing_without_a_result_capture_preserves_an_incomplete_execution(self):
        with self.start() as evidence:
            evidence.perform("click", "Click", None, {}, lambda: None)
            captures = self.capture.call_count
            evidence.finish("page_closed", capture_final=False)
            self.assertEqual(captures, self.capture.call_count)
        model = EvidenceReader.read(self.directory)
        self.assertEqual("incomplete", next(iter(model.executions.values()))["status"])
        self.assertEqual([], model.links())

    def test_finishing_an_uncaptured_page_records_no_invented_observation(self):
        with self.start() as evidence:
            evidence.finish("page_closed", capture_final=False)
        self.assertEqual(0, self.capture.call_count)
        self.assertEqual({}, EvidenceReader.read(self.directory).observations)

    def test_redacted_assertion_errors_preserve_the_original_failure(self):
        failure = AssertionError("Expected secret-password")

        def assertion():
            raise failure

        def reading():
            raise RuntimeError("Could not read secret-password")

        with self.start() as evidence:
            with self.assertRaises(AssertionError) as caught:
                evidence.checks.evaluate("Password", rule=CheckRule("fixture:password"),
                    assertion=assertion, reading=reading, redact_error=True)
            self.assertIs(failure, caught.exception)
        text = (self.directory / "evidence.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret-password", text)
        check = EvidenceReader.read(self.directory).checks[0]
        self.assertEqual("failed", check["status"])
        self.assertIn("AssertionError", check["details"]["playwright.assertion_error"])
        self.assertIn("RuntimeError", check["details"]["playwright.reading_error"])
        self.assertFalse(self.runtime.is_suspended)

    def test_password_keys_and_invalid_call_arguments_are_redacted(self):
        locator = SimpleNamespace(count=lambda: 1, evaluate=lambda *_args, **_options: True)
        parameters = {"key": "secret-password", "text": "secret-password",
            "playwright.positional_arguments": ["secret-password"],
            "playwright.keyword_arguments": {"value": "secret-password"}}
        with patch("testar_evidence_playwright.evidence.target_path", return_value=None):
            with self.start() as evidence:
                evidence.perform("press", "Type password", locator, parameters, lambda: None)
        text = (self.directory / "evidence.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret-password", text)
        execution = next(iter(EvidenceReader.read(self.directory).executions.values()))
        saved = execution["execution"]["parameters"]
        self.assertEqual(sorted(parameters), saved["playwright.redacted_parameters"])

    def test_unresolved_value_target_is_redacted(self):
        with self.start() as evidence:
            evidence.perform("fill", "Fill unmapped target", None, {"value": "secret-password"}, lambda: None)
        text = (self.directory / "evidence.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret-password", text)

    def test_element_handle_fill_redacts_the_password_and_native_error_details(self):
        handle = object.__new__(ElementHandle)
        metadata = describe_action(handle, self.page, "fill", handle.fill, ("secret-password",), {})
        failure = RuntimeError("Cannot fill secret-password")

        def operation():
            raise failure

        with self.start() as evidence:
            with self.assertRaises(RuntimeError) as caught:
                evidence.perform(**metadata, operation=operation)
            self.assertIs(failure, caught.exception)
        text = (self.directory / "evidence.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret-password", text)
        execution = next(iter(EvidenceReader.read(self.directory).executions.values()))
        self.assertEqual("rejected", execution["status"])
        self.assertIn("details omitted", execution["error"])

    def test_nonpassword_input_arguments_remain_available(self):
        locator = SimpleNamespace(count=lambda: 1, evaluate=lambda *_args, **_options: False)
        with patch("testar_evidence_playwright.evidence.target_path", return_value=None):
            with self.start() as evidence:
                evidence.perform("press", "Type a key", locator, {"key": "ArrowDown"}, lambda: None)
        execution = next(iter(EvidenceReader.read(self.directory).executions.values()))
        self.assertEqual("ArrowDown", execution["execution"]["parameters"]["key"])


if __name__ == "__main__":
    unittest.main()

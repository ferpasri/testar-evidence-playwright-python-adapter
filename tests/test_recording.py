"""Verify failure handling without requiring a running browser."""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from testar_evidence import ObservedValue, Oracle, EvidenceError, EvidenceReader, Widget
from testar_evidence_playwright import CaptureOptions, PlaywrightEvidence
from testar_evidence_playwright.capture import BrowserSnapshot


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "evidence"
        patcher = patch("testar_evidence_playwright.evidence.capture_page", side_effect=lambda *_:
            BrowserSnapshot(1, {"ui.title": "Fixture"}, [Widget("root")], None))
        self.capture = patcher.start()
        self.addCleanup(patcher.stop)
        self.page = SimpleNamespace(url="about:blank")

    def start(self):
        return PlaywrightEvidence.start(self.output, self.page, CaptureOptions(screenshots=False))

    def test_nested_recording_is_rejected_before_the_inner_operation_runs(self):
        calls = []
        with self.start() as evidence:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                evidence.perform("macro", "Outer action", None, {}, lambda:
                    evidence.perform("click", "Nested action", None, {}, lambda: calls.append("inner")))
        evidence = EvidenceReader.read(self.output)
        self.assertEqual([], calls)
        self.assertEqual(1, len(evidence.action_executions))
        self.assertEqual("failed", next(iter(evidence.action_executions.values()))["status"])
        self.assertEqual([], evidence.recorded_transitions)

    def test_invalid_check_data_is_rejected_before_running_the_test_oracle(self):
        calls = []
        with self.start() as evidence:
            with self.assertRaises(EvidenceError):
                evidence.checks.evaluate("Check", oracle=Oracle(""), assertion=lambda: calls.append("assertion"),
                    observed_value=lambda: ObservedValue.value("value"))
        self.assertEqual([], calls)
        self.assertEqual([], EvidenceReader.read(self.output).oracle_results)

    def test_interrupted_action_keeps_the_original_exception_and_rejection(self):
        failure = KeyboardInterrupt("Stop")

        def operation():
            raise failure

        with self.assertRaises(KeyboardInterrupt) as caught:
            with self.start() as evidence:
                evidence.perform("custom", "Interrupted action", None, {}, operation)
        self.assertIs(failure, caught.exception)
        evidence = EvidenceReader.read(self.output)
        self.assertEqual("failed", next(iter(evidence.action_executions.values()))["status"])
        self.assertEqual("action_failed", next(iter(evidence.test_runs.values()))["reason"])

    def test_persistence_failure_keeps_saved_prefix_and_releases_the_writer(self):
        evidence = self.start()
        evidence.observe()
        failure = OSError("Disk unavailable")
        with patch.object(evidence._store, "record", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                evidence.perform("click", "Operation", None, {}, lambda: self.fail("Must not execute"))
            self.assertIs(failure, caught.exception)
        with self.assertRaisesRegex(RuntimeError, "writer failed"):
            evidence.observe()
        evidence.close()
        evidence = EvidenceReader.read(self.output)
        self.assertEqual(1, len(evidence.observations))
        self.assertEqual("running", next(iter(evidence.test_runs.values()))["status"])
        self.assertEqual({}, evidence.action_executions)

    def test_cleanup_failure_does_not_replace_a_failed_assertion(self):
        failure = AssertionError("Expected confirmation")

        def assertion():
            raise failure

        evidence = self.start()
        original = evidence._store.record

        def record(event):
            if event.event_type == "oracle_result_recorded":
                raise OSError("Disk unavailable")
            return original(event)

        with patch.object(evidence._store, "record", side_effect=record):
            with self.assertRaises(AssertionError) as caught:
                with evidence:
                    evidence.checks.evaluate("Confirmation", oracle=Oracle("fixture:confirmation"),
                        assertion=assertion, observed_value=lambda: ObservedValue.value("Wrong"))
        self.assertIs(failure, caught.exception)
        self.assertTrue(any("Evidence" in note for note in caught.exception.__notes__))
        self.assertEqual(1, len(EvidenceReader.read(self.output).observations))

    def test_session_operations_require_the_original_thread(self):
        errors = []
        with self.start() as evidence:
            def observe():
                try:
                    evidence.observe()
                except RuntimeError as error:
                    errors.append(error)

            thread = Thread(target=observe)
            thread.start()
            thread.join(timeout=5)
        self.assertEqual(1, len(errors))
        self.assertIn("Playwright thread", str(errors[0]))
        self.assertEqual(1, self.capture.call_count)

    def test_explicit_secret_action_errors_do_not_include_the_secret(self):
        def operation():
            raise RuntimeError("Failed to enter super-secret-value")

        with self.start() as evidence:
            with self.assertRaises(RuntimeError):
                evidence.perform("fill", "Secret action", None,
                    {"playwright.redacted_parameters": ["value"]}, operation)
        text = (self.output / "evidence.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("super-secret-value", text)
        self.assertIn("RuntimeError", text)

    def test_assertion_timeout_requires_a_positive_finite_number(self):
        with self.start() as evidence:
            for value in (0, -1, float("nan"), float("inf"), True, "100"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    evidence.checks.timeout_millis = value

    def test_recording_error_remains_available_alongside_an_original_oracle_failure(self):
        failure = AssertionError("Original oracle finding")
        recording_failure = OSError("Cannot capture the page")

        def assertion():
            raise failure

        with self.start() as evidence:
            with patch.object(evidence, "_record_oracle_result", side_effect=recording_failure):
                with self.assertRaises(AssertionError) as caught:
                    evidence.checks.evaluate("Condition", oracle=Oracle("fixture:condition"),
                        assertion=assertion, observed_value=ObservedValue.unknown)
            self.assertIs(failure, caught.exception)
            self.assertIs(recording_failure, evidence.checks.recording_error)
            evidence.checks.evaluate("Another condition", oracle=Oracle("fixture:other"),
                assertion=lambda: None, observed_value=ObservedValue.unknown)
            self.assertIsNone(evidence.checks.recording_error)


if __name__ == "__main__":
    unittest.main()

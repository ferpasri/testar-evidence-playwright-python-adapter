"""Frameworks can report their existing action and check boundaries explicitly."""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from testar_evidence import EvidenceError, EvidenceReader, Widget
from testar_evidence_playwright import CaptureOptions, PlaywrightEvidence
from testar_evidence_playwright.capture import BrowserSnapshot


class ManualRecordingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "trace"
        self.page = SimpleNamespace(url="about:blank")
        patcher = patch("testar_evidence_playwright.evidence.capture_page", side_effect=lambda *_:
            BrowserSnapshot(1, {"ui.title": "Fixture"}, [Widget("root")], None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, directory=None):
        return PlaywrightEvidence.start(directory or self.output, self.page,
            CaptureOptions(screenshots=False), origin="Example exploratory framework")

    def test_explicit_boundaries_form_one_connected_trace_with_checks_on_the_recorded_state(self):
        with self.start() as evidence:
            initial = evidence.capture()
            evidence.select_action("click", "First action", None, {})
            evidence.submit_action(True)
            middle = evidence.capture()
            evidence.checks.record_result("fixture:check", "Expected result", "passed")
            evidence.checks.record_result("fixture:another", "Another check", "failed", "Wrong result")
            evidence.select_action("click", "Second action", None, {})
            evidence.submit_action(True)
            final = evidence.capture()
            evidence.finish()
        model = EvidenceReader.read(self.output)
        self.assertEqual([initial, middle, final], list(model.observations))
        self.assertEqual(2, len(model.links()))
        self.assertEqual(["passed", "failed"], [check["status"] for check in model.checks])
        self.assertTrue(all(check["target"] == {"kind": "observation", "observation": middle}
            for check in model.checks))
        self.assertTrue(all(check["actual"] == {"kind": "unknown"} for check in model.checks))
        self.assertTrue(all("expected" not in check for check in model.checks))

    def test_rejected_outcomes_keep_the_error_and_result_capture_without_an_action_link(self):
        with self.start() as evidence:
            evidence.capture()
            evidence.select_action("click", "Blocked button", None, {})
            evidence.submit_action(False, "execution_skipped: blocked")
            evidence.capture()
        model = EvidenceReader.read(self.output)
        execution = next(iter(model.executions.values()))
        self.assertEqual("rejected", execution["status"])
        self.assertEqual("execution_skipped: blocked", execution["error"])
        self.assertEqual(2, len(model.observations))
        self.assertEqual([], model.links())

    def test_unexpected_action_exception_is_rejected_on_context_exit_and_preserved(self):
        failure = RuntimeError("Browser action failed")
        with self.assertRaises(RuntimeError) as caught:
            with self.start() as evidence:
                evidence.select_action("click", "Button", None, {})
                raise failure
        self.assertIs(failure, caught.exception)
        execution = next(iter(EvidenceReader.read(self.output).executions.values()))
        self.assertEqual("rejected", execution["status"])
        self.assertIn("Browser action failed", execution["error"])

    def test_unreported_action_remains_incomplete_when_the_run_ends(self):
        with self.start() as evidence:
            evidence.select_action("click", "Button", None, {})
        execution = next(iter(EvidenceReader.read(self.output).executions.values()))
        self.assertEqual("incomplete", execution["status"])

    def test_action_lifecycle_rejects_conflicting_calls_without_changing_the_selected_action(self):
        with self.start() as evidence:
            with self.assertRaisesRegex(RuntimeError, "No selected action"):
                evidence.submit_action(True)
            evidence.select_action("click", "Button", None, {})
            with self.assertRaisesRegex(RuntimeError, "outcome"):
                evidence.capture()
            with self.assertRaisesRegex(RuntimeError, "outcome"):
                evidence.select_action("click", "Another button", None, {})
            with self.assertRaises(EvidenceError):
                evidence.submit_action("yes")
            evidence.submit_action(True)
            evidence.capture()
        self.assertEqual(1, len(EvidenceReader.read(self.output).links()))

    def test_start_trace_reserves_safe_unique_names_and_retains_the_original_labels(self):
        with patch("testar_evidence_playwright.evidence.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 22, 15, 30, 12, 123000)
            directories = []
            for _ in range(2):
                with PlaywrightEvidence.start_trace(self.root, self.page, sut="../Customer portal",
                        test_objective="form checks", sequence=1,
                        options=CaptureOptions(screenshots=False)) as evidence:
                    directories.append(evidence.directory)
        self.assertEqual(self.root.resolve(), directories[0].parent)
        self.assertEqual("Customer-portal_form-checks_sequence-1_trace-2026-10-22_15-30-12-123",
            directories[0].name)
        self.assertEqual(directories[0].name + "-2", directories[1].name)
        self.assertTrue((directories[0] / "evidence.jsonl").exists())
        properties = next(iter(EvidenceReader.read(directories[0]).runs.values()))["run"]["properties"]
        self.assertEqual("../Customer portal", properties["playwright.sut"])
        self.assertEqual("form checks", properties["evidence.test_objective"])

    def test_invalid_reported_check_does_not_replace_a_valid_recorded_result(self):
        with self.start() as evidence:
            observation = evidence.capture()
            with self.assertRaises(EvidenceError):
                evidence.checks.record_result("fixture:rule", "Condition", "guessed")
            evidence.checks.record_result("fixture:rule", "Condition", "error", "Oracle unavailable")
        checks = EvidenceReader.read(self.output).checks
        self.assertEqual(1, len(checks))
        self.assertEqual("error", checks[0]["status"])
        self.assertEqual(observation, checks[0]["target"]["observation"])


if __name__ == "__main__":
    unittest.main()

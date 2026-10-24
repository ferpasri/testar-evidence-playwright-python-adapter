"""Frameworks can report their existing action and check boundaries explicitly."""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from testar_evidence import Oracle, EvidenceError, EvidenceReader, Widget
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
            evidence.report_action_result("returned")
            middle = evidence.capture()
            evidence.checks.record_result(Oracle("fixture:check"), "Expected result", "passed")
            evidence.checks.record_result(Oracle("fixture:another"), "Another check", "failed", "Wrong result")
            evidence.select_action("click", "Second action", None, {})
            evidence.report_action_result("returned")
            final = evidence.capture()
            evidence.finish()
        evidence = EvidenceReader.read(self.output)
        self.assertEqual([initial, middle, final], list(evidence.observations))
        self.assertEqual(2, len(evidence.recorded_transitions))
        self.assertEqual(["passed", "failed"], [check["oracle_status"] for check in evidence.oracle_results])
        self.assertTrue(all(check["oracle_target"] == {"target_type": "observation", "observation": middle}
            for check in evidence.oracle_results))
        self.assertTrue(all(check["observed_value"] == {"value_status": "unknown"} for check in evidence.oracle_results))
        self.assertTrue(all("expected" not in check for check in evidence.oracle_results))

    def test_rejected_outcomes_keep_the_error_and_result_capture_without_an_action_link(self):
        with self.start() as evidence:
            evidence.capture()
            evidence.select_action("click", "Blocked button", None, {})
            evidence.report_action_result("error", "execution_skipped: blocked")
            evidence.capture()
        evidence = EvidenceReader.read(self.output)
        execution = next(iter(evidence.action_executions.values()))
        self.assertEqual("failed", execution["status"])
        self.assertEqual("execution_skipped: blocked", execution["execution_result"]["error"])
        self.assertEqual(2, len(evidence.observations))
        self.assertEqual([], evidence.recorded_transitions)

    def test_unexpected_action_exception_is_rejected_on_context_exit_and_preserved(self):
        failure = RuntimeError("Browser action failed")
        with self.assertRaises(RuntimeError) as caught:
            with self.start() as evidence:
                evidence.select_action("click", "Button", None, {})
                raise failure
        self.assertIs(failure, caught.exception)
        execution = next(iter(EvidenceReader.read(self.output).action_executions.values()))
        self.assertEqual("failed", execution["status"])
        self.assertIn("Browser action failed", execution["execution_result"]["error"])

    def test_unreported_action_remains_incomplete_when_the_run_ends(self):
        with self.start() as evidence:
            evidence.select_action("click", "Button", None, {})
        execution = next(iter(EvidenceReader.read(self.output).action_executions.values()))
        self.assertEqual("incomplete", execution["status"])

    def test_action_lifecycle_rejects_conflicting_calls_without_changing_the_selected_action(self):
        with self.start() as evidence:
            with self.assertRaisesRegex(RuntimeError, "No selected action"):
                evidence.report_action_result("returned")
            evidence.select_action("click", "Button", None, {})
            with self.assertRaisesRegex(RuntimeError, "outcome"):
                evidence.capture()
            with self.assertRaisesRegex(RuntimeError, "outcome"):
                evidence.select_action("click", "Another button", None, {})
            with self.assertRaises(EvidenceError):
                evidence.report_action_result("yes")
            evidence.report_action_result("returned")
            evidence.capture()
        self.assertEqual(1, len(EvidenceReader.read(self.output).recorded_transitions))

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
        properties = next(iter(EvidenceReader.read(directories[0]).test_runs.values()))["test_run"]["properties"]
        self.assertEqual("../Customer portal", properties["playwright.sut"])
        self.assertEqual("form checks", properties["evidence.test_objective"])

    def test_invalid_reported_check_does_not_replace_a_valid_recorded_result(self):
        with self.start() as evidence:
            observation = evidence.capture()
            with self.assertRaises(EvidenceError):
                evidence.checks.record_result(Oracle("fixture:oracle"), "Condition", "guessed")
            evidence.checks.record_result(Oracle("fixture:oracle"), "Condition", "error", "Oracle unavailable")
        checks = EvidenceReader.read(self.output).oracle_results
        self.assertEqual(1, len(checks))
        self.assertEqual("error", checks[0]["oracle_status"])
        self.assertEqual(observation, checks[0]["oracle_target"]["observation"])


if __name__ == "__main__":
    unittest.main()

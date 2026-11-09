"""A controller can select against committed evidence without another source capture."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch

from testar_evidence import EvidenceReader, ReconstructedEvidence, Widget
from testar_evidence_playwright import AvailableAction, CaptureOptions, PlaywrightEvidence
from testar_evidence_playwright.capture import BrowserSnapshot


class SavedObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "trace"
        capture = patch("testar_evidence_playwright.evidence.capture_page",
                        return_value=BrowserSnapshot(1, {"ui.title": "Form"}, [Widget("root")], None))
        self.capture = capture.start()
        self.addCleanup(capture.stop)
        self.page = SimpleNamespace(url="about:blank")

    def start(self):
        return PlaywrightEvidence.start(self.directory, self.page, CaptureOptions(screenshots=False))

    def commit_action(self, recorder):
        action = AvailableAction("click", "Reported action")
        observation = recorder.capture()
        recorder.record_available_actions([action], scope="eligible", observation=observation)
        self.assertEqual(observation, recorder.commit_capture())
        return action, observation

    def test_selection_and_execution_reuse_the_saved_source_observation(self):
        with self.start() as recorder:
            action, source = self.commit_action(recorder)
            snapshot = recorder.recorded_evidence
            snapshot.observations.clear()
            self.assertIn(source, recorder.recorded_evidence.observations)
            # Preparing one click reads only its saved action bindings; copying
            # every previous state and widget would slow long executions.
            with patch.object(ReconstructedEvidence, "snapshot", side_effect=AssertionError("Full history copy")), \
                    patch.object(ReconstructedEvidence, "observations", new_callable=PropertyMock,
                                 side_effect=AssertionError("All observations copied")):
                execution = recorder.select_recorded_action(action, source, {})
            self.assertEqual("result", recorder.execute_selected(lambda: "result"))
            recorder.capture()
            recorder.commit_capture()
            self.assertEqual(2, self.capture.call_count)
        trace = EvidenceReader.read(self.directory)
        self.assertEqual(2, len(trace.observations))
        self.assertEqual(1, len(trace.recorded_transitions))
        self.assertEqual(source, trace.action_executions[execution]["action_execution"]["source_observation"])

    def test_older_observations_and_unreported_actions_are_rejected(self):
        with self.start() as recorder:
            action, previous = self.commit_action(recorder)
            _, current = self.commit_action(recorder)
            with self.assertRaises(ValueError):
                recorder.select_recorded_action(action, previous, {})
            with self.assertRaises(ValueError):
                recorder.select_recorded_action(AvailableAction("click", "Different action"), current, {})
            self.assertEqual({}, recorder.recorded_evidence.action_executions)

    def test_failed_selected_operation_preserves_failure_and_redacts_uncertain_input(self):
        with self.start() as recorder:
            action, observation = self.commit_action(recorder)
            execution = recorder.select_recorded_action(action, observation, {"value": "unique-password-783"})
            def operation():
                raise RuntimeError("Failure containing unique-password-783")
            with self.assertRaises(RuntimeError):
                recorder.execute_selected(operation)
        trace = EvidenceReader.read(self.directory)
        record = trace.action_executions[execution]
        self.assertEqual("failed", record["status"])
        self.assertNotIn("value", record["action_execution"]["parameters"])
        self.assertNotIn("unique-password-783", record["execution_result"]["error"])
        self.assertEqual([], trace.recorded_transitions)

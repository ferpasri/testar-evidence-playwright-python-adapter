"""Schema-3 histories and explicit oracle metadata use the shared evidence contract."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from testar_evidence import EvidenceError, EvidenceReader, ObservedValue, Oracle, OracleSeverity, Widget
from testar_evidence_playwright import CaptureOptions, PlaywrightEvidence
from testar_evidence_playwright.capture import BrowserSnapshot


class SchemaThreeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "trace"
        self.page = SimpleNamespace(url="about:blank")
        capture = patch("testar_evidence_playwright.evidence.capture_page", side_effect=lambda *_:
            BrowserSnapshot(1, {"ui.title": "Fixture"}, [Widget("root")], None))
        capture.start()
        self.addCleanup(capture.stop)

    def start(self, **options):
        return PlaywrightEvidence.start(self.directory, self.page, CaptureOptions(screenshots=False), **options)

    def test_run_and_sequence_lifecycles_use_schema_three_in_the_saved_trace(self):
        with self.start() as recorder:
            recorder.perform("custom", "Operation", None, {}, lambda: None)
        evidence = EvidenceReader.read(self.directory)
        self.assertEqual(1, len(evidence.test_runs))
        self.assertEqual(1, len(evidence.test_sequences))
        self.assertEqual("finished", next(iter(evidence.test_runs.values()))["status"])
        self.assertEqual("finished", next(iter(evidence.test_sequences.values()))["status"])
        events = [json.loads(line) for line in (self.directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(event["schema_version"] == 3 for event in events))
        self.assertEqual(["test_run_started", "test_sequence_started"],
                         [event["event"]["event_type"] for event in events[:2]])
        self.assertEqual(["test_sequence_finished", "test_run_finished"],
                         [event["event"]["event_type"] for event in events[-2:]])
        execution = next(iter(evidence.action_executions.values()))
        self.assertEqual("returned", execution["execution_result"]["outcome"])
        self.assertEqual("completed", execution["status"])
        self.assertEqual(1, len(evidence.recorded_transitions))

    def test_reported_result_keeps_a_precise_target_values_details_and_source_severity(self):
        with self.start() as recorder:
            observation = recorder.capture()
            recorder.checks.record_result(Oracle("fixture:title", "2", {"comparison": "equals"}), "Title check", "failed",
                property="ui.title", expected_value=None, observed_value=ObservedValue.value("Fixture"),
                oracle_details={"caller.attempt": 3}, oracle_severity=OracleSeverity("  Important  ", 25.125))
            recorder.checks.record_result(Oracle("fixture:another"), "Another result", "unknown")
        evidence = EvidenceReader.read(self.directory)
        result = evidence.oracle_results[0]
        self.assertEqual({"target_type": "state_property", "observation": observation, "property": "ui.title"},
                         result["oracle_target"])
        self.assertEqual({"label": "  Important  ", "value": 25.125}, result["oracle_severity"])
        self.assertEqual("failed", result["oracle_status"])
        self.assertEqual(3, result["oracle_details"]["caller.attempt"])
        self.assertEqual("2", result["oracle"]["version"])
        self.assertIn("expected_value", result)
        self.assertIsNone(result["expected_value"])
        self.assertNotIn("oracle_severity", evidence.oracle_results[1])
        self.assertNotIn("expected_value", evidence.oracle_results[1])
        self.assertEqual(1, len(evidence.observations))
        self.assertEqual({}, evidence.action_executions)

    def test_evaluate_preserves_supplied_severity_and_has_no_default_severity(self):
        with self.start() as recorder:
            recorder.checks.evaluate("Explicit severity", oracle=Oracle("fixture:severity"),
                assertion=lambda: None, observed_value=ObservedValue.unknown,
                oracle_severity=OracleSeverity("informational"))
            recorder.checks.evaluate("Omitted severity", oracle=Oracle("fixture:default"),
                assertion=lambda: None, observed_value=ObservedValue.unknown)
        results = EvidenceReader.read(self.directory).oracle_results
        self.assertEqual({"label": "informational"}, results[0]["oracle_severity"])
        self.assertNotIn("oracle_severity", results[1])

    def test_invalid_severity_is_rejected_before_running_the_test_oracle(self):
        called = []
        with self.start() as recorder:
            with self.assertRaises(EvidenceError):
                recorder.checks.evaluate("Check", oracle=Oracle("fixture:invalid"),
                    assertion=lambda: called.append(True), observed_value=ObservedValue.unknown,
                    oracle_severity=OracleSeverity(" "))
        self.assertEqual([], called)
        self.assertEqual([], EvidenceReader.read(self.directory).oracle_results)

    def test_sequence_index_is_stored_as_sequence_metadata(self):
        with self.start(sequence_index=7) as recorder:
            recorder.capture()
        evidence = EvidenceReader.read(self.directory)
        sequence = next(iter(evidence.test_sequences.values()))["test_sequence"]
        self.assertEqual(7, sequence["sequence_index"])
        self.assertEqual("7", sequence["source_sequence"])

    def test_invalid_sequence_index_is_rejected_before_creating_the_trace(self):
        for value in (0, -1, True, 1 << 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.start(sequence_index=value)
            self.assertFalse(self.directory.exists())

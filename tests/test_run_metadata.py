"""Verify optional run descriptions through recording and reopening."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from testar_evidence import EvidenceReader, Widget
from testar_evidence_playwright import CaptureOptions, PlaywrightEvidence, __version__, sync_playwright
from testar_evidence_playwright.capture import BrowserSnapshot


class RunMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "evidence"
        self.page = SimpleNamespace(url="about:blank")
        capture = patch("testar_evidence_playwright.evidence.capture_page", side_effect=lambda *_:
            BrowserSnapshot(1, {"ui.title": "Fixture"}, [Widget("root")], None))
        capture.start()
        self.addCleanup(capture.stop)

    def properties(self, directory=None):
        evidence = EvidenceReader.read(directory or self.output)
        return next(iter(evidence.test_runs.values()))["test_run"]["properties"]

    def test_omitted_descriptions_stay_absent_with_automatic_adapter_identity(self):
        with PlaywrightEvidence.start(self.output, self.page):
            pass
        properties = self.properties()
        self.assertNotIn("evidence.origin", properties)
        self.assertNotIn("evidence.test_objective", properties)
        self.assertEqual("testar-evidence-playwright-python-adapter", properties["evidence.adapter"])
        self.assertEqual(__version__, properties["evidence.adapter_version"])
        self.assertNotIn("playwright.adapter_version", properties)

    def test_flexible_origins_are_run_metadata_and_preserve_original_text(self):
        for index, origin in enumerate(("Custom runner", "  QA équipe / custom CI  ", "Loan scenario script")):
            with self.subTest(origin=origin):
                directory = self.output / str(index)
                with PlaywrightEvidence.start(directory, self.page, origin=origin,
                        test_objective="  Request loan — prêt  ") as evidence:
                    evidence.perform("click", "Fixture click", None, {}, lambda: None)
                properties = self.properties(directory)
                self.assertEqual(origin, properties["evidence.origin"])
                self.assertEqual("  Request loan — prêt  ", properties["evidence.test_objective"])
                execution = next(iter(EvidenceReader.read(directory).action_executions.values()))["action_execution"]
                self.assertNotIn("playwright.origin", execution["parameters"])
                self.assertNotIn("evidence.origin", execution["parameters"])

    def test_descriptions_can_be_supplied_independently(self):
        for key, argument in (("evidence.origin", "origin"), ("evidence.test_objective", "test_objective")):
            with self.subTest(argument=argument):
                directory = self.output / argument
                with PlaywrightEvidence.start(directory, self.page, **{argument: "Caller description"}):
                    pass
                properties = self.properties(directory)
                self.assertEqual("Caller description", properties[key])
                other = "evidence.test_objective" if argument == "origin" else "evidence.origin"
                self.assertNotIn(other, properties)

    def test_shared_properties_are_preserved_without_mutating_the_callers_dictionary(self):
        supplied = {"evidence.origin": "Source name", "evidence.test_objective": "Inspect form",
            "caller.details": {"build": 12}}
        original = deepcopy(supplied)
        with PlaywrightEvidence.start(self.output, self.page, properties=supplied, origin="Source name"):
            pass
        self.assertEqual(original, supplied)
        for key, value in original.items():
            self.assertEqual(value, self.properties()[key])

    def test_invalid_descriptions_are_rejected_before_creating_files(self):
        for argument in ("origin", "test_objective"):
            for invalid in ("", " \t\n", [], {}, 1, False):
                with self.subTest(argument=argument, invalid=invalid), self.assertRaises(ValueError):
                    PlaywrightEvidence.start(self.output, self.page, **{argument: invalid})
                self.assertFalse(self.output.exists())

    def test_invalid_or_conflicting_shared_metadata_is_rejected_before_creating_files(self):
        cases = [
            {"properties": {"evidence.origin": None}},
            {"properties": {"evidence.test_objective": 12}},
            {"properties": {"evidence.origin": "A"}, "origin": "B"},
            {"properties": {"evidence.test_objective": "A"}, "test_objective": "B"},
            {"properties": {"evidence.adapter": "Another adapter"}},
            {"properties": {"evidence.adapter_version": "Another version"}},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                PlaywrightEvidence.start(self.output, self.page, **arguments)
            self.assertFalse(self.output.exists())

    def test_omitted_trace_objective_uses_only_a_filename_fallback(self):
        with PlaywrightEvidence.start_trace(self.output, self.page) as evidence:
            directory = evidence.directory
        self.assertTrue(directory.name.startswith("application_run_trace-"))
        self.assertNotIn("evidence.origin", self.properties(directory))
        self.assertNotIn("evidence.test_objective", self.properties(directory))

    def test_trace_name_uses_the_shared_objective_when_supplied_as_a_property(self):
        with PlaywrightEvidence.start_trace(self.output, self.page,
                properties={"evidence.test_objective": "Inspect form"}) as evidence:
            directory = evidence.directory
        self.assertTrue(directory.name.startswith("application_Inspect-form_trace-"))
        self.assertEqual("Inspect form", self.properties(directory)["evidence.test_objective"])

    def test_invalid_trace_metadata_does_not_reserve_a_directory(self):
        with self.assertRaises(ValueError):
            PlaywrightEvidence.start_trace(self.output, self.page, origin=" ")
        self.assertFalse(self.output.exists())

    def test_invalid_facade_descriptions_are_rejected_before_starting_playwright(self):
        with patch("testar_evidence_playwright.facade.native.sync_playwright") as factory:
            for argument in ("origin", "test_objective"):
                for invalid in ("", " \t", [], 1, False):
                    with self.subTest(argument=argument, invalid=invalid), self.assertRaises(ValueError):
                        sync_playwright(evidence_path=self.output, **{argument: invalid})
            factory.assert_not_called()
        self.assertFalse(self.output.exists())

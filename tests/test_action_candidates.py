"""Tool-reported action candidates remain separate from executed selections."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from testar_evidence import EvidenceReader, Widget
from testar_evidence_playwright import ActionCandidate, CaptureOptions, PlaywrightEvidence
from testar_evidence_playwright.capture import BrowserSnapshot


class ActionCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'trace'
        patcher = patch('testar_evidence_playwright.evidence.capture_page', return_value=
                        BrowserSnapshot(1, {'ui.title': 'Fixture'}, [Widget('root')], None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self):
        return PlaywrightEvidence.start(self.directory, SimpleNamespace(url='about:blank'),
                                        CaptureOptions(screenshots=False))

    def test_unselected_candidates_and_planning_parameters_are_preserved(self):
        first = ActionCandidate('type_text', 'Type', parameters={'strategy': 'random', 'mode': 'append'})
        second = ActionCandidate('type_text', 'Type', parameters={'strategy': 'random', 'mode': 'replace'})
        with self.start() as recorder:
            source = recorder.record_candidates([first, second], scope='eligible')
            recorder.perform(first.kind, first.name, None, {'value': 'input'}, lambda: None, candidate=first)
        model = EvidenceReader.read(self.directory)
        observation = model.observations[source]
        self.assertEqual('eligible', observation['candidate_scope'])
        self.assertEqual(2, len(observation['candidates']))
        execution = next(iter(model.executions.values()))['execution']
        self.assertEqual(source, execution['source'])
        self.assertEqual(first.definition().id, execution['action'])
        self.assertNotEqual(first.definition().id, second.definition().id)
        self.assertEqual(1, len(model.executions))

    def test_empty_eligible_list_is_distinct_from_unknown_candidates(self):
        with self.start() as recorder:
            source = recorder.record_candidates([], scope='eligible')
        self.assertEqual([], EvidenceReader.read(self.directory).observations[source]['candidates'])
        self.assertEqual('eligible', EvidenceReader.read(self.directory).observations[source]['candidate_scope'])

    def test_unreported_selection_is_rejected_before_executing_the_operation(self):
        first = ActionCandidate('click', 'First')
        second = ActionCandidate('click', 'Second')
        called = []
        with self.start() as recorder:
            recorder.record_candidates([first])
            with self.assertRaisesRegex(ValueError, 'not reported'):
                recorder.perform(second.kind, second.name, None, {}, lambda: called.append(True),
                                 candidate=second)
        self.assertEqual([], called)
        self.assertEqual({}, EvidenceReader.read(self.directory).executions)

    def test_recorded_oracle_details_retain_caller_context(self):
        with self.start() as recorder:
            recorder.capture()
            recorder.checks.record_result('fixture:oracle', 'Oracle', 'passed', details={'caller.attempt': 3})
        check = EvidenceReader.read(self.directory).checks[0]
        self.assertEqual(3, check['details']['caller.attempt'])
        self.assertEqual({'kind': 'unknown'}, check['actual'])

"""Caller-reported available actions remain separate from executed selections."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from testar_evidence import Oracle, EvidenceReader, Widget
from testar_evidence_playwright import AvailableAction, CaptureOptions, PlaywrightEvidence
from testar_evidence_playwright.capture import BrowserSnapshot


class AvailableActionTests(unittest.TestCase):
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

    def test_unselected_available_actions_and_planning_parameters_are_preserved(self):
        first = AvailableAction('type_text', 'Type', parameters={'strategy': 'random', 'mode': 'append'})
        second = AvailableAction('type_text', 'Type', parameters={'strategy': 'random', 'mode': 'replace'})
        with self.start() as recorder:
            source = recorder.record_available_actions([first, second], scope='eligible')
            recorder.perform(first.action_type, first.name, None, {'value': 'input'}, lambda: None, available_action=first)
        evidence = EvidenceReader.read(self.directory)
        observation = evidence.observations[source]
        self.assertEqual('eligible', observation['available_action_scope'])
        self.assertEqual(2, len(observation['available_actions']))
        execution = next(iter(evidence.action_executions.values()))['action_execution']
        self.assertEqual(source, execution['source_observation'])
        self.assertEqual(first.description().id, execution['action_description'])
        self.assertNotEqual(first.description().id, second.description().id)
        self.assertEqual(1, len(evidence.action_executions))

    def test_empty_eligible_list_is_distinct_from_unknown_available_actions(self):
        with self.start() as recorder:
            source = recorder.record_available_actions([], scope='eligible')
        self.assertEqual([], EvidenceReader.read(self.directory).observations[source]['available_actions'])
        self.assertEqual('eligible', EvidenceReader.read(self.directory).observations[source]['available_action_scope'])

    def test_unreported_selection_is_rejected_before_executing_the_operation(self):
        first = AvailableAction('click', 'First')
        second = AvailableAction('click', 'Second')
        called = []
        with self.start() as recorder:
            recorder.record_available_actions([first])
            with self.assertRaisesRegex(ValueError, 'not reported'):
                recorder.perform(second.action_type, second.name, None, {}, lambda: called.append(True),
                                 available_action=second)
        self.assertEqual([], called)
        self.assertEqual({}, EvidenceReader.read(self.directory).action_executions)

    def test_recorded_oracle_details_retain_caller_context(self):
        with self.start() as recorder:
            recorder.capture()
            recorder.checks.record_result(Oracle('fixture:oracle'), 'Oracle', 'passed', oracle_details={'caller.attempt': 3})
        check = EvidenceReader.read(self.directory).oracle_results[0]
        self.assertEqual(3, check['oracle_details']['caller.attempt'])
        self.assertEqual({"value_status": "unknown"}, check['observed_value'])

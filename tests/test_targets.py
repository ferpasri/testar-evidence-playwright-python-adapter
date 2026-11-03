"""Batched targets preserve native locator matching and recorded widget bindings."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError
from testar_evidence import EvidenceReader
from testar_evidence_playwright import AvailableAction, CaptureOptions
from testar_evidence_playwright.capture import target_path
from testar_evidence_playwright.sync_api import sync_playwright, evidence_for
from testar_evidence_playwright.targets import remember_target, target_paths, _resolve_batch


class TargetResolutionTests(unittest.TestCase):
    def test_only_simple_unfiltered_page_selectors_are_remembered(self):
        page = object()
        for selector in ('#apply', 'button[id="apply"]', "[name='amount']"):
            locator = SimpleNamespace()
            remember_target(page, "locator", (selector,), {}, locator)
            self.assertEqual((page, selector), locator._evidence_css_target)
        for selector, options in (
            ('button:visible', {}), ('button >> nth=0', {}), ('body > button', {}),
            ('#apply', {"has_text": "Apply"}), ('[id="escaped\\value"]', {}),
        ):
            locator = SimpleNamespace()
            remember_target(page, "locator", (selector,), options, locator)
            self.assertFalse(hasattr(locator, "_evidence_css_target"))

    def test_batch_failure_falls_back_without_dropping_targets(self):
        page = object()
        locators = [SimpleNamespace(_evidence_css_target=(page, '#first')),
                    SimpleNamespace(_evidence_css_target=(page, '#second'))]
        fallback = Mock(side_effect=['path:first', None])
        with patch('testar_evidence_playwright.targets._resolve_batch', side_effect=PlaywrightError('Detached')):
            self.assertEqual(['path:first', None], target_paths(page, locators, fallback=fallback))
        self.assertEqual(locators, [call.args[0] for call in fallback.call_args_list])

    def test_batches_are_bounded_and_foreign_pages_use_native_resolution(self):
        page = object()
        locators = [SimpleNamespace(_evidence_css_target=(page, f'#target-{index}')) for index in range(130)]
        foreign = SimpleNamespace(_evidence_css_target=(object(), '#foreign'))
        fallback = Mock(return_value='foreign-path')
        with patch('testar_evidence_playwright.targets._resolve_batch', side_effect=lambda page, selectors: selectors) as batch:
            paths = target_paths(page, [*locators, foreign, None], fallback=fallback)
        self.assertEqual([128, 2], [len(call.args[1]) for call in batch.call_args_list])
        self.assertEqual(['foreign-path', None], paths[-2:])
        fallback.assert_called_once_with(foreign)


@unittest.skipUnless(os.environ.get('TESTAR_BROWSER_TESTS') == '1', 'Opt-in browser check')
class TargetResolutionBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.manager = sync_playwright(evidence_path=self.temporary.name,
            capture_options=CaptureOptions(screenshots=False))
        self.playwright = self.manager.start()
        self.addCleanup(self.manager.stop)
        self.browser = self.playwright.chromium.launch(channel=os.environ.get('PLAYWRIGHT_CHANNEL', 'chrome'))
        self.page = self.browser.new_page()
        self.page.set_content('<button id="apply">Apply</button><button id="duplicate">Same</button>'
            '<button id="duplicate">Same</button><div id="host"></div><iframe srcdoc="<button>Frame</button>"></iframe>')
        self.page.evaluate("document.querySelector('#host').attachShadow({mode:'open'}).innerHTML = '<button id=shadow>Shadow</button><button id=duplicate>Same</button>'")

    def test_native_results_match_for_shadow_missing_ambiguous_and_fallback_locators(self):
        locators = [self.page.locator('#apply'), self.page.locator('button[id="shadow"]'),
                    self.page.locator('#missing'), self.page.locator('#duplicate'),
                    self.page.get_by_role('button', name='Apply'),
                    self.page.locator('button').nth(0), self.page.locator('#apply', has_text='Apply'),
                    self.page.frame_locator('iframe').get_by_role('button')]
        expected = [target_path(locator) for locator in locators]
        with patch('testar_evidence_playwright.targets._resolve_batch', wraps=_resolve_batch) as batch:
            fallback = Mock(wraps=target_path)
            self.assertEqual(expected, target_paths(self.page, locators, fallback=fallback))
        self.assertEqual(1, batch.call_count)
        self.assertEqual(4, fallback.call_count)
        self.assertIsNotNone(expected[1])
        self.assertEqual([None, None], expected[2:4])
        self.assertIsNone(expected[-1])

    def test_available_actions_keep_bindings_and_resolve_again_for_later_lists(self):
        recorder = evidence_for(self.page)
        actions = [AvailableAction('click', 'Apply', self.page.locator('#apply')),
                   AvailableAction('press', 'Apply Enter', self.page.locator('#apply'), {'key':'Enter'}),
                   AvailableAction('click', 'Shadow', self.page.locator('#shadow')),
                   AvailableAction('click', 'Missing', self.page.locator('#missing'))]
        expected = [target_path(action.locator) for action in actions]
        source = recorder.capture()
        expected_widgets = [None if path is None else recorder._pending.snapshot.widget_at(path).id for path in expected]
        with patch('testar_evidence_playwright.targets._resolve_batch', wraps=_resolve_batch) as batch:
            with patch('testar_evidence_playwright.evidence.target_path', wraps=target_path) as fallback:
                recorder.record_available_actions(actions, observation=source)
                self.assertEqual(1, batch.call_count)
                fallback.assert_not_called()
        second = recorder.capture()
        self.page.evaluate("document.querySelector('#apply').remove()")
        recorder.record_available_actions(actions, observation=second)
        recorder.finish()
        directory = next(Path(self.temporary.name).glob('*/manifest.json')).parent
        evidence = EvidenceReader.read(directory)
        bindings = evidence.observations[source]['available_actions']
        self.assertEqual([action.description().id for action in actions], [binding['action_description'] for binding in bindings])
        self.assertEqual(expected_widgets, [binding['target_widget'] for binding in bindings])
        self.assertIsNone(evidence.observations[second]['available_actions'][0]['target_widget'])

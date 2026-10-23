"""Exercise facade cleanup, callbacks, and secret-value recording."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from testar_evidence import CheckReading, CheckRule, EvidenceReader
from testar_evidence_playwright import CaptureOptions, PlaywrightEvidence, evidence_for, expect, sync_playwright


@unittest.skipUnless(os.environ.get("TESTAR_BROWSER_TESTS") == "1", "Set TESTAR_BROWSER_TESTS=1 to run browser tests")
class PlaywrightFacadeEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "automatic"
        self.playwright = sync_playwright(evidence_path=self.directory, sut="fixture",
            test_objective="facade-edges", capture_options=CaptureOptions(screenshots=False,
                computed_styles=())).start()
        self.addCleanup(self.playwright.stop)
        options = {"headless": True}
        channel = os.environ.get("PLAYWRIGHT_CHANNEL", "chrome").strip()
        if channel:
            options["channel"] = channel
        self.browser = self.playwright.chromium.launch(**options)
        self.addCleanup(self.close_browser)
        self.page = self.browser.new_page()
        self.page.set_default_timeout(1_000)

    def close_browser(self):
        if self.browser.is_connected():
            self.browser.close()

    def document(self, content):
        self.page.evaluate("html => { document.body.innerHTML = html; }", content)

    def models(self):
        return [EvidenceReader.read(path.parent)
            for path in sorted(self.directory.glob("*/manifest.json"))]

    def jsonl(self):
        return "\n".join(path.read_text(encoding="utf-8")
            for path in sorted(self.directory.glob("*/evidence.jsonl")))

    def test_password_typing_and_keyboard_calls_omit_the_supplied_values(self):
        self.document("<input id='password' type='password'>")
        password = self.page.locator("#password")
        password.type("locator-password-secret")
        self.page.evaluate("document.querySelector('input').focus()")
        self.page.keyboard.insert_text("keyboard-password-secret")
        self.page.keyboard.press("x")
        password.clear()
        self.page.close()

        self.assertEqual(4, len(self.models()[0].executions))
        self.assertNotIn("locator-password-secret", self.jsonl())
        self.assertNotIn("keyboard-password-secret", self.jsonl())
        executions = list(self.models()[0].executions.values())
        self.assertNotIn("key", executions[2]["execution"]["parameters"])
        self.assertIn("key", executions[2]["execution"]["parameters"]["playwright.redacted_parameters"])

    def test_password_assertions_keep_the_original_failure_but_omit_its_saved_values(self):
        self.document("<input id='password' type='password'>")
        password = self.page.locator("#password")
        password.fill("actual-password-secret")
        expect(password).to_have_value("actual-password-secret")
        with self.assertRaises(AssertionError) as raised:
            expect(password).to_have_value("expected-password-secret", timeout=100)
        self.assertIn("expected-password-secret", str(raised.exception))
        self.page.close()

        checks = self.models()[0].checks
        self.assertEqual(["passed", "failed"], [check["status"] for check in checks])
        self.assertTrue(all(check["actual"] == {"kind": "unknown"} for check in checks))
        self.assertNotIn("actual-password-secret", self.jsonl())
        self.assertNotIn("expected-password-secret", self.jsonl())

    def test_invalid_password_action_arguments_preserve_type_error_and_are_redacted(self):
        self.document("<input id='password' type='password'>")
        with self.assertRaises(TypeError):
            self.page.locator("#password").fill("invalid-call-secret", invalid_option=True)
        self.page.close()

        execution = next(iter(self.models()[0].executions.values()))
        self.assertEqual("rejected", execution["status"])
        self.assertNotIn("invalid-call-secret", self.jsonl())

    def test_zero_argument_callback_and_listener_removal_keep_native_behavior(self):
        calls = []

        def callback():
            calls.append("called")

        self.page.on("domcontentloaded", callback)
        self.page.goto("data:text/html,<p>First</p>")
        self.page.remove_listener("domcontentloaded", callback)
        self.page.goto("data:text/html,<p>Second</p>")
        self.page.close()

        self.assertEqual(["called"], calls)
        self.assertEqual(2, len(self.models()[0].executions))

    def test_native_page_close_event_finishes_the_cached_trace_without_new_capture(self):
        self.document("<input id='name'>")
        self.page.locator("#name").fill("Saved final value")
        object.__getattribute__(self.page, "close")()

        model = self.models()[0]
        self.assertEqual(1, len(model.links()))
        self.assertTrue(all(run["status"] == "finished" for run in model.runs.values()))
        self.assertIn("Saved final value", self.jsonl())

    def test_action_in_a_callback_during_a_wait_is_recorded(self):
        self.document("<input id='name'>")

        def received(message):
            self.page.locator("#name").fill("Callback value")

        self.page.once("console", received)
        self.page.evaluate("setTimeout(() => console.log('ready'), 30)")
        self.page.wait_for_timeout(500)
        self.assertEqual("Callback value", self.page.locator("#name").input_value())
        self.page.close()

        self.assertEqual(1, len(self.models()[0].executions))

    def test_failed_evidence_writer_does_not_prevent_native_page_cleanup(self):
        self.document("<input id='name'>")
        self.page.locator("#name").fill("Saved value")
        recorder = self.page._evidence_runtime.recorder_for(self.page)
        with patch.object(recorder._store, "record", side_effect=OSError("writer failed")):
            with self.assertRaisesRegex(OSError, "writer failed"):
                self.page.close()
        self.assertTrue(self.page.is_closed())
        self.browser.close()

    def test_deeper_hooks_share_the_automatic_trace_without_duplicate_actions_or_checks(self):
        from testar_evidence_playwright.sync_api import evidence_for as sync_evidence_for

        self.document("<input id='name'>")
        name = self.page.locator("#name")
        name.fill("Automatic value")
        evidence = evidence_for(self.page)
        self.assertIs(evidence, sync_evidence_for(self.page))
        evidence.perform("custom_input", "Set the input through a custom operation", name,
            {"value": "Custom value"}, lambda: name.fill("Custom value"))
        evidence.checks.evaluate("Custom input check", rule=CheckRule("example:input-value"),
            locator=name, property="playwright.dom.value", expected="Custom value",
            assertion=lambda: expect(name).to_have_value("Custom value"),
            reading=lambda: CheckReading.value(name.input_value()))
        self.page.close()

        with self.assertRaisesRegex(RuntimeError, "finished"):
            evidence_for(self.page)
        models = self.models()
        self.assertEqual(1, len(models))
        self.assertEqual(2, len(models[0].executions))
        self.assertEqual(2, len(models[0].links()))
        self.assertEqual(1, len(models[0].checks))
        self.assertEqual("passed", models[0].checks[0]["status"])
        self.assertEqual({"kind": "value", "value": "Custom value"}, models[0].checks[0]["actual"])

    def test_explicit_recording_does_not_repeat_the_wrapped_browser_operation(self):
        self.document("<button onclick='window.count += 1'>Count</button>")
        self.page.evaluate("window.count = 0")
        manual_directory = Path(self.temporary.name) / "manual"
        with PlaywrightEvidence.start(manual_directory, self.page,
                options=CaptureOptions(screenshots=False, computed_styles=())) as evidence:
            evidence.click(self.page.locator("button"))
            evidence.checks.has_text("example:text", "Button text", self.page.locator("button"), "Count")
        self.assertEqual(1, self.page.evaluate("window.count"))
        self.page.close()

        manual = EvidenceReader.read(manual_directory)
        self.assertEqual(1, len(manual.executions))
        self.assertEqual(1, len(manual.checks))
        self.assertTrue(all(len(model.executions) == 0 and model.checks == [] for model in self.models()))


if __name__ == "__main__":
    unittest.main()

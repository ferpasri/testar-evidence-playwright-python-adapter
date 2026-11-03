"""Exercise facade cleanup, callbacks, and secret-value recording."""

from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from testar_evidence import ObservedValue, Oracle, EvidenceReader
from testar_evidence_playwright.capture import capture_page
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

    def traces(self):
        return [EvidenceReader.read(path.parent)
            for path in sorted(self.directory.glob("*/manifest.json"))]

    def operation_reports(self):
        reports = []
        for manifest in self.directory.glob("*/manifest.json"):
            evidence = EvidenceReader.read(manifest.parent)
            for observation in evidence.observations.values():
                for artifact in observation["artifacts"]:
                    if artifact["media_type"] == "application/json":
                        reports.append(json.loads((manifest.parent / artifact["path"]).read_text(encoding="utf-8")))
        return reports

    def test_compound_retains_order_arguments_and_outcomes_without_intermediate_captures(self):
        self.document('<input id="name" value="Original">')
        locator = self.page.locator("#name")
        recorder = evidence_for(self.page)
        def replace():
            locator.fill("")
            locator.press_sequentially("John")
            locator.press("Enter")
            return "completed"
        with patch("testar_evidence_playwright.evidence.capture_page", wraps=capture_page) as capture:
            self.assertEqual("completed", recorder.perform("replace_text", "Replace text", locator,
                {"value": "John"}, replace, record_operations=True))
            self.close_browser()
            self.assertEqual(2, capture.call_count)
        evidence = self.traces()[0]
        self.assertEqual(2, len(evidence.observations))
        self.assertEqual(1, len(evidence.action_executions))
        self.assertEqual(1, len(evidence.recorded_transitions))
        reports = self.operation_reports()
        self.assertEqual(1, len(reports))
        report = reports[0]
        self.assertEqual("compound-action-operations", report["format"])
        self.assertIn(report["action_execution"], evidence.action_executions)
        operations = report["operations"]
        self.assertEqual([1, 2, 3], [item["index"] for item in operations])
        self.assertEqual(["Locator.fill", "Locator.press_sequentially", "Locator.press"],
                         [item["method"] for item in operations])
        self.assertEqual(["", "John"], [item["parameters"]["value"] for item in operations[:2]])
        self.assertEqual("Enter", operations[2]["parameters"]["key"])
        self.assertTrue(all(item["outcome"] == "returned" and item["error"] is None for item in operations))

    def test_compound_failure_keeps_completed_operations_and_original_exception(self):
        self.document('<input id="name" value="Original">')
        locator = self.page.locator("#name")
        recorder = evidence_for(self.page)
        failures = []
        def replace():
            locator.fill("")
            try:
                self.page.locator("#missing").click(timeout=20)
            except Exception as error:
                failures.append(error)
                raise
            locator.press_sequentially("Must not run")
        with self.assertRaises(Exception) as raised:
            recorder.perform("replace_text", "Replace text", locator, {}, replace, record_operations=True)
        self.assertIs(failures[0], raised.exception)
        self.assertEqual("", locator.input_value())
        self.close_browser()
        evidence = self.traces()[0]
        execution = next(iter(evidence.action_executions.values()))
        self.assertEqual("error", execution["execution_result"]["outcome"])
        self.assertEqual([], evidence.recorded_transitions)
        self.assertEqual(["returned", "error"],
                         [item["outcome"] for item in self.operation_reports()[0]["operations"]])

    def test_compound_password_arguments_and_errors_are_redacted_in_artifact_and_events(self):
        self.document('<input id="password" type="password">')
        secret = "compound-secret-value"
        locator = self.page.locator("#password")
        failure = AssertionError("Unexpected " + secret)
        def replace():
            locator.fill(secret)
            locator.press("Enter")
            raise failure
        with self.assertRaises(AssertionError) as raised:
            evidence_for(self.page).perform("replace_text", "Replace secret", locator,
                {"value": secret}, replace, record_operations=True)
        self.assertIs(failure, raised.exception)
        self.close_browser()
        reports = self.operation_reports()
        self.assertNotIn(secret, self.jsonl() + json.dumps(reports))
        self.assertEqual(["value"], reports[0]["operations"][0]["parameters"]["playwright.redacted_parameters"])
        self.assertEqual(["key"], reports[0]["operations"][1]["parameters"]["playwright.redacted_parameters"])

    def test_compound_page_closure_keeps_operation_log_with_unavailable_capture(self):
        self.document('<input id="name">')
        locator = self.page.locator("#name")
        def replace_and_close():
            locator.fill("John")
            self.page.close()
        evidence_for(self.page).perform("custom", "Replace and close", locator, {},
            replace_and_close, record_operations=True)
        self.close_browser()
        evidence = self.traces()[0]
        observations = list(evidence.observations.values())
        self.assertEqual("unavailable", observations[-1]["recorded_state"]["capture"])
        self.assertEqual([], observations[-1]["recorded_state"]["widgets"])
        self.assertEqual(1, len(self.operation_reports()[0]["operations"]))

    def test_compound_does_not_group_preceding_login_operations(self):
        self.document('<input id="username"><input id="password" type="password"><button id="login">Sign in</button>')
        self.page.locator("#username").fill("John")
        self.page.locator("#password").fill("demo")
        self.page.locator("#login").click()
        locator = self.page.locator("#username")
        def replace():
            locator.fill("")
            locator.press_sequentially("Jane")
        evidence_for(self.page).perform("replace_text", "Replace text", locator, {},
            replace, record_operations=True)
        self.close_browser()
        evidence = self.traces()[0]
        self.assertEqual(4, len(evidence.action_executions))
        self.assertEqual(4, len(evidence.recorded_transitions))
        self.assertEqual(1, len(self.operation_reports()))

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

        self.assertEqual(4, len(self.traces()[0].action_executions))
        self.assertNotIn("locator-password-secret", self.jsonl())
        self.assertNotIn("keyboard-password-secret", self.jsonl())
        executions = list(self.traces()[0].action_executions.values())
        self.assertNotIn("key", executions[2]["action_execution"]["parameters"])
        self.assertIn("key", executions[2]["action_execution"]["parameters"]["playwright.redacted_parameters"])

    def test_password_assertions_keep_the_original_failure_but_omit_its_saved_values(self):
        self.document("<input id='password' type='password'>")
        password = self.page.locator("#password")
        password.fill("actual-password-secret")
        expect(password).to_have_value("actual-password-secret")
        with self.assertRaises(AssertionError) as raised:
            expect(password).to_have_value("expected-password-secret", timeout=100)
        self.assertIn("expected-password-secret", str(raised.exception))
        self.page.close()

        checks = self.traces()[0].oracle_results
        self.assertEqual(["passed", "failed"], [check["oracle_status"] for check in checks])
        self.assertTrue(all(check["observed_value"] == {"value_status": "unknown"} for check in checks))
        self.assertNotIn("actual-password-secret", self.jsonl())
        self.assertNotIn("expected-password-secret", self.jsonl())

    def test_invalid_password_action_arguments_preserve_type_error_and_are_redacted(self):
        self.document("<input id='password' type='password'>")
        with self.assertRaises(TypeError):
            self.page.locator("#password").fill("invalid-call-secret", invalid_option=True)
        self.page.close()

        execution = next(iter(self.traces()[0].action_executions.values()))
        self.assertEqual("failed", execution["status"])
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
        self.assertEqual(2, len(self.traces()[0].action_executions))

    def test_native_page_close_event_finishes_the_cached_trace_without_new_capture(self):
        self.document("<input id='name'>")
        self.page.locator("#name").fill("Saved final value")
        object.__getattribute__(self.page, "close")()

        evidence = self.traces()[0]
        self.assertEqual(1, len(evidence.recorded_transitions))
        self.assertTrue(all(run["status"] == "finished" for run in evidence.test_runs.values()))
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

        self.assertEqual(1, len(self.traces()[0].action_executions))

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
        evidence.checks.evaluate("Custom input check", oracle=Oracle("example:input-value"),
            locator=name, property="playwright.dom.value", expected_value="Custom value",
            assertion=lambda: expect(name).to_have_value("Custom value"),
            observed_value=lambda: ObservedValue.value(name.input_value()))
        self.page.close()

        with self.assertRaisesRegex(RuntimeError, "finished"):
            evidence_for(self.page)
        traces = self.traces()
        self.assertEqual(1, len(traces))
        self.assertEqual(2, len(traces[0].action_executions))
        self.assertEqual(2, len(traces[0].recorded_transitions))
        self.assertEqual(1, len(traces[0].oracle_results))
        self.assertEqual("passed", traces[0].oracle_results[0]["oracle_status"])
        self.assertEqual({"value_status": "value", "value": "Custom value"}, traces[0].oracle_results[0]["observed_value"])

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
        self.assertEqual(1, len(manual.action_executions))
        self.assertEqual(1, len(manual.oracle_results))
        self.assertTrue(all(len(evidence.action_executions) == 0 and evidence.oracle_results == [] for evidence in self.traces()))


if __name__ == "__main__":
    unittest.main()

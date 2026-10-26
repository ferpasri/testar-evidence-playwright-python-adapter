"""Exercise recording against a local website through the real Playwright browser."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright
from testar_evidence import ObservedValue, Oracle, EvidenceError, EvidenceReader
from testar_evidence_playwright import AvailableAction, CaptureOptions, PlaywrightEvidence

from examples.parabank_denied_loan import run


class LoanFixture:
    """Keep the loan example deterministic without accessing an external application."""

    def __enter__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                length = int(self.headers.get("Content-Length", "0"))
                values = parse_qs(self.rfile.read(length).decode("utf-8"))
                if self.path == "/login":
                    if values == {"username": ["john"], "password": ["demo"]}:
                        content = "<h2>Account Services</h2><a href='/requestloan'>Request Loan</a>"
                    else:
                        content = "<h1>Incorrect credentials</h1>"
                elif self.path == "/requestloan":
                    content = """
                        <h2>Account Services</h2><h1>Apply for a Loan</h1>
                        <form action='/apply' method='post'>
                            <input id='amount' name='amount'>
                            <input id='downPayment' name='downPayment'>
                            <input type='submit' value='Apply Now'>
                        </form>
                        """
                elif self.path == "/apply":
                    denied = values == {"amount": ["999999"], "downPayment": ["1"]}
                    content = "<p id='loanStatus'>" + ("Denied" if denied else "Approved") + "</p>"
                else:
                    content = """
                        <h1>Customer Login</h1><form action='/login' method='post'>
                            <input name='username'>
                            <input name='password' type='password' value='stored-password'>
                            <input type='submit' value='Log In'>
                        </form>
                        """
                document = ("<!doctype html><html><head><meta charset='utf-8'>"
                            "<title>Local loan fixture</title></head><body>"
                            + content + "</body></html>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(document)))
                self.end_headers()
                self.wfile.write(document)

            def log_message(self, format, *arguments):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        return self

    def __exit__(self, exception_type, exception, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@unittest.skipUnless(os.environ.get("TESTAR_BROWSER_TESTS") == "1", "Set TESTAR_BROWSER_TESTS=1 to run browser tests")
class PlaywrightEvidenceBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            channel = os.environ.get("PLAYWRIGHT_CHANNEL", "chrome").strip()
            options = {"headless": True}
            if channel:
                options["channel"] = channel
            cls.browser = cls.playwright.chromium.launch(**options)
        except BaseException:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "trace"
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(2_000)

    def read(self):
        return EvidenceReader.read(self.directory)

    def test_loan_script_saves_eight_actions_and_three_precise_checks(self):
        with LoanFixture() as fixture:
            with PlaywrightEvidence.start(self.directory, self.page) as evidence:
                run(evidence, self.page, fixture.url)
                evidence.finish("completed")
                evidence.finish("completed")

        evidence = self.read()
        self.assertEqual(8, len(evidence.action_executions))
        self.assertEqual(8, len(evidence.recorded_transitions))
        self.assertGreaterEqual(len(evidence.observations), 9)
        self.assertEqual(3, len(evidence.oracle_results))
        self.assertEqual("completed", next(iter(evidence.test_runs.values()))["reason"])
        for check in evidence.oracle_results:
            self.assertEqual("passed", check["oracle_status"])
            self.assertEqual("widget_property", check["oracle_target"]["target_type"])
        self.assertEqual({"value_status": "value", "value": "Denied"}, evidence.oracle_results[-1]["observed_value"])
        for observation in evidence.observations.values():
            self.assertEqual("unknown", observation["available_action_scope"])
            self.assertEqual(1, len(observation["artifacts"]))
            reference = observation["artifacts"][0]
            content = (self.directory / reference["path"]).read_bytes()
            self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(hashlib.sha256(content).hexdigest(), reference["sha256"])
        for execution in evidence.action_executions.values():
            self.assertEqual("completed", execution["status"])
            self.assertIn(execution["action_execution"]["source_observation"], evidence.observations)
            self.assertIn(execution["resulting_observation"], evidence.observations)

    def test_failed_assertion_saves_actual_reading_and_run_reason(self):
        self.page.set_content("<p id='status'>Actual</p>")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.checks.timeout_millis = 100
            with self.assertRaises(AssertionError):
                evidence.checks.has_text("example:text", "Expected text", self.page.locator("#status"), "Expected")

        evidence = self.read()
        self.assertEqual("failed", evidence.oracle_results[0]["oracle_status"])
        self.assertEqual({"value_status": "value", "value": "Actual"}, evidence.oracle_results[0]["observed_value"])
        self.assertEqual("check_failed", next(iter(evidence.test_runs.values()))["reason"])
        self.assertTrue(evidence.oracle_results[0]["oracle_details"])

    def test_rejected_browser_action_has_no_result_or_fabricated_link(self):
        self.page.set_content("<p>Ready</p>")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            with self.assertRaises(PlaywrightTimeoutError):
                evidence.click(self.page.locator("#missing"), timeout=100)

        evidence = self.read()
        execution = next(iter(evidence.action_executions.values()))
        self.assertEqual("failed", execution["status"])
        self.assertIsNone(execution["resulting_observation"])
        self.assertIn("missing", execution["execution_result"]["error"])
        self.assertEqual([], evidence.recorded_transitions)
        self.assertEqual("action_failed", next(iter(evidence.test_runs.values()))["reason"])

    def test_custom_oracle_error_keeps_original_exception_and_unknown_reading(self):
        self.page.set_content("<p id='status'>Ready</p>")
        original = RuntimeError("Oracle API unavailable")

        def assert_condition():
            raise original

        def read_actual():
            raise ValueError("Reading unavailable")

        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            with self.assertRaises(RuntimeError) as raised:
                evidence.checks.evaluate("Custom oracle", oracle=Oracle("example:custom", "7"),
                                         locator=self.page.locator("#status"), property="custom.value",
                                         expected_value=None, assertion=assert_condition, observed_value=read_actual)
            self.assertIs(original, raised.exception)

        check = self.read().oracle_results[0]
        self.assertEqual("error", check["oracle_status"])
        self.assertEqual({"value_status": "unknown"}, check["observed_value"])
        self.assertIn("expected_value", check)
        self.assertIsNone(check["expected_value"])
        self.assertEqual("7", check["oracle"]["version"])

    def test_unsupplied_expectation_is_distinct_from_explicit_null(self):
        self.page.set_content("<p id='status'>Ready</p>")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.checks.evaluate("No expectation", oracle=Oracle("example:none"),
                                     locator=self.page.locator("#status"), property="custom.value",
                                     assertion=lambda: None, observed_value=lambda: ObservedValue.value(None))
            evidence.checks.evaluate("Null expectation", oracle=Oracle("example:null"),
                                     locator=self.page.locator("#status"), property="custom.value",
                                     expected_value=None, assertion=lambda: None, observed_value=lambda: ObservedValue.value(None))

        checks = self.read().oracle_results
        self.assertNotIn("expected_value", checks[0])
        self.assertIsNone(checks[1]["expected_value"])
        self.assertEqual({"value_status": "value", "value": None}, checks[0]["observed_value"])

    def test_missing_attribute_is_an_absent_reading(self):
        self.page.set_content("<img id='help'>")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.checks.timeout_millis = 100
            with self.assertRaises(AssertionError):
                evidence.checks.has_attribute("example:alt", "Image description", self.page.locator("#help"), "alt", "Help")

        check = self.read().oracle_results[0]
        self.assertEqual("failed", check["oracle_status"])
        self.assertEqual({"value_status": "absent"}, check["observed_value"])
        self.assertEqual("widget_property", check["oracle_target"]["target_type"])
        self.assertEqual("playwright.attribute.alt", check["oracle_target"]["property"])

    def test_value_check_reads_current_input_value(self):
        self.page.set_content("<input id='name' value='Original'>")
        self.page.locator("#name").fill("Current")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.checks.has_value("example:value", "Current input value", self.page.locator("#name"), "Current")

        check = self.read().oracle_results[0]
        self.assertEqual("passed", check["oracle_status"])
        self.assertEqual({"value_status": "value", "value": "Current"}, check["observed_value"])
        self.assertEqual("playwright.dom.value", check["oracle_target"]["property"])

    def test_assertion_retries_save_one_final_evaluation(self):
        self.page.set_content("""
            <p id='status'>Loading</p>
            <script>setTimeout(() => document.getElementById('status').textContent = 'Ready', 250)</script>
            """)
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.checks.has_text("example:ready", "Ready", self.page.locator("#status"), "Ready")

        checks = self.read().oracle_results
        self.assertEqual(1, len(checks))
        self.assertEqual("passed", checks[0]["oracle_status"])
        self.assertEqual({"value_status": "value", "value": "Ready"}, checks[0]["observed_value"])

    def test_consecutive_checks_save_distinct_visits_without_actions(self):
        self.page.set_content("<p id='status' title='Tooltip'>Ready</p>")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.checks.has_text("example:text", "Text", self.page.locator("#status"), "Ready")
            evidence.checks.has_attribute("example:title", "Title", self.page.locator("#status"), "title", "Tooltip")

        evidence = self.read()
        self.assertEqual(2, len(evidence.oracle_results))
        self.assertNotEqual(evidence.oracle_results[0]["oracle_target"]["observation"], evidence.oracle_results[1]["oracle_target"]["observation"])
        self.assertEqual({}, evidence.action_executions)
        self.assertEqual([], evidence.recorded_transitions)

    def test_available_action_execution_retains_source_widget_binding(self):
        self.page.set_content('<button id="first">First</button><button id="second">Second</button>')
        first = AvailableAction("click", "Choose", self.page.locator("#first"))
        second = AvailableAction("click", "Choose", self.page.locator("#second"))
        with PlaywrightEvidence.start(self.directory, self.page) as recorder:
            source = recorder.record_available_actions([first, second], scope="eligible")
            recorder.perform(second.action_type, second.name, second.locator, {}, second.locator.click,
                             available_action=second)
            recorder.capture()
        evidence = self.read()
        observation = evidence.observations[source]
        execution = next(iter(evidence.action_executions.values()))["action_execution"]
        widget = next(item for item in observation["recorded_state"]["widgets"]
                      if item["properties"].get("playwright.attribute.id") == "second")
        self.assertEqual(2, len(observation["available_actions"]))
        self.assertEqual(source, execution["source_observation"])
        self.assertEqual(second.description().id, execution["action_description"])
        self.assertEqual(widget["id"], execution["target_widget"])
        self.assertIn({"action_description": execution["action_description"], "target_widget": widget["id"]},
                      observation["available_actions"])
        self.assertEqual(1, len(evidence.recorded_transitions))

    def test_custom_action_preserves_parameters_and_caller_operation(self):
        self.page.set_content("<button id='button' onclick=\"this.textContent='Done'\">Continue</button>")
        parameters = {"nested": {"number": 9, "values": ["a", "b"]}, "optional": None}
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.perform("custom", "Custom action", self.page.locator("#button"), parameters,
                             lambda: self.page.locator("#button").click())

        evidence = self.read()
        execution = next(iter(evidence.action_executions.values()))
        self.assertEqual("completed", execution["status"])
        for key, value in parameters.items():
            self.assertEqual(value, execution["action_execution"]["parameters"][key])
        self.assertEqual("Done", self.page.locator("#button").inner_text())
        action = evidence.action_descriptions[execution["action_execution"]["action_description"]]
        self.assertEqual("custom", action["action_type"])

    def test_secret_fill_omits_password_from_saved_values_and_parameters(self):
        self.page.set_content("<input id='password' type='password' value='initial-password-secret'>")
        secret = "changed-password-secret"
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.fill_secret(self.page.locator("#password"), secret)

        evidence = self.read()
        self.assertEqual(secret, self.page.locator("#password").input_value())
        saved = (self.directory / "evidence.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(secret, saved)
        self.assertNotIn("initial-password-secret", saved)
        execution = next(iter(evidence.action_executions.values()))
        self.assertEqual("completed", execution["status"])
        self.assertNotIn("value", execution["action_execution"]["parameters"])

    def test_password_appearing_during_fill_wait_does_not_save_secret_parameter(self):
        self.page.set_content("""
            <div id='fields'></div>
            <script>
                setTimeout(() => {
                    document.getElementById('fields').innerHTML = "<input id='password' type='password'>";
                }, 1_000);
            </script>
            """)
        secret = "delayed-password-secret"
        options = CaptureOptions(computed_styles=(), screenshots=False)
        with PlaywrightEvidence.start(self.directory, self.page, options) as evidence:
            evidence.fill(self.page.locator("#password"), secret)

        self.assertEqual(secret, self.page.locator("#password").input_value())
        parameters = next(iter(self.read().action_executions.values()))["action_execution"]["parameters"]
        self.assertNotIn("value", parameters)
        self.assertIn("value", parameters["playwright.redacted_parameters"])
        self.assertNotIn(secret, (self.directory / "evidence.jsonl").read_text(encoding="utf-8"))

    def test_excluded_password_widget_still_redacts_fill_parameter(self):
        self.page.set_content("<input id='password' type='password'>")
        secret = "excluded-password-secret"
        options = CaptureOptions(max_widgets=1, excluded_attributes=("type",), computed_styles=(), screenshots=False)
        with PlaywrightEvidence.start(self.directory, self.page, options) as evidence:
            evidence.fill(self.page.locator("#password"), secret)

        parameters = next(iter(self.read().action_executions.values()))["action_execution"]["parameters"]
        self.assertNotIn("value", parameters)
        self.assertIn("value", parameters["playwright.redacted_parameters"])
        self.assertNotIn(secret, (self.directory / "evidence.jsonl").read_text(encoding="utf-8"))

    def test_excluded_widget_keeps_check_without_claiming_property_target(self):
        self.page.set_content("<p id='status'>Ready</p>")
        options = CaptureOptions(max_widgets=1, computed_styles=(), screenshots=False)
        with PlaywrightEvidence.start(self.directory, self.page, options) as evidence:
            evidence.checks.has_text("example:ready", "Ready", self.page.locator("#status"), "Ready")

        evidence = self.read()
        self.assertEqual("observation", evidence.oracle_results[0]["oracle_target"]["target_type"])
        self.assertTrue(evidence.oracle_results[0]["oracle_details"])
        observation = next(iter(evidence.observations.values()))
        self.assertEqual(1, len(observation["recorded_state"]["widgets"]))
        self.assertEqual([], observation["artifacts"])

    def test_existing_evidence_is_preserved_and_finish_leaves_page_owned_by_caller(self):
        self.page.set_content("<p>Ready</p>")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.observe()
            evidence.finish("completed")
            with self.assertRaises(RuntimeError):
                evidence.navigate("about:blank")
        self.assertFalse(self.page.is_closed())
        self.assertEqual("Ready", self.page.locator("p").inner_text())
        original = (self.directory / "evidence.jsonl").read_bytes()
        with self.assertRaises(EvidenceError) as raised:
            PlaywrightEvidence.start(self.directory, self.page)
        self.assertEqual("already_exists", raised.exception.code)
        self.assertEqual(original, (self.directory / "evidence.jsonl").read_bytes())

    def test_closed_page_capture_preserves_readable_prior_evidence(self):
        self.page.set_content("<p>Ready</p>")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.observe()
            self.page.close()
            with self.assertRaises(PlaywrightError):
                evidence.observe()

        evidence = self.read()
        self.assertEqual(1, len(evidence.observations))
        self.assertEqual({}, evidence.action_executions)
        self.assertEqual("finished", next(iter(evidence.test_runs.values()))["status"])

    def test_context_preserves_an_unhandled_script_failure(self):
        self.page.set_content("<p>Ready</p>")
        original = RuntimeError("Script setup failed")
        with self.assertRaises(RuntimeError) as raised:
            with PlaywrightEvidence.start(self.directory, self.page) as evidence:
                evidence.observe()
                raise original

        self.assertIs(original, raised.exception)
        self.assertFalse(self.page.is_closed())
        self.assertEqual("finished", next(iter(self.read().test_runs.values()))["status"])

    def test_direct_playwright_calls_do_not_invent_recorded_actions(self):
        self.page.set_content("<button onclick=\"this.textContent='Done'\">Continue</button>")
        with PlaywrightEvidence.start(self.directory, self.page) as evidence:
            evidence.observe()
            self.page.locator("button").click()
            evidence.observe()

        evidence = self.read()
        self.assertEqual(2, len(evidence.observations))
        self.assertEqual("Done", self.page.locator("button").inner_text())
        self.assertEqual({}, evidence.action_executions)
        self.assertEqual([], evidence.recorded_transitions)


if __name__ == "__main__":
    unittest.main()

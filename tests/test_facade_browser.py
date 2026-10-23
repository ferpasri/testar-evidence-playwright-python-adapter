"""Check automatic recording through the ordinary synchronous Playwright API."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest

from playwright.sync_api import (
    Locator, Page, Request, Response, TimeoutError as PlaywrightTimeoutError,
    sync_playwright as native_sync_playwright,
)
from testar_evidence import EvidenceReader
from testar_evidence_playwright import CaptureOptions, __version__, expect, sync_playwright


class FacadeFixture:
    def __enter__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                content = """<!doctype html><html><head><title>Local bank</title></head><body>
                    <main id="content"><h1>Customer Login</h1>
                        <input name="username"><input name="password" type="password">
                        <button id="login" onclick="showAccounts()">Log In</button>
                    </main>
                    <script>
                        function showAccounts() {
                            document.getElementById('content').innerHTML =
                                '<h1>Account Services</h1><button id="loan" onclick="showLoan()">Request Loan</button>';
                        }
                        function showLoan() {
                            document.getElementById('content').innerHTML =
                                '<h1>Apply for a Loan</h1><input id="amount"><input id="downPayment">' +
                                '<button id="apply" onclick="applyLoan()">Apply Now</button>';
                        }
                        function applyLoan() {
                            const denied = document.getElementById('amount').value === '999999' &&
                                document.getElementById('downPayment').value === '1';
                            document.getElementById('content').innerHTML =
                                '<p id="status">' + (denied ? 'Denied' : 'Approved') + '</p>';
                        }
                    </script></body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

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
class PlaywrightFacadeBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = FacadeFixture().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.__exit__(None, None, None)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "traces"
        self.manager = sync_playwright(evidence_path=self.directory, sut="local-bank",
            test_objective="browser-compatibility",
            capture_options=CaptureOptions(screenshots=False, computed_styles=()))
        self.playwright = self.manager.start()
        self.playwright_stopped = False
        self.addCleanup(self.stop_playwright)
        channel = os.environ.get("PLAYWRIGHT_CHANNEL", "chrome").strip()
        options = {"headless": True}
        if channel:
            options["channel"] = channel
        self.browser = self.playwright.chromium.launch(**options)
        self.addCleanup(self.close_browser)
        self.page = self.browser.new_page()
        self.page.set_default_timeout(1_000)

    def close_browser(self):
        if not self.playwright_stopped and self.browser.is_connected():
            self.browser.close()

    def stop_playwright(self):
        try:
            self.playwright.stop()
        finally:
            self.playwright_stopped = True

    def document(self, content):
        # Browser-side fixture setup is an arbitrary script, rather than a claimed user action.
        self.page.evaluate("html => { document.body.innerHTML = html; }", content)

    def read_models(self):
        return [EvidenceReader.read(manifest.parent)
                for manifest in sorted(self.directory.glob("*/manifest.json"))]

    def read_one(self):
        models = self.read_models()
        self.assertEqual(1, len(models))
        return models[0]

    def test_run_descriptions_are_optional_and_apply_to_each_page(self):
        self.stop_playwright()
        configurations = [
            {},
            {"origin": "  QA équipe / custom CI  ", "test_objective": "  Request loan — prêt  "},
            {"properties": {"evidence.origin": "Scenario script", "evidence.test_objective": "Inspect form"}},
        ]
        for index, configuration in enumerate(configurations):
            with self.subTest(configuration=configuration):
                parent = Path(self.temporary.name) / ("metadata-" + str(index))
                with sync_playwright(evidence_path=parent, capture_options=CaptureOptions(screenshots=False,
                        computed_styles=()), **configuration) as playwright:
                    channel = os.environ.get("PLAYWRIGHT_CHANNEL", "chrome").strip()
                    browser = playwright.chromium.launch(headless=True, channel=channel or None)
                    try:
                        for value in ("First page", "Second page"):
                            page = browser.new_page()
                            page.evaluate("document.body.innerHTML = '<input>'")
                            page.locator("input").fill(value)
                    finally:
                        browser.close()
                manifests = sorted(parent.glob("*/manifest.json"))
                self.assertEqual(2, len(manifests))
                for manifest in manifests:
                    model = EvidenceReader.read(manifest.parent)
                    properties = next(iter(model.runs.values()))["run"]["properties"]
                    self.assertEqual("testar-evidence-playwright-python-adapter", properties["evidence.adapter"])
                    self.assertEqual(__version__, properties["evidence.adapter_version"])
                    descriptions = configuration.get("properties", {
                        "evidence.origin": configuration.get("origin"),
                        "evidence.test_objective": configuration.get("test_objective"),
                    })
                    for key, expected in descriptions.items():
                        if expected is None:
                            self.assertNotIn(key, properties)
                        else:
                            self.assertEqual(expected, properties[key])
                    for record in model.executions.values():
                        self.assertNotIn("playwright.origin", record["execution"]["parameters"])

    def test_unmodified_loan_script_records_eight_actions_and_three_final_checks(self):
        self.page.goto(self.fixture.url)
        self.page.locator("[name=username]").fill("john")
        self.page.locator("[name=password]").fill("facade-password-secret")
        self.page.get_by_role("button", name="Log In").click()
        expect(self.page.locator("h1")).to_have_text("Account Services")
        self.page.get_by_role("button", name="Request Loan").click()
        expect(self.page.locator("h1")).to_have_text("Apply for a Loan")
        self.page.locator("#amount").fill("999999")
        self.page.locator("#downPayment").fill("1")
        self.page.get_by_role("button", name="Apply Now").click()
        expect(self.page.locator("#status")).to_have_text("Denied")
        self.page.close()

        model = self.read_one()
        self.assertEqual(8, len(model.executions))
        self.assertEqual(8, len(model.links()))
        self.assertEqual(3, len(model.checks))
        self.assertTrue(all(check["status"] == "passed" for check in model.checks))
        self.assertEqual({"kind": "value", "value": "Denied"}, model.checks[-1]["actual"])
        for path in self.directory.glob("*/evidence.jsonl"):
            self.assertNotIn("facade-password-secret", path.read_text(encoding="utf-8"))

    def test_navigation_and_control_methods_keep_native_return_values(self):
        response = self.page.goto(self.fixture.url)
        self.assertIsInstance(response, Response)
        self.assertEqual(200, response.status)
        self.document("<select id='choice'><option value='a'>A</option><option value='b'>B</option></select>"
                      "<input id='flag' type='checkbox'><input id='name'>")
        self.assertEqual(["b"], self.page.locator("#choice").select_option("b"))
        self.assertIsNone(self.page.locator("#flag").check())
        self.assertIsNone(self.page.locator("#flag").uncheck())
        self.assertIsNone(self.page.locator("#name").press("x"))
        self.assertEqual("x", self.page.locator("#name").input_value())
        self.page.close()

        self.assertEqual(5, len(self.read_one().executions))

    def test_chained_locators_preserve_native_types_and_record_the_final_action(self):
        self.document("<ul><li>First <button onclick=\"this.textContent='Done'\">Choose</button></li>"
                      "<li>Second <button>Choose</button></li></ul>")
        selected = self.page.locator("li").filter(has_text="First").get_by_role("button").first
        self.assertIsInstance(self.page, Page)
        self.assertIsInstance(selected, Locator)
        self.assertIsInstance(self.page.locator("li").nth(1), Locator)
        self.assertTrue(all(isinstance(item, Locator) for item in self.page.locator("li").all()))
        self.assertIs(self.page, self.page.main_frame.page)
        selected.click()
        self.assertEqual("Done", selected.inner_text())
        self.page.close()

        self.assertEqual(1, len(self.read_one().executions))

    def test_page_selector_shortcut_does_not_repeat_the_browser_action(self):
        self.document("<button id='count' onclick='window.count += 1'>Count</button>")
        self.page.evaluate("window.count = 0")
        self.page.click("#count")
        self.assertEqual(1, self.page.evaluate("window.count"))
        self.page.close()

        self.assertEqual(1, len(self.read_one().executions))

    def test_trial_click_keeps_actionability_check_without_claiming_an_execution(self):
        self.document("<button onclick='window.count += 1'>Count</button>")
        self.page.evaluate("window.count = 0")
        self.page.locator("button").click(trial=True)
        self.assertEqual(0, self.page.evaluate("window.count"))
        self.page.close()

        self.assertEqual({}, self.read_one().executions)

    def test_readings_do_not_become_executed_actions_or_oracle_results(self):
        self.document("<input value='Original'><p title='Tooltip'>Ready</p>")
        self.assertEqual("Original", self.page.locator("input").input_value())
        self.assertEqual("Ready", self.page.locator("p").inner_text())
        self.assertEqual("Tooltip", self.page.locator("p").get_attribute("title"))
        self.assertEqual(1, self.page.locator("p").count())
        self.assertTrue(self.page.locator("p").is_visible())
        self.assertTrue(self.page.screenshot().startswith(b"\x89PNG\r\n\x1a\n"))
        self.page.close()

        model = self.read_one()
        self.assertEqual({}, model.executions)
        self.assertEqual([], model.checks)

    def test_failed_action_preserves_playwright_timeout_and_saves_rejected_submission(self):
        self.document("<p>Ready</p>")
        with self.assertRaises(PlaywrightTimeoutError) as raised:
            self.page.locator("#missing").click(timeout=100)
        self.assertIn("missing", str(raised.exception))
        self.page.close()

        model = self.read_one()
        execution = next(iter(model.executions.values()))
        self.assertEqual(1, len(model.executions))
        self.assertEqual("rejected", execution["status"])
        self.assertIsNone(execution["result"])
        self.assertIn("missing", execution["error"])
        self.assertEqual([], model.links())

    def test_assertion_retry_records_one_final_result_with_its_final_reading(self):
        self.document("<p id='status'>Loading</p>")
        self.page.evaluate("setTimeout(() => document.querySelector('p').textContent = 'Ready', 250)")
        self.assertIsNone(expect(self.page.locator("#status")).to_have_text("Ready", timeout=2_000))
        self.page.close()

        checks = self.read_one().checks
        self.assertEqual(1, len(checks))
        self.assertEqual("passed", checks[0]["status"])
        self.assertEqual({"kind": "value", "value": "Ready"}, checks[0]["actual"])

    def test_failed_and_negated_assertions_keep_native_behavior(self):
        self.document("<p id='status'>Ready</p>")
        expect(self.page.locator("#status")).not_to_have_text("Loading", timeout=100)
        with self.assertRaises(AssertionError) as raised:
            expect(self.page.locator("#status"), "Displayed status").to_have_text("Missing", timeout=100)
        self.assertIn("Displayed status", str(raised.exception))
        self.page.close()

        checks = self.read_one().checks
        self.assertEqual(["passed", "failed"], [check["status"] for check in checks])
        self.assertEqual({"kind": "value", "value": "Ready"}, checks[1]["actual"])

    def test_page_and_widget_assertions_identify_the_checked_properties(self):
        self.document("<input id='name' value='John'><p id='status' title='Tooltip'>Ready</p>")
        self.page.evaluate("document.title = 'Fixture title'")
        expect(self.page).to_have_title("Fixture title")
        expect(self.page.locator("#name")).to_have_value("John")
        expect(self.page.locator("#status")).to_have_attribute("title", "Tooltip")
        self.page.close()

        checks = self.read_one().checks
        self.assertEqual(3, len(checks))
        self.assertEqual("state_property", checks[0]["target"]["kind"])
        self.assertEqual("widget_property", checks[1]["target"]["kind"])
        self.assertEqual("playwright.dom.value", checks[1]["target"]["property"])
        self.assertEqual("playwright.attribute.title", checks[2]["target"]["property"])
        self.assertEqual({"kind": "value", "value": "Fixture title"}, checks[0]["actual"])

    def test_context_close_records_the_final_state_before_its_page_is_destroyed(self):
        self.document("<input id='name' value='Before'>")
        self.page.locator("#name").fill("After")
        self.page.context.close()

        model = self.read_one()
        self.assertEqual(1, len(model.links()))
        self.assertTrue(all(run["status"] == "finished" for run in model.runs.values()))
        final_observation = list(model.observations.values())[-1]
        self.assertTrue(any(widget["properties"].get("playwright.dom.value") == "After"
                            for widget in final_observation["state"]["widgets"]))

    def test_stop_flushes_traces_when_the_script_has_not_closed_its_pages(self):
        self.document("<input id='name'>")
        self.page.locator("#name").fill("Final value")
        self.stop_playwright()

        model = self.read_one()
        self.assertEqual(1, len(model.links()))
        self.assertTrue(all(run["status"] == "finished" for run in model.runs.values()))

    def test_multiple_pages_have_separate_runs_and_execution_paths(self):
        self.document("<input id='name'>")
        self.page.locator("#name").fill("First page")
        second = self.browser.new_page()
        second.evaluate("document.body.innerHTML = '<input id=\"name\">'")
        second.locator("#name").fill("Second page")
        second.close()
        self.page.close()

        models = self.read_models()
        self.assertEqual(2, len(models))
        self.assertEqual([1, 1], sorted(len(model.executions) for model in models))
        identifiers = [next(iter(model.runs)) for model in models]
        self.assertEqual(2, len(set(identifiers)))

    def test_event_callbacks_receive_native_objects_and_listener_removal_still_works(self):
        requests = []

        def received(request):
            self.assertIsInstance(request, Request)
            self.assertIs(self.page, request.frame.page)
            requests.append(request)

        self.page.on("request", received)
        self.page.goto(self.fixture.url)
        self.page.remove_listener("request", received)
        self.page.goto(self.fixture.url + "second")
        self.assertEqual(1, len(requests))
        self.page.close()

        self.assertEqual(2, len(self.read_one().executions))

    def test_popup_context_manager_and_once_listener_return_recording_enabled_pages(self):
        popups = []
        self.page.once("popup", popups.append)
        with self.page.expect_popup() as event:
            self.page.evaluate("window.open('about:blank')")
        popup = event.value
        self.assertIsInstance(popup, Page)
        self.assertEqual([popup], popups)
        popup.evaluate("document.body.innerHTML = '<input id=\"name\">'")
        popup.locator("#name").fill("Popup value")
        popup.close()
        self.page.close()

        models = self.read_models()
        self.assertEqual(2, len(models))
        self.assertEqual([0, 1], sorted(len(model.executions) for model in models))

    def test_keyboard_and_mouse_calls_record_each_interaction_once(self):
        self.document("<input id='name'><button onclick='window.count += 1'>Count</button>")
        self.page.evaluate("window.count = 0; document.querySelector('input').focus()")
        self.page.keyboard.insert_text("typed")
        self.page.keyboard.press("End")
        box = self.page.locator("button").bounding_box()
        self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        self.assertEqual("typed", self.page.locator("input").input_value())
        self.assertEqual(1, self.page.evaluate("window.count"))
        self.page.close()

        self.assertEqual(3, len(self.read_one().executions))

    def test_standard_playwright_import_remains_unmodified(self):
        self.document("<input id='name'>")
        self.page.locator("#name").fill("Recorded")
        self.stop_playwright()
        trace_count = len(self.read_models())

        with native_sync_playwright() as native:
            channel = os.environ.get("PLAYWRIGHT_CHANNEL", "chrome").strip()
            options = {"headless": True}
            if channel:
                options["channel"] = channel
            browser = native.chromium.launch(**options)
            try:
                page = browser.new_page()
                self.assertIs(type(page), Page)
                page.set_content("<input id='name'>")
                page.locator("#name").fill("Not recorded")
            finally:
                browser.close()

        self.assertEqual(trace_count, len(self.read_models()))

    def test_import_compatible_namespace_and_context_manager_flush_the_trace(self):
        from testar_evidence_playwright.sync_api import Page as ExportedPage
        from testar_evidence_playwright.sync_api import expect as facade_expect
        from testar_evidence_playwright.sync_api import sync_playwright as facade_playwright

        self.stop_playwright()
        parent = Path(self.temporary.name) / "context-traces"
        with facade_playwright(evidence_path=parent, sut="fixture", test_objective="context-lifecycle",
                capture_options=CaptureOptions(screenshots=False, computed_styles=())) as playwright:
            channel = os.environ.get("PLAYWRIGHT_CHANNEL", "chrome").strip()
            options = {"headless": True}
            if channel:
                options["channel"] = channel
            browser = playwright.chromium.launch(**options)
            page = browser.new_page()
            self.assertIs(ExportedPage, Page)
            self.assertIsInstance(page, ExportedPage)
            page.evaluate("document.body.innerHTML = '<input id=\"name\">'")
            page.locator("#name").fill("Context value")
            facade_expect(page.locator("#name")).to_have_value("Context value")

        paths = list(parent.glob("*/manifest.json"))
        self.assertEqual(1, len(paths))
        model = EvidenceReader.read(paths[0].parent)
        self.assertEqual(1, len(model.executions))
        self.assertEqual("passed", model.checks[0]["status"])
        self.assertTrue(all(run["status"] == "finished" for run in model.runs.values()))


if __name__ == "__main__":
    unittest.main()

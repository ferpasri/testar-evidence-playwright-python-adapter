"""Capture selection, native property meanings, and browser target mapping."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import os
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from testar_evidence_playwright.capture import CaptureOptions, capture_page, identity, target_path


class CaptureOptionsTests(unittest.TestCase):
    def test_defaults_bound_the_saved_properties(self) -> None:
        options = CaptureOptions()
        self.assertEqual(2_000, options.max_widgets)
        self.assertEqual(8_192, options.max_string_length)
        self.assertEqual(4_194_304, options.max_property_characters)
        self.assertEqual(256, options.max_attributes)
        self.assertEqual(("display", "visibility", "font-size", "font-family", "color", "background-color"),
                         options.computed_styles)
        self.assertEqual((), options.excluded_attributes)
        self.assertTrue(options.screenshots)

    def test_capture_bounds_reject_invalid_values_and_boolean_numbers(self) -> None:
        limits = {"max_widgets": 5_000, "max_string_length": 65_536,
                  "max_property_characters": 8_388_608, "max_attributes": 1_024}
        for name, maximum in limits.items():
            for value in (0, -1, maximum + 1, True, 1.5, "1", None):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        CaptureOptions(**{name: value})

    def test_property_names_reject_ambiguous_and_invalid_selections(self) -> None:
        selections = ["color", None, ("color", "color"), ("font size",), ("",), ("bad\nname",),
                      ("bad\x7fname",), ("x" * 257,), (1,), (["color"],)]
        for name in ("computed_styles", "excluded_attributes"):
            for selection in selections:
                with self.subTest(name=name, selection=selection):
                    with self.assertRaises(ValueError):
                        CaptureOptions(**{name: selection})
        with self.assertRaises(ValueError):
            CaptureOptions(computed_styles=tuple(f"style-{index}" for index in range(65)))
        with self.assertRaises(ValueError):
            CaptureOptions(excluded_attributes=tuple(f"attribute-{index}" for index in range(257)))

    def test_caller_changes_cannot_change_the_capture_selection(self) -> None:
        styles = ["color"]
        attributes = ["data-private"]
        options = CaptureOptions(computed_styles=styles, excluded_attributes=attributes)
        styles.append("display")
        attributes.clear()
        self.assertEqual(("color",), options.computed_styles)
        self.assertEqual(("data-private",), options.excluded_attributes)
        with self.assertRaises(FrozenInstanceError):
            options.screenshots = False

    def test_screenshot_selection_is_a_boolean(self) -> None:
        for value in (0, 1, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CaptureOptions(screenshots=value)


class CaptureMappingTests(unittest.TestCase):
    def test_dom_paths_identify_widgets_and_their_captured_parents(self) -> None:
        result = {
            "nodes": [
                {"path": "#document", "parent": None, "properties": {"playwright.dom.nodeType": 9}},
                {"path": "#document/html[0]", "parent": "#document", "properties": {"ui.text": "Ready"}},
            ],
            "properties": {"ui.url": "https://example.test/", "ui.title": "Page"},
        }
        page = Mock()
        page.evaluate.return_value = result
        page.screenshot.return_value = b"example screenshot"
        with patch("testar_evidence_playwright.capture.time.time_ns", return_value=123_456_789):
            snapshot = capture_page(page, CaptureOptions())
        self.assertEqual(123_456, snapshot.timestamp_micros)
        self.assertEqual(result["properties"], snapshot.properties)
        self.assertEqual(b"example screenshot", snapshot.screenshot)
        self.assertIsNone(snapshot.widgets[0].parent)
        self.assertEqual(snapshot.widgets[0].id, snapshot.widgets[1].parent)
        self.assertEqual("playwright:widget:" + identity("#document/html[0]"), snapshot.widgets[1].id)
        self.assertIs(snapshot.widgets[1], snapshot.widget_at("#document/html[0]"))
        self.assertIsNone(snapshot.widget_at("missing"))
        self.assertIsNone(snapshot.widget_at(None))
        self.assertNotIn("playwright.dom.path", result["nodes"][1]["properties"])
        page.screenshot.assert_called_once_with(type="png")

    def test_options_reach_the_capture_script_and_screenshots_can_be_disabled(self) -> None:
        page = Mock()
        page.evaluate.return_value = {"nodes": [], "properties": {}}
        snapshot = capture_page(page, CaptureOptions(10, 20, 30, 40, ("display",), ("data-private",), False))
        self.assertIsNone(snapshot.screenshot)
        page.screenshot.assert_not_called()
        self.assertEqual({"maxWidgets": 10, "maxValueCharacters": 20, "maxTotalCharacters": 30,
                          "maxAttributesPerWidget": 40, "computedStyles": ["display"],
                          "excludedAttributes": ["data-private"]}, page.evaluate.call_args.args[1])

    def test_target_mapping_does_not_guess_for_missing_ambiguous_or_unavailable_widgets(self) -> None:
        self.assertIsNone(target_path(None))
        for count in (0, 2):
            locator = Mock()
            locator.count.return_value = count
            self.assertIsNone(target_path(locator))
            locator.evaluate.assert_not_called()
        locator = Mock()
        locator.count.side_effect = PlaywrightError("Page closed")
        self.assertIsNone(target_path(locator))
        locator = Mock()
        locator.count.return_value = 1
        locator.evaluate.side_effect = PlaywrightError("Detached")
        self.assertIsNone(target_path(locator))

    def test_available_target_path_and_unicode_identity_are_preserved(self) -> None:
        locator = Mock()
        locator.count.return_value = 1
        locator.evaluate.return_value = "#document/html[0]/body[1]/button[0]"
        self.assertEqual(locator.evaluate.return_value, target_path(locator))
        self.assertEqual("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", identity(""))
        self.assertNotEqual(identity("café"), identity("cafe"))


@unittest.skipUnless(os.environ.get("TESTAR_BROWSER_TESTS") == "1", "Set TESTAR_BROWSER_TESTS=1 to run browser checks")
class CaptureBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(
                headless=True, channel=os.environ.get("PLAYWRIGHT_CHANNEL", "chrome"))
        except BaseException:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_default_timeout(5_000)

    def tearDown(self) -> None:
        self.context.close()

    def test_raw_properties_aliases_and_known_exclusions_keep_their_meaning(self) -> None:
        self.page.set_content("""
            <title>Actual page title</title>
            <input id="name" value="original" title="Tooltip" aria-label="Accessible name"
                data-private="excluded" data-long="This string deliberately exceeds the configured value length limit">
            <img id="help" alt="" title="Image tooltip">
            <div id="plain" style="color: rgb(12, 34, 56)">  Original text  </div>
            """)
        self.page.locator("#name").fill("live")
        options = CaptureOptions(100, 32, 100_000, 100, ("display",), ("data-private",), True)
        snapshot = capture_page(self.page, options)
        properties = self.widget(snapshot, "name").properties
        self.assertEqual("original", properties["playwright.attribute.value"])
        self.assertEqual("live", properties["playwright.dom.value"])
        self.assertEqual("live", properties["ui.value"])
        self.assertEqual("Tooltip", properties["playwright.attribute.title"])
        self.assertEqual("Accessible name", properties["playwright.attribute.aria-label"])
        for name in ("ui.title", "ui.name", "ui.role", "playwright.attribute.data-private",
                     "playwright.attribute.data-long", "playwright.style.color"):
            self.assertNotIn(name, properties)
        omissions = properties["evidence.omitted_properties"]
        self.assertIn({"property": "playwright.attribute.data-private", "reason": "excluded"}, omissions)
        self.assertIn({"property": "playwright.attribute.data-long", "reason": "limit"}, omissions)
        self.assertIn("playwright.style.display", properties)
        self.assertEqual("playwright.dom.value", properties["evidence.property_aliases"]["ui.value"])
        self.assertEqual("", self.widget(snapshot, "help").properties["ui.alt"])
        self.assertEqual("  Original text  ", self.widget(snapshot, "plain").properties["ui.text"])
        self.assertEqual("Actual page title", snapshot.properties["ui.title"])
        self.assertEqual(snapshot.properties["ui.title"], snapshot.properties["playwright.document.title"])
        self.assertTrue(snapshot.properties["evidence.capture"]["truncated"])
        self.assertTrue(snapshot.screenshot.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_password_values_are_redacted_with_explicit_omission_reasons(self) -> None:
        self.page.set_content('<input id="password" type="password" value="initial-secret">')
        self.page.locator("#password").fill("changed-secret")
        snapshot = capture_page(self.page, CaptureOptions(screenshots=False))
        properties = self.widget(snapshot, "password").properties
        for name in ("playwright.attribute.value", "playwright.dom.value", "ui.value"):
            self.assertNotIn(name, properties)
        omissions = properties["evidence.omitted_properties"]
        self.assertIn({"property": "playwright.attribute.value", "reason": "redacted"}, omissions)
        self.assertIn({"property": "playwright.dom.value", "reason": "redacted"}, omissions)
        self.assertNotIn("initial-secret", str(snapshot.properties) + str(snapshot.widgets))
        self.assertNotIn("changed-secret", str(snapshot.properties) + str(snapshot.widgets))

    def test_open_shadow_widgets_have_parent_links_and_matching_target_paths(self) -> None:
        self.page.set_content('<div id="host"></div>')
        self.page.evaluate("""() => {
            document.querySelector('#host').attachShadow({mode: 'open'}).innerHTML = '<button id="shadow">Ready</button>';
        }""")
        snapshot = capture_page(self.page, CaptureOptions(screenshots=False))
        host = self.widget(snapshot, "host")
        shadow = self.widget(snapshot, "shadow")
        self.assertEqual(host.id, shadow.parent)
        self.assertIn("/#shadow-root/", shadow.properties["playwright.dom.path"])
        self.assertIs(shadow, snapshot.widget_at(target_path(self.page.locator("#shadow"))))

    def test_frame_contents_are_excluded_and_the_capture_description_explains_the_scope(self) -> None:
        self.page.set_content('<iframe id="frame" srcdoc="<button id=inside>Inside</button>"></iframe>')
        frame_button = self.page.frame_locator("#frame").locator("#inside")
        self.assertEqual(1, frame_button.count())
        snapshot = capture_page(self.page, CaptureOptions(screenshots=False))
        self.assertEqual(1, snapshot.properties["playwright.capture.omitted_frame_documents"])
        self.assertIn("frame documents are excluded", snapshot.properties["evidence.capture"]["area"])
        self.assertFalse(any(widget.properties.get("playwright.attribute.id") == "inside" for widget in snapshot.widgets))
        self.assertIsNone(target_path(frame_button))

    def test_widget_limits_keep_the_document_root_and_report_truncation(self) -> None:
        self.page.set_content('<button id="ready">Ready</button>')
        snapshot = capture_page(self.page, CaptureOptions(max_widgets=1, screenshots=False))
        self.assertEqual(1, len(snapshot.widgets))
        self.assertEqual("#document", snapshot.widgets[0].properties["playwright.dom.path"])
        self.assertTrue(snapshot.properties["evidence.capture"]["truncated"])
        self.assertIsNone(snapshot.widget_at(target_path(self.page.locator("#ready"))))
        self.assertIsNone(snapshot.screenshot)

    @staticmethod
    def widget(snapshot, identifier):
        return next(widget for widget in snapshot.widgets if widget.properties.get("playwright.attribute.id") == identifier)


if __name__ == "__main__":
    unittest.main()

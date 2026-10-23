"""Describe native operations without executing them or guessing application behavior."""

from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import re
import unittest

from playwright.sync_api import ElementHandle, Frame, Keyboard, Locator, Mouse, Page

from testar_evidence_playwright.facade_actions import describe_action, json_value


class _Locator(Locator):
    def __init__(self, selector="input"):
        self.selector = selector

    def __str__(self):
        return "Locator(" + self.selector + ")"

    def fill(self, value, *, timeout=None):
        raise AssertionError("Metadata creation must not execute the browser operation")

    def click(self, *, trial=None, timeout=None):
        raise AssertionError("Metadata creation must not execute the browser operation")


class _Page(Page):
    def __init__(self):
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return _Locator(selector)

    def goto(self, url, *, wait_until=None, timeout=None):
        raise AssertionError("Metadata creation must not execute the browser operation")

    def fill(self, selector, value, *, timeout=None):
        raise AssertionError("Metadata creation must not execute the browser operation")


class _Frame(Frame):
    def __init__(self):
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return _Locator(selector)

    def click(self, selector, *, timeout=None):
        raise AssertionError("Metadata creation must not execute the browser operation")


class _Keyboard(Keyboard):
    def __init__(self):
        pass

    def type(self, text, *, delay=None):
        raise AssertionError("Metadata creation must not execute the browser operation")


class _Mouse(Mouse):
    def __init__(self):
        pass

    def click(self, x, y, *, button=None):
        raise AssertionError("Metadata creation must not execute the browser operation")


class _ElementHandle(ElementHandle):
    def __init__(self):
        pass

    def fill(self, value, *, timeout=None):
        raise AssertionError("Metadata creation must not execute the browser operation")


class FacadeActionTests(unittest.TestCase):
    def setUp(self):
        self.page = _Page()

    def test_navigation_keeps_supplied_url_and_options_without_running_native_code(self):
        metadata = describe_action(self.page, self.page, "goto", self.page.goto,
            ("https://example.test/",), {"wait_until": "domcontentloaded"})
        self.assertEqual("navigate", metadata["kind"])
        self.assertEqual("Page.goto", metadata["name"])
        self.assertIsNone(metadata["locator"])
        self.assertEqual({"url": "https://example.test/", "wait_until": "domcontentloaded",
            "playwright.method": "Page.goto"}, metadata["parameters"])

    def test_locator_actions_retain_the_exact_locator_and_native_argument_names(self):
        locator = _Locator("#username")
        metadata = describe_action(locator, self.page, "fill", locator.fill, ("john",), {"timeout": 500})
        self.assertIs(locator, metadata["locator"])
        self.assertEqual("fill", metadata["kind"])
        self.assertEqual({"value": "john", "timeout": 500, "playwright.method": "Locator.fill"},
            metadata["parameters"])

    def test_page_selector_actions_resolve_the_selector_as_the_target(self):
        metadata = describe_action(self.page, self.page, "fill", self.page.fill,
            ("#username", "john"), {})
        self.assertEqual(["#username"], self.page.selectors)
        self.assertEqual("#username", metadata["locator"].selector)

    def test_frame_selector_actions_keep_the_frame_target(self):
        frame = _Frame()
        metadata = describe_action(frame, self.page, "click", frame.click, ("#submit",), {})
        self.assertEqual(["#submit"], frame.selectors)
        self.assertEqual("Frame.click", metadata["name"])
        self.assertEqual("#submit", metadata["locator"].selector)
        self.assertEqual([], self.page.selectors)

    def test_keyboard_text_uses_the_focused_widget_and_the_common_value_parameter(self):
        keyboard = _Keyboard()
        metadata = describe_action(keyboard, self.page, "type", keyboard.type,
            ("entered text",), {"delay": 2})
        self.assertEqual([":focus"], self.page.selectors)
        self.assertEqual("entered text", metadata["parameters"]["value"])
        self.assertNotIn("text", metadata["parameters"])
        self.assertEqual(2, metadata["parameters"]["delay"])

    def test_coordinate_actions_do_not_guess_a_widget_target(self):
        mouse = _Mouse()
        metadata = describe_action(mouse, self.page, "click", mouse.click, (10, 20), {})
        self.assertIsNone(metadata["locator"])
        self.assertEqual(10, metadata["parameters"]["x"])
        self.assertIn("screen coordinates", metadata["parameters"]["playwright.target_resolution"])

    def test_element_handles_report_the_unmapped_target_for_conservative_redaction(self):
        handle = _ElementHandle()
        metadata = describe_action(handle, self.page, "fill", handle.fill, ("a password",), {})
        self.assertIsNone(metadata["locator"])
        self.assertEqual("ElementHandle.fill", metadata["name"])
        self.assertIn("not mapped", metadata["parameters"]["playwright.target_resolution"])

    def test_trial_operations_are_not_classified_as_executed_actions(self):
        locator = _Locator()
        self.assertIsNone(describe_action(locator, self.page, "click", locator.click, (), {"trial": True}))
        self.assertIsNotNone(describe_action(locator, self.page, "click", locator.click, (), {"trial": False}))

    def test_reading_and_arbitrary_javascript_calls_are_not_classified_as_actions(self):
        for method in ("text_content", "count", "evaluate", "is_visible", "wait_for"):
            with self.subTest(method=method):
                self.assertIsNone(describe_action(_Locator(), self.page, method, lambda: None, (), {}))

    def test_invalid_native_arguments_are_left_for_the_native_call_to_reject(self):
        locator = _Locator()
        metadata = describe_action(locator, self.page, "fill", locator.fill,
            ("first", "unexpected"), {"unknown_option": True})
        self.assertEqual(["first", "unexpected"], metadata["parameters"]["playwright.positional_arguments"])
        self.assertEqual({"unknown_option": True}, metadata["parameters"]["playwright.keyword_arguments"])

    def test_binary_arguments_save_size_and_digest_instead_of_content(self):
        payload = b"private file contents"
        self.assertEqual({"type": "bytes", "size": len(payload), "sha256": sha256(payload).hexdigest()},
            json_value(payload))

    def test_supported_non_json_arguments_have_stable_descriptions(self):
        result = json_value({"path": Path("upload.txt"), "duration": timedelta(seconds=2),
            "pattern": re.compile("ready", re.IGNORECASE), "locator": _Locator("#ready")})
        self.assertEqual("upload.txt", result["path"])
        self.assertEqual({"type": "duration", "milliseconds": 2000}, result["duration"])
        self.assertEqual("ready", result["pattern"]["pattern"])
        self.assertEqual("Locator(#ready)", result["locator"]["locator"])

    def test_recursive_arguments_and_nonfinite_numbers_remain_valid_json_data(self):
        values = []
        values.append(values)
        self.assertEqual([{"type": "circular_reference"}], json_value(values))
        for value in (float("inf"), float("nan")):
            self.assertEqual({"type": "non_finite_number"}, json_value(value))

    def test_unsupported_arguments_are_not_inspected_or_formatted(self):
        class Opaque:
            def __repr__(self):
                raise AssertionError("Do not inspect arbitrary application objects")

        result = json_value(Opaque())
        self.assertEqual("unsupported_argument", result["type"])
        self.assertTrue(result["class"].endswith("Opaque"))


if __name__ == "__main__":
    unittest.main()

"""Describe native browser actions without executing or changing their arguments."""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import inspect
import math
from pathlib import Path
import re
from typing import Any, Callable

from playwright.sync_api import ElementHandle, Frame, Keyboard, Locator, Mouse, Page, Touchscreen


_ELEMENT_ACTIONS = {
    "blur", "check", "clear", "click", "dblclick", "dispatch_event", "drag_to", "drop",
    "fill", "focus", "hover", "press", "press_sequentially", "scroll_into_view_if_needed",
    "select_option", "select_text", "set_checked", "set_input_files", "tap", "type", "uncheck",
}
_PAGE_ACTIONS = _ELEMENT_ACTIONS | {
    "drag_and_drop", "emulate_media", "go_back", "go_forward", "goto", "reload", "set_content",
    "set_viewport_size",
}
_FRAME_ACTIONS = _ELEMENT_ACTIONS | {"drag_and_drop", "goto", "set_content"}
_KEYBOARD_ACTIONS = {"down", "insert_text", "press", "type", "up"}
_MOUSE_ACTIONS = {"click", "dblclick", "down", "move", "up", "wheel"}
_TEXT_ACTIONS = {"insert_text", "press_sequentially", "type"}


def describe_action(subject: Any, page: Page, method_name: str, bound_method: Callable,
                    args: tuple, kwargs: dict) -> dict | None:
    """Return reporting data for a supported operation; leave native execution to the caller."""
    api_name = _action_api(subject, method_name)
    if api_name is None:
        return None
    try:
        supplied = dict(inspect.signature(bound_method).bind_partial(*args, **kwargs).arguments)
    except (TypeError, ValueError):
        supplied = {"playwright.positional_arguments": list(args), "playwright.keyword_arguments": kwargs}
    if supplied.get("trial") is True:
        return None
    parameters = json_value(supplied)
    parameters["playwright.method"] = api_name + "." + method_name
    if method_name in _TEXT_ACTIONS and "text" in parameters:
        parameters["value"] = parameters.pop("text")
    if method_name == "clear":
        parameters["value"] = ""
    locator = _action_locator(subject, page, supplied)
    if isinstance(subject, ElementHandle):
        parameters["playwright.target_resolution"] = "The element handle is not mapped to a captured widget."
    elif isinstance(subject, (Mouse, Touchscreen)):
        parameters["playwright.target_resolution"] = "The operation identifies screen coordinates rather than a widget."
    kind = "navigate" if method_name == "goto" else method_name
    return {"kind": kind, "name": api_name + "." + method_name,
        "locator": locator, "parameters": parameters}


def json_value(value: Any) -> Any:
    """Represent supplied arguments as JSON data without inspecting unsupported objects."""
    return _json_value(value, set(), 0)


def _action_api(subject: Any, method_name: str) -> str | None:
    for api_type, methods in (
        (Page, _PAGE_ACTIONS),
        (Frame, _FRAME_ACTIONS),
        (Locator, _ELEMENT_ACTIONS),
        (ElementHandle, _ELEMENT_ACTIONS),
        (Keyboard, _KEYBOARD_ACTIONS),
        (Mouse, _MOUSE_ACTIONS),
        (Touchscreen, {"tap"}),
    ):
        if isinstance(subject, api_type):
            return api_type.__name__ if method_name in methods else None
    return None


def _action_locator(subject: Any, page: Page, supplied: dict) -> Locator | None:
    if isinstance(subject, Locator):
        return subject
    if isinstance(subject, Keyboard):
        try:
            return page.locator(":focus")
        except Exception:
            return None
    if isinstance(subject, (Page, Frame)):
        selector = supplied.get("selector", supplied.get("source"))
        if isinstance(selector, str):
            try:
                return subject.locator(selector)
            except Exception:
                return None
    return None


def _json_value(value: Any, visited: set[int], depth: int) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"type": "non_finite_number"}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, re.Pattern):
        return {"type": "regular_expression", "pattern": _json_value(value.pattern, visited, depth + 1),
            "flags": value.flags}
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        return {"type": "bytes", "size": len(payload), "sha256": sha256(payload).hexdigest()}
    if isinstance(value, timedelta):
        return {"type": "duration", "milliseconds": value.total_seconds() * 1_000}
    api_name = next((base.__name__ for base in type(value).__mro__
        if base.__module__ == "playwright.sync_api._generated"), None)
    if api_name is not None:
        result = {"type": "playwright." + api_name}
        if isinstance(value, Locator):
            try:
                result["locator"] = str(value)
            except Exception:
                pass
        return result
    if isinstance(value, (dict, list, tuple)):
        if depth >= 32:
            return {"type": "maximum_argument_depth"}
        if id(value) in visited:
            return {"type": "circular_reference"}
        visited.add(id(value))
        try:
            if isinstance(value, dict):
                if all(isinstance(key, str) for key in value):
                    return {key: _json_value(item, visited, depth + 1) for key, item in value.items()}
                return {"type": "mapping", "entries": [
                    [_json_value(key, visited, depth + 1), _json_value(item, visited, depth + 1)]
                    for key, item in value.items()]}
            return [_json_value(item, visited, depth + 1) for item in value]
        finally:
            visited.remove(id(value))
    return {"type": "unsupported_argument", "class": type(value).__module__ + "." + type(value).__qualname__}

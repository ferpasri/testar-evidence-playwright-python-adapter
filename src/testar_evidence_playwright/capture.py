"""Capture the concrete DOM information available to a Playwright page."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
import time
from typing import Any, TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError
from testar_evidence import Widget

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


@dataclass(frozen=True)
class CaptureOptions:
    """Limits and property selection applied to each captured observation."""

    max_widgets: int = 2_000
    max_string_length: int = 8_192
    max_property_characters: int = 4_194_304
    max_attributes: int = 256
    computed_styles: tuple[str, ...] = (
        "display", "visibility", "font-size", "font-family", "color", "background-color"
    )
    excluded_attributes: tuple[str, ...] = ()
    screenshots: bool = True

    def __post_init__(self) -> None:
        _require_bound(self.max_widgets, 5_000, "max_widgets")
        _require_bound(self.max_string_length, 65_536, "max_string_length")
        _require_bound(self.max_property_characters, 8_388_608, "max_property_characters")
        _require_bound(self.max_attributes, 1_024, "max_attributes")
        object.__setattr__(self, "computed_styles", _names(self.computed_styles, 64, "computed_styles"))
        object.__setattr__(self, "excluded_attributes", _names(self.excluded_attributes, 256, "excluded_attributes"))
        if not isinstance(self.screenshots, bool):
            raise ValueError("screenshots must be a boolean")


def _require_bound(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")


def _names(values: tuple[str, ...], maximum: int, name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must contain property names")
    names = tuple(values)
    if len(names) > maximum:
        raise ValueError(f"{name} has too many entries")
    for value in names:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError(f"{name} entries must be names of at most 256 characters")
        if any(character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
            raise ValueError(f"{name} entries must contain no whitespace or control characters")
    if len(set(names)) != len(names):
        raise ValueError(f"{name} has repeated entries")
    return names


@dataclass(frozen=True)
class BrowserSnapshot:
    timestamp_micros: int
    properties: dict[str, Any]
    widgets: list[Widget]
    screenshot: bytes | None

    def widget_at(self, path: str | None) -> Widget | None:
        if path is None:
            return None
        return next((widget for widget in self.widgets
                     if widget.properties.get("playwright.dom.path") == path), None)


def capture_page(page: Page, options: CaptureOptions) -> BrowserSnapshot:
    result = page.evaluate(_resource("capture.js"), {
        "maxWidgets": options.max_widgets,
        "maxValueCharacters": options.max_string_length,
        "maxTotalCharacters": options.max_property_characters,
        "maxAttributesPerWidget": options.max_attributes,
        "computedStyles": list(options.computed_styles),
        "excludedAttributes": list(options.excluded_attributes),
    })
    captured_at = time.time_ns() // 1_000
    widgets = []
    for node in result["nodes"]:
        path = node["path"]
        parent = node["parent"]
        properties = dict(node["properties"])
        properties["playwright.dom.path"] = path
        widgets.append(Widget(_widget_id(path), _widget_id(parent) if parent is not None else None, properties))
    screenshot = page.screenshot(type="png") if options.screenshots else None
    return BrowserSnapshot(captured_at, dict(result["properties"]), widgets, screenshot)


def target_path(locator: Locator | None) -> str | None:
    if locator is None:
        return None
    try:
        if locator.count() != 1:
            return None
        return locator.evaluate(_resource("target.js"), timeout=1_000)
    except PlaywrightError:
        return None


def identity(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _widget_id(path: str) -> str:
    return "playwright:widget:" + identity(path)


@lru_cache(maxsize=2)
def _resource(name: str) -> str:
    return files("testar_evidence_playwright").joinpath("resources", name).read_text(encoding="utf-8")

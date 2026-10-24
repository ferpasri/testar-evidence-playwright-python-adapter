"""Capture recorded GUI evidence from synchronous Playwright tests."""

from ._metadata import VERSION
from .capture import CaptureOptions
from .checks import PlaywrightChecks
from .evidence import AvailableAction, PlaywrightEvidence
from .facade import evidence_for, sync_playwright
from .facade_assertions import expect

__version__ = VERSION
__all__ = ["AvailableAction", "CaptureOptions", "PlaywrightChecks", "PlaywrightEvidence", "evidence_for", "expect", "sync_playwright"]

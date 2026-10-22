"""Record synchronous Playwright scripts as portable execution evidence."""

from .capture import CaptureOptions
from .checks import PlaywrightChecks
from .evidence import PlaywrightEvidence

__version__ = "0.2.1"

__all__ = ["CaptureOptions", "PlaywrightChecks", "PlaywrightEvidence", "__version__"]

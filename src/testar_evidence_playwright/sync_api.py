"""The synchronous Playwright API with automatic GUI evidence recording."""

from playwright import sync_api as _native

from .facade import evidence_for, sync_playwright
from .facade_assertions import expect

__all__ = list(_native.__all__) + ["evidence_for"]


def __getattr__(name):
    return getattr(_native, name)


def __dir__():
    return sorted(set(globals()) | set(__all__))

"""Resolve simple caller-provided CSS targets together through native Playwright."""

import re

from playwright.sync_api import Error as PlaywrightError

from .capture import _resource


# Only single-element selectors whose matches() semantics equal Playwright CSS.
# More complex selectors retain their native individual resolution.
_SIMPLE_CSS = re.compile(
    r'(?:[a-zA-Z][a-zA-Z0-9_-]*)?(?:#[a-zA-Z_][a-zA-Z0-9_-]*|'
    r'\[[a-zA-Z_][a-zA-Z0-9_-]*=(?:"[^"\\\r\n]*"|\'[^\'\\\r\n]*\')\])'
)


def remember_target(page, name, arguments, options, result):
    if name != "locator":
        return
    selector = arguments[0] if arguments else options.get("selector")
    if any(value is not None for key, value in options.items() if key != "selector"):
        return
    if isinstance(selector, str) and _SIMPLE_CSS.fullmatch(selector):
        result._evidence_css_target = (page, selector)


def _resolve_batch(page, selectors):
    script = "(elements, selectors) => { const pathFor = (" + _resource("target.js") + "); "
    script += "return selectors.map(selector => pathFor(elements.filter(element => element.matches(selector)))); }"
    return page.locator(", ".join(selectors)).evaluate_all(script, selectors)


def target_paths(page, locators, *, fallback):
    paths = [None] * len(locators)
    batches = {}
    remaining = []
    for index, locator in enumerate(locators):
        if locator is None:
            continue
        target = getattr(locator, "_evidence_css_target", None)
        if isinstance(target, tuple) and len(target) == 2 and target[0] is page:
            batches.setdefault(target[1], []).append(index)
        else:
            remaining.append(index)
    selectors = list(batches)
    # Bound selector size and processing per request on pages with many targets.
    for offset in range(0, len(selectors), 128):
        chunk = selectors[offset:offset + 128]
        try:
            resolved = _resolve_batch(page, chunk)
        except PlaywrightError:
            remaining.extend(index for selector in chunk for index in batches[selector])
            continue
        for selector, path in zip(chunk, resolved):
            for index in batches[selector]:
                paths[index] = path
    for index in remaining:
        paths[index] = fallback(locators[index])
    return paths

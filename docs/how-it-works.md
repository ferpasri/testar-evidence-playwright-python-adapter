# How the Playwright Python adapter works

A script describes its test. Playwright operates the browser. The adapter records supported operations, captured pages, and final test-oracle results. The generic evidence library writes the portable files.

```text
Your Python script
    -> synchronous Playwright facade
    -> PlaywrightEvidence: action and recording boundaries
    -> PlaywrightChecks: assertions and their results
    -> capture_page + JavaScript: properties and screenshots
    -> testar-evidence-python: validation and portable files
```

## The components

| Component | What it does |
| --- | --- |
| [`sync_api.py`](../src/testar_evidence_playwright/sync_api.py) | Provides `sync_playwright`, `expect`, and `evidence_for` alongside native Playwright API types. |
| [`facade.py`](../src/testar_evidence_playwright/facade.py) | Connects native browser objects to their page's recorder, delegates operations, and finishes traces as pages and browsers close. |
| [`facade_actions.py`](../src/testar_evidence_playwright/facade_actions.py) | Describes supported browser actions and their supplied parameters. |
| [`facade_assertions.py`](../src/testar_evidence_playwright/facade_assertions.py) | Delegates Playwright assertions and identifies their recorded targets, expected values, and readings. |
| [`evidence.py`](../src/testar_evidence_playwright/evidence.py): `PlaywrightEvidence` | Starts a run, records action boundaries, connects observations to executions, and finishes the run. It also exposes the explicit recording API. |
| [`checks.py`](../src/testar_evidence_playwright/checks.py): `PlaywrightChecks` | Runs wrapped test oracles and records their expected value, final reading, status, and target. |
| [`capture.py`](../src/testar_evidence_playwright/capture.py): `CaptureOptions` | Configures collection limits, selected computed styles, excluded attributes, and screenshots. |
| `capture_page` and `BrowserSnapshot` in `capture.py` | Read the page and hold its captured properties, widget hierarchy, timestamp, and screenshot. |
| [`capture.js`](../src/testar_evidence_playwright/resources/capture.js) | Runs in the page to read document and element properties, capture descriptions, and known omissions. |
| [`target.js`](../src/testar_evidence_playwright/resources/target.js) | Finds a locator's structural DOM path so it can be matched to a captured widget. |

## The minimal script integration

Change the Playwright imports and configure recording when starting Playwright:

```python
from testar_evidence_playwright.sync_api import sync_playwright, expect

with sync_playwright(
    evidence_path="target/traces",
    sut="parabank",
    test_objective="denied-loan",
) as playwright:
    browser = playwright.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page()
    page.goto("https://para.testar.org/")
    page.locator("input[name='username']").fill("john")
    browser.close()
```

The adapter delegates operations to the real Playwright library. A page remains a native Playwright `Page`; locators remain native `Locator` objects. The adapter follows objects returned by calls, chained locators, and event callbacks. It keeps a separate trace for each page.

Use the adapter's `expect` for automatic recording of page and locator assertions. Native Playwright still decides whether an assertion passes and how to retry it. Readings such as `page.title()` and `locator.count()` do not themselves record test-oracle results.

## Identifying the run

Optional `origin` and `test_objective` arguments describe the caller and its test intent. They are saved once on the run using `evidence.origin` and `evidence.test_objective`. The adapter records its identifier and version automatically. When a description is omitted, its property stays absent; recording still works. A generic filename label only helps name the directory. See [run metadata](SPEC.md#run-metadata).

## Following one click and assertion

Consider these ordinary Playwright calls:

```python
page.get_by_role("button", name="Apply Now", exact=True).click()
expect(page.locator("#loanStatus")).to_have_text("Denied")
```

1. Before the click, the recorder uses the latest pending capture or captures the page. `capture.js` collects properties; `capture_page` converts them into generic widgets and optionally takes a screenshot.
2. `target.js` identifies the button's DOM path. The adapter finds the corresponding captured widget when possible.
3. The adapter records the source observation with the chosen action as its candidate, records selection, and delegates the click. It records accepted submission on return, or rejected submission with the operation's error on exception.
4. A resulting capture completes the accepted action's state-action-state link.
5. Playwright runs the assertion, including retries. The adapter records one final outcome, reads the mapped text property, and captures the page for that evaluation.
6. At the next action or trace ending, the adapter writes the pending observation and evaluation.

Denied is the expected business result, so the evaluation passes when Denied appears. Acceptance of the click describes the click operation; the assertion checks the business response.

## Why a capture can remain pending

The latest capture briefly stays in memory until the next action is known. That action can then be attached as a candidate to the same observation. A result observation can also be the next action's source, forming a state-action-state trace.

This pending capture is unrelated to an evaluation whose status is pending. Automatically recorded assertions report their final outcomes.

Repeated assertions can produce separate observations without action links between them. The adapter preserves those visits rather than inventing actions. `observe()` requests another captured visit when using the explicit API.

## When an integration needs more information

The facade sees supported Playwright operations and assertions. An exploratory tool may also know which alternative actions it discovered, why it selected one, or which custom oracle callback it ran. The tool supplies those details through explicit integration.

For a facade page, use `evidence_for(page)` to access its existing recorder. The following custom test oracle is added to the same automatic trace:

```python
from testar_evidence import CheckReading, CheckRule
from testar_evidence_playwright.sync_api import evidence_for

status = page.locator("#loanStatus")


def assert_denied():
    assert status.inner_text() == "Denied"


evidence_for(page).checks.evaluate(
    "The loan request is denied",
    rule=CheckRule("example:loan-denied"),
    locator=status,
    property="playwright.dom.innerText",
    expected="Denied",
    assertion=assert_denied,
    reading=lambda: CheckReading.value(status.inner_text()),
)
```

`checks.record_result(...)` reports a check already run by the caller, without rerunning its callback. That result targets the captured observation and has an unknown actual reading. `perform(...)` wraps a custom operation on the same recorder.

An integration using native Playwright without the facade starts its own recorder with `PlaywrightEvidence.start_trace(...)`. It reports `capture()`, `select_action(...)`, and `submit_action(...)` around existing execution calls. The optional [explicit-recording example](../examples/parabank_denied_loan.py) demonstrates this approach.

`checks.evaluate(...)` runs the supplied assertion once; that assertion owns its retry behavior. It records a rule, locator or state property, expectation, and actual reading. `perform(...)` treats a callback containing a whole routine as one outer action. Browser interactions executed inside a callback while another action is being recorded belong to that outer action. Use explicit integration when the evidence needs finer boundaries.

Soft assertions use their assertion framework's result collection and require explicit integration. API response assertions also delegate to native Playwright and require an appropriate explicit result target.

## Understanding the captured data

The capture description tells consumers what area, widgets, and properties were collected and which known limitations apply. Source properties retain their concrete names and values. Shared aliases such as `ui.url`, `ui.title`, and `ui.text` identify declared equal readings within the same record.

Captured password values are redacted. Supported password-input operations and password-value assertions also omit secret values from their recorded metadata. Custom callbacks supply their own recorded parameters, expectations, and readings. Screenshots and other application data follow the chosen capture settings.

Capture reads, screenshots, locator resolution, and assertion readings are successive browser operations. The application can change between them. The script controls application readiness and decides when another observation is useful.

A widget's structural path identifies its place within one capture. Comparing different visits or tools requires explicit matching rules in consumers. The shared traces retain the original evidence for those decisions.

See [SPEC](SPEC.md) for recording rules and [the README](../README.md) for installation and commands.

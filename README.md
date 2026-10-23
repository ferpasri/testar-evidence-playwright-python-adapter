# Playwright Python evidence adapter

A Python library that runs synchronous Playwright operations and records their execution evidence in the shared [TESTAR evidence format](https://github.com/ferpasri/testar-evidence-specs).

```text
Python script -> Playwright Python adapter -> testar-evidence-python -> portable evidence
```

The facade delegates browser operations to Playwright and records supported actions and Playwright assertions automatically. Explicit recording methods support integrations that need more control. Package version `0.2.2` follows the shared `0.2` release series.

## Install

Use Python 3.11 or later. From this repository, install the evidence library and this adapter as editable dependencies:

```powershell
python -m pip install -e ../testar-evidence-python
python -m pip install -e .
```

An editable installation imports the package from its source directory, so local changes are available without reinstalling it. Playwright is installed as a dependency. The example uses an installed Google Chrome by default.

## Record with the facade

Import `sync_playwright` and `expect` from the adapter, configure the output once, and use normal Playwright calls:

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
    page.locator("input[name='password']").fill("demo")
    page.get_by_role("button", name="Log In", exact=True).click()
    expect(page.get_by_text("Account Services", exact=True)).to_be_visible()
    browser.close()
```

The adapter retains native Playwright `Page` and `Locator` objects, arguments, return values, and exceptions. It records supported navigation, input, click, keyboard, and mouse operations. Read-only calls such as `locator.count()` remain readings. The adapter's `expect` records the final outcome of page and locator assertions while Playwright handles their retries.

Each page gets its own timestamped trace below `evidence_path`. The SUT and test objective labels form part of its directory name. Closing the page, browser, or Playwright session finishes its trace. A directory contains `manifest.json`, `evidence.jsonl`, and screenshots in `artifacts/` when enabled. Load it in the dashboard.

Captured password values and supported password-input arguments are redacted. The caller remains responsible for other sensitive application data collected by DOM capture, screenshots, or custom recording callbacks.

## Run descriptions

`sync_playwright()`, `PlaywrightEvidence.start(...)`, and `start_trace(...)` accept optional `origin` and `test_objective` descriptions. For example, `origin="Loan scenario script"` names the caller and `test_objective="Verify that an oversized loan request is denied"` describes intent. Omitting them still records evidence. The adapter supplies its own name and version automatically. See [run metadata](docs/SPEC.md#run-metadata).

## Record additional evidence

For a facade page, `evidence_for(page)` returns its existing `PlaywrightEvidence` recorder. Import it from `testar_evidence_playwright.sync_api` or `testar_evidence_playwright` to add custom operations and test-oracle results to the same automatic trace.

`evidence_for(page).checks.evaluate(...)` wraps an ordinary Python assertion or custom oracle callback. `checks.record_result(...)` reports an outcome already obtained by the caller, and `perform(...)` wraps a custom operation. See [How it works](docs/how-it-works.md) for an example.

An integration using native Playwright without the facade can create its own recorder with `PlaywrightEvidence.start_trace(...)`. It reports `capture()`, `select_action(...)`, and `submit_action(...)` around its execution calls. The caller supplies extra information such as discovered alternatives or reasons for action selection.

The facade supports synchronous Playwright. Soft assertions and API response assertions keep their native behavior; recording their results requires explicit integration.

## Loan-denial example

[parabank_facade.py](examples/parabank_facade.py) logs in as `john` / `demo` through the facade, checks Account Services, opens Request Loan, checks Apply for a Loan, enters amount `999999` and down payment `1`, submits, and checks Denied.

```powershell
python examples/parabank_facade.py
```

Add `--headed` to show the browser. Use `--url` for another starting address and `--channel` for another Playwright browser channel. Use `--output` for the parent directory containing timestamped page traces. The example prints that parent directory. Denied is the expected business result. The hosted application's data and behavior determine whether the example succeeds.

The optional [explicit-recording example](examples/parabank_denied_loan.py) follows the same scenario using `PlaywrightEvidence` helpers. It shows the additional control available for deeper integrations.

## Tests

Run the unit tests:

```powershell
python -m unittest discover -s tests -v
```

Enable browser integration tests against a controlled local fixture with installed Chrome:

```powershell
$env:TESTAR_BROWSER_TESTS = "1"
$env:PLAYWRIGHT_CHANNEL = "chrome"
python -m unittest discover -s tests -v
```

In Bash, use `TESTAR_BROWSER_TESTS=1 PLAYWRIGHT_CHANNEL=chrome python -m unittest discover -s tests -v`. To use Playwright's bundled Chromium, install it with `python -m playwright install chromium` and set `PLAYWRIGHT_CHANNEL` to an empty string. Browser tests use their local fixture rather than the public application.

See [How it works](docs/how-it-works.md) for the components and integration flow, [SPEC](docs/SPEC.md) for recording behavior, and [TODO](TODO.md) for planned work.

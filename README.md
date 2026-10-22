# Playwright Python evidence adapter

A Python library that records scripted Playwright actions, captured pages, and test-oracle results in the shared [TESTAR evidence format](https://github.com/ferpasri/testar-evidence-specs).

```text
Python script -> Playwright Python adapter -> testar-evidence-python -> portable evidence
```

The adapter imports `testar-evidence` as a library. Its public API is in `testar_evidence_playwright`. Package version `0.2.1` follows the shared `0.2` release series.

## Install

Use Python 3.11 or later. From this repository, install the evidence library and this adapter as editable dependencies:

```powershell
python -m pip install -e ../testar-evidence-python
python -m pip install -e .
```

An editable installation imports the package from its source directory, so local changes are available without reinstalling it. Playwright is installed as a dependency. The example uses an installed Google Chrome by default.

## Record a script

The script owns its Playwright browser and page. Start an evidence session with that page and an output directory:

```python
from testar_evidence_playwright import PlaywrightEvidence

with PlaywrightEvidence.start(output, page) as evidence:
    evidence.navigate(url)
    evidence.fill(page.locator("input[name='username']"), "john")
    evidence.fill_secret(page.locator("input[name='password']"), "demo")
    evidence.click(page.get_by_role("button", name="Log In", exact=True))
    evidence.checks.has_text(
        "example:welcome", "Welcome heading", page.locator("h1"), "Welcome"
    )
    evidence.finish("completed")
```

The helpers execute the requested operation and record its evidence. `perform(...)` wraps other actions, and `checks.evaluate(...)` wraps custom test oracles. Direct Playwright calls remain under the script's control; use the helpers for operations that should appear in the trace.

`fill_secret` omits the supplied value from action parameters. Captured password input values are also redacted. The script author chooses the data supplied to custom actions and test oracles.

## Loan-denial example

[parabank_denied_loan.py](examples/parabank_denied_loan.py) logs in as `john` / `demo`, checks Account Services, opens Request Loan, checks Apply for a Loan, enters amount `999999` and down payment `1`, submits, and checks Denied.

```powershell
python examples/parabank_denied_loan.py
```

Add `--headed` to show the browser. Use `--url` for another starting address, `--channel` for another Playwright browser channel, and `--output` for an exact evidence directory:

```powershell
python examples/parabank_denied_loan.py --headed --output target/my-loan-evidence
```

By default, each run reserves a directory such as `target/traces/parabank_denied-loan_trace-2026-10-22_15-30-12-123/`. The application label is `parabank`; the test objective label is `denied-loan`. A local timestamp includes milliseconds, and a collision adds a numeric suffix. An explicit output path remains exactly as supplied; existing evidence is preserved by refusing to overwrite its manifest or event log.

Load the printed directory in the dashboard. It contains `manifest.json`, `evidence.jsonl`, and screenshot files in `artifacts/`. A successful scenario records eight executions and three passed evaluations. Denied is the expected business result. The hosted application's data and behavior determine whether the example succeeds.

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

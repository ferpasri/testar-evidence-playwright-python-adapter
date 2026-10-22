# How the Playwright Python adapter works

A script describes its test. Playwright operates the browser. The adapter records those operations, the captured page, and the final results of the script's test oracles. The generic evidence library writes the portable files.

```text
Your Python script
    -> PlaywrightEvidence: action and recording boundaries
    -> PlaywrightChecks: assertions and their results
    -> capture_page + JavaScript: captured properties and screenshots
    -> testar-evidence-python: validation and portable files
```

## The components

| Component | What it does |
| --- | --- |
| [`evidence.py`](../src/testar_evidence_playwright/evidence.py): `PlaywrightEvidence` | Starts a run, wraps actions, connects observations to executions, and finishes the run. This is the script's entry point. |
| [`checks.py`](../src/testar_evidence_playwright/checks.py): `PlaywrightChecks` | Runs wrapped test oracles and records their expected value, final reading, status, and target. |
| [`capture.py`](../src/testar_evidence_playwright/capture.py): `CaptureOptions` | Configures collection bounds, selected computed styles, excluded attributes, and screenshots. |
| `capture_page` and `BrowserSnapshot` in `capture.py` | Read the page and hold its captured properties, widget hierarchy, timestamp, and screenshot. |
| [`capture.js`](../src/testar_evidence_playwright/resources/capture.js) | Runs in the page to read document and element properties, capture descriptions, and known omissions. |
| [`target.js`](../src/testar_evidence_playwright/resources/target.js) | Finds a locator's structural DOM path so it can be matched to a captured widget. |

The capture functions and snapshot class are internal helpers. Most scripts use `PlaywrightEvidence`, its `checks` property, and optionally `CaptureOptions`.

## Integrating a script

Keep the script's browser, page, and locators. Start a session and route the actions and test-oracle evaluations that should appear in the evidence through the adapter:

```python
with PlaywrightEvidence.start(output, page) as evidence:
    evidence.navigate(url)
    evidence.fill(page.locator("input[name='username']"), "john")
    evidence.fill_secret(page.locator("input[name='password']"), "demo")
    evidence.click(page.get_by_role("button", name="Log In", exact=True))
    evidence.checks.has_text(
        "example:account-services",
        "Account Services is displayed",
        page.get_by_text("Account Services", exact=True),
        "Account Services",
    )
    evidence.finish("completed")
```

Here `output` is the chosen evidence directory, `page` is the caller's synchronous Playwright page, and `url` is the starting address. The complete browser setup is in [the loan example](../examples/parabank_denied_loan.py).

Direct `locator.click()` calls and ordinary assertions continue to work. The adapter records operations that pass through its helpers. A framework integrating this adapter decides which setup actions, generated actions, and test oracles to route through it.

## Following one click and assertion

Consider clicking Apply Now and checking that the loan status is Denied:

1. Before the click, `PlaywrightEvidence` uses the latest pending capture or captures the page. `capture.js` collects properties; `capture_page` converts them into generic widgets and optionally takes a screenshot.
2. `target.js` identifies the button's DOM path. The adapter finds the corresponding captured widget when possible.
3. The adapter records the source observation with the next scripted action as its candidate, records selection, and calls Playwright's click. It records accepted submission on return, or rejected submission with the operation's error on exception.
4. `PlaywrightChecks` runs the retrying assertion. After its final outcome, it reads the text, captures the page, and prepares an evaluation connected to that observation and, when identifiable, the widget property.
5. At the next action or session ending, the adapter writes the pending observation, completes the accepted click's execution link, and writes the evaluation.

Denied is the expected business result, so that evaluation passes when Denied appears. Acceptance of the click describes the click operation, not whether the business response met the test objective.

## Why a capture can remain pending

The latest capture briefly stays in memory until the next scripted action is known. That action can then be attached as a candidate to the same observation. A result observation can also be the next action's source, forming a state-action-state trace.

This pending capture is unrelated to an evaluation whose status is pending. The built-in assertion helpers report their final outcomes.

`observe()` requests another captured visit. Repeated assertions can produce separate observations without action links between them. The adapter preserves those visits rather than inventing actions.

## Extending actions and test oracles

For another action, use `perform`:

```python
account = page.locator("#fromAccountId")
evidence.perform(
    "select", "Select source account", account,
    {"value": "12345"}, lambda: account.select_option("12345"),
)
```

Choose an account value available in the application. Parameters should describe the operation's actual inputs. A callback containing a whole routine creates one outer action; wrap its individual steps when the evidence needs those separate actions.

The built-in test-oracle helpers are `has_text`, `has_value`, and `has_attribute`. Set `evidence.checks.timeout_millis` to configure their retry timeout. For another test oracle, use `checks.evaluate` with its name, `CheckRule`, target locator and property, optional expectation, assertion callback, and a callback returning `CheckReading.value(...)`, `.absent()`, or `.unknown()`.

The assertion determines success or failure. The reading describes what was obtained after that assertion. A failed assertion is recorded and rethrown; an unrelated exception is an error and is also rethrown. If a captured widget cannot be identified, the evaluation retains the requested target and explanation at observation level.

## Understanding the captured data

The capture description tells consumers what area, widgets, and properties were collected and which known limitations apply. Source properties retain their concrete names and values. Shared aliases such as `ui.url`, `ui.title`, and `ui.text` identify declared equal readings within the same record.

Captured password values are redacted. `fill_secret` also excludes its argument from action parameters. Custom callbacks supply their own recorded parameters, expectations, and readings.

Capture reads, screenshots, locator resolution, and assertion readings are successive browser operations. The application can change between them. The script controls application readiness and decides when another observation is useful.

A widget's structural path identifies its place within one capture. Comparing different visits or tools requires explicit matching rules in consumers. The shared traces retain the original evidence for those decisions.

See [SPEC](SPEC.md) for recording rules and [the README](../README.md) for installation and commands.

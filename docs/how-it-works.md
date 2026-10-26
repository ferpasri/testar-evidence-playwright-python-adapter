# How the Playwright Python adapter works

Playwright controls the browser. The adapter records supported operations and final oracle results. The independent evidence library validates and saves the generic records.

```text
Python script -> Playwright facade -> page recorder and checks -> DOM capture -> evidence library
```

## Components

| Component | What it does |
| --- | --- |
| `sync_api.py` | Provides `sync_playwright`, `expect`, and `evidence_for` alongside native Playwright API types. |
| `facade.py` | Delegates native calls, follows returned objects and callbacks, and owns each page's recorder. |
| `facade_actions.py` | Describes supported operations and the arguments to record. |
| `facade_assertions.py` | Delegates retrying assertions and identifies their expected/observed values and targets. |
| `PlaywrightEvidence` in `evidence.py` | Starts run and sequence, records actions and transitions, and finishes the histories. |
| `AvailableAction` in `evidence.py` | Describes a caller-reported choice and its planning parameters. |
| `PlaywrightChecks` in `checks.py` | Runs a test oracle or records an existing result with optional details and severity. |
| `CaptureOptions`, `BrowserSnapshot`, and `capture_page` | Configure capture and hold one captured page, hierarchy, timestamp, and screenshot. |
| `capture.js` / `target.js` | Read DOM properties and find a locator's structural path. |

## Minimal integration

Import `sync_playwright` and `expect` from `testar_evidence_playwright.sync_api`, configure recording once, and keep the ordinary Playwright operations. Pages and locators retain their native API behavior. The facade records supported actions and final Playwright assertions; ordinary Python assertions and custom tool callbacks use explicit reporting.

The [facade example](../examples/parabank_facade.py) shows the loan-denial scenario. The [explicit example](../examples/parabank_denied_loan.py) uses caller-defined boundaries.

## Additional information from a caller

Use `evidence_for(page)` to access the existing facade recorder. A caller can report choices, compound actions, and custom oracle results in the same trace:

```python
from testar_evidence import ObservedValue, Oracle, OracleSeverity
from testar_evidence_playwright import AvailableAction
from testar_evidence_playwright.sync_api import evidence_for

recorder = evidence_for(page)
action = AvailableAction("click", "Apply for loan", page.locator("#apply"))
recorder.record_available_actions([action], scope="eligible")
recorder.perform(action.action_type, action.name, action.locator, {}, action.locator.click,
                 available_action=action)
recorder.capture()

# The caller already ran this oracle and supplies its actual outcome and value.
recorder.checks.record_result(Oracle("example:loan-status"), "Loan was denied", "passed",
    locator=page.locator("#loanStatus"), property="playwright.dom.textContent",
    expected_value="Denied", observed_value=ObservedValue.value("Denied"),
    oracle_severity=OracleSeverity("informational"))
```

The caller supplies the list it discovered and the results it obtained. Planning parameters describe available actions; actual supplied input belongs to an execution. Passing `available_action=` connects selection to a reported choice. Several native operations inside one `perform` callback form one compound action, with nested facade records suppressed.

`checks.evaluate` runs a custom oracle callback once and obtains its observed value afterward. `checks.record_result` records an existing outcome without running the oracle again. Custom callers choose their targets, values, details, and optional severity. A supplied severity label keeps its source meaning; omission does not create a default.

Without the facade, start a recorder and call `capture`, `select_action`, `report_action_result`, and `capture` around existing native execution. The outcome is `returned` or `error`; its business correctness is evaluated by a separate oracle.

## Following one action and oracle

For Apply Now followed by checking Denied:

1. The recorder captures the source observation or uses the latest buffered capture.
2. It identifies the selected available action and its widget when the locator can be matched.
3. Playwright executes the click once. The recorder saves `returned` or `error` as its action execution result.
4. A later capture supplies the resulting observation and creates the recorded transition.
5. Playwright evaluates the text assertion with its normal retries. The adapter records one final oracle result, its observed value, and its precise target when identifiable.
6. The next action or trace ending flushes buffered records. The sequence finishes before the run.

Denied passes this oracle because denial is its expected business response. A returned click and a passed oracle describe different facts.

The latest observation can stay buffered so the next action can be included in its available-action list. That capture can then be both the previous action's result and the next action's source. Consecutive oracle evaluations can produce observations with no action between them. The recorder preserves those observations without inventing a transition.

## Where the data goes

The evidence library validates and writes `manifest.json`, `evidence.jsonl`, and referenced screenshot artifacts. Each used page has its own trace. See [SPEC](SPEC.md#captured-data) for property mappings, hidden widgets, and capture limits, and [README](../README.md) for setup and test commands.

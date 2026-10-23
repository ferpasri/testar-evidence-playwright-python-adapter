# Playwright Python adapter specification

The adapter translates synchronous Playwright operations and test-oracle results into concrete execution evidence. A recorded run follows a page. Completed actions connect their before and resulting observations into a state-action-state execution trace.

The script controls the browser, application waits, and test flow. The imported `testar-evidence-python` library validates records and saves the manifest, JSONL events, and artifacts. See [How it works](how-it-works.md) for the implementation components.

## Automatic recording

`testar_evidence_playwright.sync_api` exposes the adapter's `sync_playwright`, `expect`, and `evidence_for` alongside native Playwright API types. Configure the evidence parent directory, SUT label, and test objective label once:

```python
with sync_playwright(
    evidence_path="target/traces",
    sut="parabank",
    test_objective="denied-loan",
) as playwright:
    ...
```

The facade delegates to native Playwright objects. Pages, locators, frames, keyboard and mouse objects retain their native API behavior. Returned objects and objects supplied to event callbacks continue through the same facade. Each page has a separate trace.

Supported actions include navigation, click, text entry, selection, checking controls, dragging, keyboard and mouse input. Their parameters describe the supplied operation. Readings and locator construction do not become actions or evaluations. A call with `trial=True` performs Playwright's readiness check without recording an executed action. Arbitrary JavaScript passed to `evaluate` retains its native behavior; the caller can use explicit recording to describe its purpose.

The adapter's `expect` records page and locator assertions, including their negative forms. Native Playwright handles retries; the adapter records one final outcome. A property reading is attached when the assertion has a mapped concrete reading. Collection assertions target the observation, and other assertions retain their outcome with an unknown actual reading when necessary. Soft assertions and API response assertions delegate to Playwright; their results need explicit recording integration.

The facade captures after a supported action returns. Closing a page, its browser context, the browser, or the Playwright session finishes the associated traces. If a page has already closed, available captured evidence is saved without inventing a resulting observation. A submitted action without a resulting capture remains incomplete.

## Run metadata

The optional `origin` is a descriptive name for the calling tool, script, or integration. It accepts any nonblank source name. The optional test objective describes what the caller intends to investigate or verify. Both values retain their original text, including Unicode and spacing; omitted values stay absent.

The adapter records the [shared run metadata](https://github.com/ferpasri/testar-evidence-specs/blob/main/docs/run-metadata.md) once in run properties:

| Property | Recorded value |
| --- | --- |
| `evidence.origin` | The caller's origin, when supplied. |
| `evidence.test_objective` | The caller's test objective, when supplied. |
| `evidence.adapter` | This adapter's identifier, supplied automatically. |
| `evidence.adapter_version` | This adapter's version, supplied automatically. |

The required `producer` identifies the producing component. Origin identifies the caller independently; one source can perform predefined setup and then explore. The test objective describes intent. Check results identify the conditions actually evaluated and their outcomes. Candidate scope describes the reported candidate list.

The facade and explicit recorder accept optional `origin` and `test_objective` keyword arguments. Explicit recorders also accept the shared keys in `properties`. Conflicting descriptions or adapter identity values are rejected before writing a trace. An omitted objective uses `run` only as a directory label; it adds no objective property.

## Captured observations

Capture reads the document, DOM elements, and open shadow roots. Each widget has a structural DOM path and a corresponding identifier within its observation. Frame documents and closed shadow roots are outside the capture scope.

State properties retain the actual document URL and title under `playwright.document.url` and `playwright.document.title`, with equal-value aliases `ui.url` and `ui.title`. Widgets retain attributes under `playwright.attribute.*`, selected live DOM readings under `playwright.dom.*`, bounds, and selected computed styles under `playwright.style.*`. An HTML value attribute, live input value, text content, and title attribute keep their concrete meanings.

`evidence.property_aliases` declares equal readings within the same record. `evidence.capture` describes the collection policy and known truncation. `evidence.omitted_properties` explains known exclusions, redactions, unavailable readings, and limits. A missing captured property alone does not establish that it is absent from the application.

`CaptureOptions` controls collection. Supply it as `capture_options` to the facade or `options` to an explicit recorder.

| Option | Default | Maximum |
| --- | ---: | ---: |
| `max_widgets`, including document root | 2,000 | 5,000 |
| `max_string_length`, per widget string | 8,192 | 65,536 |
| `max_property_characters`, selected property budget | 4,194,304 | 8,388,608 |
| `max_attributes`, per widget | 256 | 1,024 |

The property budget counts selected names and JSON-serialized values, including aliases. Oversized strings are omitted whole. Structural paths, capture descriptions, and bounded omission descriptions are stored separately.

Default computed styles are display, visibility, font size, font family, color, and background color. Callers can select up to 64 styles and exclude up to 256 attribute names. Password values and value attributes are redacted. `screenshots=True` records viewport PNGs; callers can disable them.

## Actions and observations

The explicit `navigate`, `click`, `fill`, and `fill_secret` helpers execute an action and record its lifecycle. `perform` accepts a custom action kind, description, locator, parameters, and operation callback. A callback containing several operations produces one outer action. Browser interactions executed by callbacks while another action is being recorded belong to that outer action; explicit integration is needed to report finer boundaries.

Candidate scope describes the action bindings reported at an observation. A script's next action does not enumerate the other available interactions.

The latest capture can remain pending until the next action is known. That action is then recorded as a candidate on the captured observation. Its result observation can also become the next action's source.

Selection is recorded before calling the operation. A normal return records accepted submission. An operation exception records rejected submission and its error, then is rethrown. Rejection describes failure reported by the operation wrapper, such as Playwright failing to click its target. A denied loan is a business response and does not reject a successful click.

The next captured observation completes an accepted execution. `observe()` requests another captured visit. Consecutive test-oracle evaluations can create separate visits without inventing action links. Repeated content remains separate visits.

A locator is matched to the captured widget through its structural DOM path. If it cannot be matched, the adapter preserves the locator description and an unspecified widget binding. IDs and paths across observations do not establish that two widgets are logically equivalent.

Supported text-entry actions redact recorded input when their target is a password field or its password status cannot be resolved safely. Password-value assertions omit their recorded expected and actual values and redact saved error details. The original operation or assertion still receives its supplied arguments and raises its original exception.

## Test-oracle results

Assertions record one final result: passed when the assertion returns, failed for an assertion failure, and error for another exception. The original exception is rethrown after recording.

The explicit `checks.has_text`, `has_value`, and `has_attribute` helpers use Playwright's retrying assertions. `checks.timeout_millis` sets their assertion timeout. `checks.evaluate` accepts a rule, assertion callback, actual-reading callback, locator, property name, and optional expected value. The callback owns the assertion's retry behavior. An omitted expectation differs from an explicit JSON null. Readings distinguish a value, confirmed absence, and unknown information.

After the assertion, the adapter obtains the reading and captures the page. These are successive browser operations and can observe changes at different moments. A reading failure becomes unknown with its error details. A property result targets its captured widget when identifiable; otherwise it targets the observation and retains the requested target and explanation. Collecting a property does not mean that a test oracle checked it.

`checks.recording_error` retains a failure while recording the latest explicit evaluation and is reset for the next evaluation. When an oracle also fails, the original exception is rethrown with a recording-error note. Framework integrations can inspect this property to stop for the recording failure while retaining the oracle exception.

## Explicit recording points

For a facade page, `evidence_for(page)` returns the recorder already used for its automatic trace. Its `perform`, `checks.evaluate`, and `checks.record_result` methods add custom operations and results to that same run.

An integration using native Playwright without the facade can create its own recorder and keep its original execution calls. It reports their evidence through these methods:

| Method | What the caller reports |
| --- | --- |
| `start_trace(parent, page, sut=..., test_objective=..., sequence=...)` | Start a new trace in a reserved timestamped directory; `directory` gives its path. |
| `capture()` | Capture the currently observed state for the next action and its checks. |
| `select_action(kind, name, locator, parameters)` | Identify the chosen action before the framework executes it. |
| `submit_action(accepted, error=None)` | Report the framework's returned execution outcome. |
| `checks.record_result(rule_id, name, status, error=None)` | Report a check that the framework has already executed. |

After submission, call `capture()` to collect the resulting state. An accepted execution is connected to that observation when its records are written. Rejected submissions preserve their reported error and have no completed action link. Exiting the recorder context with an unreported action exception records rejection; ending without a reported outcome leaves the execution incomplete.

A capture stays pending so it can also contain the next selected candidate. Reported check results refer to that same captured observation, use an unknown actual reading, and make no widget/property target claim. Their callbacks are not executed again. Frameworks needing detailed targets and readings use `checks.evaluate`. The caller decides capture timing; DOM reads, checks, and screenshots remain separate operations.

Ordinary Python assertions, tool-specific oracle callbacks, discovered alternatives, and reasons for action selection require caller-supplied integration. The explicit APIs provide the recording points for that information.

## Persistence and ending

Trace names use supplied SUT and test objective labels, an optional sequence number, and a local timestamp including milliseconds. Filename labels are sanitized and limited to 32 characters; a collision adds a numeric suffix. Run properties retain the original labels. The exact-directory `start` API remains available.

`finish(reason)` writes pending observations, execution results, evaluations, and the run ending. Repeated calls are safe. Leaving the recorder context also finishes an unfinished session and closes the evidence writer. An explicit recorder leaves ownership of the page and browser with its caller.

A new store refuses an existing manifest or event log. Earlier successful writes remain available after a later failure. Consumers can derive matching and abstract models from these concrete traces while retaining the supporting observations.

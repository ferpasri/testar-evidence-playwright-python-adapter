# Playwright Python adapter specification

The adapter translates Playwright operations and caller-supplied test information into the shared [evidence format](https://github.com/ferpasri/testar-evidence-specs/blob/main/docs/evidence-format.md). The independent evidence library validates the records and writes portable files.

Each page recorder owns one test run and one test sequence. They have separate identities and start events. Observations and executions belong to that sequence. The sequence finishes before its parent run. A facade session can create independent page traces; those browser sessions and test runs are different concepts.

## Python API

The facade keeps native synchronous Playwright objects. Import `sync_playwright`, `expect`, and `evidence_for` from `testar_evidence_playwright.sync_api`. Supported navigation, element, keyboard, mouse, and touch operations are recorded automatically. Reads, waits, and JavaScript evaluation retain their native behavior. Trial operations remain readiness checks.

| API | Purpose |
| --- | --- |
| `PlaywrightEvidence.start(directory, page, ...)` | Start a recorder at an exact directory; optional `sequence_index` defaults to 1. |
| `start_trace(parent, page, sut=..., test_objective=..., sequence=...)` | Reserve a timestamped directory; a supplied sequence number becomes its recorded sequence index. |
| `capture()` / `observe()` | Capture a buffered observation / write an additional observation. |
| `record_available_actions(actions, scope=...)` | Capture caller-reported choices, with generated scope by default. |
| `select_action(action_type, name, locator, parameters, available_action=...)` | Record selection before the caller's native execution. |
| `report_action_result("returned")` / `report_action_result("error", message)` | Record the operation's actual outcome. |
| `perform(..., operation=..., available_action=...)` | Execute and record one action or compound action. |
| `checks.evaluate(name, oracle=..., assertion=..., observed_value=..., ...)` | Run one oracle and record its final result. |
| `checks.record_result(oracle, name, oracle_status, ...)` | Record an existing result, with optional locator, property, expected/observed values, oracle details, and severity. |
| `finish(reason)` | Flush buffered records and finish sequence and run. |

`AvailableAction.description()` returns its generic action description. Detailed oracle arguments use `Oracle`, `ObservedValue`, and `OracleSeverity` from `testar_evidence`. `expected_value=UNSPECIFIED` omits the expectation; `None` supplies JSON null. `oracle_severity` is omitted unless supplied. `checks.recording_error` exposes a recording failure from the latest evaluation. Use the recorder on its Playwright thread.

Ordinary Python assertions and external oracle callbacks use the explicit API. Soft and API-response assertions retain their native behavior and can be reported explicitly with appropriate GUI context. See [How it works](how-it-works.md) for the components and an integration example.

## Recording an action

The recorder captures the source observation when needed, records the selected action, and delegates the operation to Playwright. A normal return produces an action execution result with outcome `returned`. An exception produces outcome `error`, retains the reported message, and reaches the original caller. A test oracle evaluates the application response separately.

A later capture in the same sequence supplies the resulting observation. The recorder then writes a recorded transition connecting the source observation, execution, and resulting observation. An error has no completed transition. Ending without an execution result or resulting observation leaves the execution incomplete.

The latest capture can remain buffered until the next action is known. Its observation then contains that available action and can serve as both the previous action's result and the next action's source. Buffered observations are separate from pending oracle statuses. Repeated captures remain distinct observations even when their contents match.

## Available actions and compound actions

`AvailableAction` describes an operation reported by the caller: action type, name, optional locator, and planning parameters. The recorder saves an action description and a binding to the captured widget when identifiable. Unexecuted choices remain recorded. Actual generated input belongs to the action execution parameters rather than its planning description.

The facade retains simple CSS targets supplied directly to `page.locator()`, such as `#apply` or `button[id="apply"]`. Available-action recording resolves these targets together in bounded native Playwright calls. It requires exactly one matching element for each widget binding, including matches in open shadow roots. Other locator forms and failed batch calls use individual native resolution. The target cache lasts for that reported list; later lists resolve their targets again.

The reported scope is generated, eligible under the caller's selection rules, or unknown. An explicitly empty eligible list keeps that meaning. The facade reports its next selected operation with unknown scope; the caller can report a richer list through the explicit API.

Passing a reported available action when selecting or performing it connects the execution to that description. A choice absent from the reported list is rejected before its callback runs. A callback containing several native operations forms one compound action. Automatic transitions inside that boundary are suppressed.

`perform(..., record_operations=True)` retains the supported native operations in order and captures the compound result once. The page must come from the facade, and supported operations stay on that page. Each operation retains its method, locator description, supplied parameters, and returned/error outcome. The original exception reaches the caller; completed operations are retained and no operation is retried automatically. Password arguments and related error text are redacted. Ordinary calls outside this explicit boundary remain separate actions.

The resulting observation references an `application/json` artifact. Its object contains `format: "compound-action-operations"`, `version: 1`, the parent `action_execution` ID, and an ordered `operations` list. Each operation contains `index`, `method`, `action_type`, nullable `locator`, `parameters`, `outcome`, and nullable `error`. The parent execution parameters include `playwright.compound_action: true`. The artifact uses the evidence library's content-addressed storage; its filename extension does not determine its media type.

Intermediate GUI captures are omitted within this boundary. The result remains buffered for oracle reporting and the next action. If the page cannot be captured, the observation records unavailable capture with no invented widget data, while retaining the operation artifact. Error executions keep the format's normal error semantics and do not acquire a completed recorded transition.

## Data used by action abstraction rules

The adapter records concrete descriptions and widget bindings. A consumer such as `testar-abstraction` chooses which fields define an abstract action. Selecting those rules does not change browser execution or trace recording.

| Recorded field | Meaning |
| --- | --- |
| Action description `action_type` | Native operation name or caller-supplied action type. |
| `playwright.description` / `playwright.locator` | Recorded name and locator description; these are descriptions, not global widget identities. |
| `playwright.available_parameters` | Nested planning parameters supplied through `AvailableAction`. |
| Execution `parameters` | Actual arguments supplied for this execution, including nested values. Supported secret arguments are redacted. |
| Available-action and execution `target_widget` | Reference to a widget in the source observation, when resolved. Its properties can be selected by abstraction rules. |

Planning and execution parameters remain separate even when their values differ. Rules select the corresponding fields explicitly, including nested paths when needed. Caller-supplied action types retain their names; a consumer defines any normalization between tools. An action description ID or structural widget path alone does not establish equivalence across traces.

## Oracle results

The adapter's assertion facade delegates retries and timeouts to Playwright and records one final result. Its explicit checks helper can run a custom oracle or record a result already obtained by the caller. Recording an existing result does not run the oracle again.

Oracle identities, statuses, targets, expected and observed values, oracle details, and optional severity follow the shared [oracle-result specification](https://github.com/ferpasri/testar-evidence-specs/blob/main/docs/oracle-results.md). Custom reports preserve the outcome and severity supplied by the caller.

Mapped assertions target a captured state property, widget, or widget property. Collection assertions target the observation. If a locator cannot identify a captured widget, the result targets the observation and retains the requested target and resolution limit in oracle details. Such a result does not establish individual property coverage.

Readings follow assertion execution and precede capture. These are separate browser operations and can observe application changes. An unavailable reading becomes unknown with its explanation. An original oracle failure is rethrown; an additional persistence failure remains available through the helper's recording-error information.

## Captured data

The capture script returns its data as JSON text through Playwright. Python decodes that text before building widgets and their parent references. Capture fields, selection limits, and redaction follow the same rules for every caller. Screenshots are obtained separately as PNG bytes.

Capture includes the document, DOM elements, and open shadow roots. Frame documents and closed shadow roots have explicit capture limitations. Widget IDs and structural paths identify elements within an observation; cross-observation equivalence is decided by a consumer's abstraction rules.

State properties retain the actual document address and title under `playwright.document.url` and `playwright.document.title`, with same-record aliases `ui.url` and `ui.title`. Widgets retain attributes under `playwright.attribute.*`, live DOM values under `playwright.dom.*`, bounds, and selected styles under `playwright.style.*`. A title attribute, text content, and accessible-label attribute keep their distinct source meanings.

DOM capture includes hidden elements within its capture limits. `playwright.style.display` and `playwright.style.visibility` are computed CSS values for each widget; they are not a combined visibility test. A child can have `visibility: visible` while an ancestor has `display: none`. Parent widget references retain the hierarchy needed to inspect that situation. Capturing hidden content preserves evidence without claiming that a user could see it.

The adapter records a widget's `title` attribute as `playwright.attribute.title`. It does not infer a widget title or accessible name from other text. `ui.role` aliases an explicit `role` attribute. State-level `ui.title` remains the document title. Widget tag names are stored as `playwright.dom.tagName`.

A visibility assertion reads Playwright's `is_visible()` result under `playwright.dom.visible` in its oracle result. This is a separate reading from DOM capture and CSS styles; it does not add that property to every captured widget. It also does not establish viewport intersection or lack of occlusion. A consumer's abstraction rules can compare captured styles, hierarchy, and text without treating one style value as general visibility. See the shared [capture specification](https://github.com/ferpasri/testar-evidence-specs/blob/main/docs/capture-evidence.md).

`evidence.capture` describes widget/property selection, limits, and truncation. `evidence.omitted_properties` records known exclusions, redactions, and unavailable values. `evidence.property_aliases` declares equivalent names within the same record. Missing captured data alone does not establish that a property is absent from the application or that no oracle ran.

| Capture limit | Default | Maximum |
| --- | ---: | ---: |
| Widgets, including document root | 2,000 | 5,000 |
| Characters per widget string | 8,192 | 65,536 |
| Selected property character budget | 4,194,304 | 8,388,608 |
| Attributes per widget | 256 | 1,024 |

The property budget counts selected names and JSON-serialized values, including aliases. Oversized strings are omitted whole. Bounded omission descriptions and structural paths are stored separately. Callers can select up to 64 computed styles and exclude up to 256 attributes. Default styles are display, visibility, font size, font family, color, and background color. Screenshots are optional viewport PNGs.

Password values and supported secret input arguments are redacted. Automatic value assertions with password or unresolved targets withhold sensitive expected/observed values and saved error details. Native calls still receive their original arguments and raise their original exceptions. Authors of custom reports choose the data they supply; capture settings also apply to the surrounding application data and screenshots.

## Run metadata

Optional origin and test objective descriptions are stored in run properties as `evidence.origin` and `evidence.test_objective`. The adapter supplies `evidence.adapter` and `evidence.adapter_version`. Descriptions retain Unicode and spacing; omitted descriptions remain absent. Origin is descriptive text rather than a fixed execution-style category.

## Storage and verification

A trace contains `manifest.json`, `evidence.jsonl`, and referenced screenshot files in `artifacts/`. Creation preserves existing evidence by refusing an existing manifest or log. The evidence library validates event references and lifecycle order, retains complete payloads, and verifies artifact hashes.

Closing a page, context, browser, or facade session finishes the corresponding recorder. An unavailable page is finished from its cached captures, without inventing a result observation. Successfully saved prefixes remain readable after later failures.

Unit and controlled local browser tests cover native delegation, final assertion outcomes, optional metadata, available actions, compound actions, execution errors, precise oracle targets, severity, password redaction, capture limits, callbacks, and reopening the produced traces. Abstraction remains separate from these recorded traces.

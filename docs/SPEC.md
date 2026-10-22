# Playwright Python adapter specification

The adapter translates explicit Playwright script actions and test-oracle evaluations into generic execution evidence. A session creates one recorded run. Completed actions connect their before and resulting observations into a state-action-state execution trace.

The script owns the synchronous Playwright page, browser, application waits, and test flow. The imported `testar-evidence-python` library validates records and saves the manifest, JSONL events, and artifacts. See [How it works](how-it-works.md) for the implementation components.

## Captured observations

Capture reads the document, DOM elements, and open shadow roots. Each widget has a structural DOM path and a corresponding identifier within its observation. Frame documents and closed shadow roots are outside the capture scope.

State properties retain the actual document URL and title under `playwright.document.url` and `playwright.document.title`, with equal-value aliases `ui.url` and `ui.title`. Widgets retain attributes under `playwright.attribute.*`, selected live DOM readings under `playwright.dom.*`, bounds, and selected computed styles under `playwright.style.*`. An HTML value attribute, live input value, text content, and title attribute keep their concrete meanings.

`evidence.property_aliases` declares equal readings within the same record. `evidence.capture` describes the collection policy and known truncation. `evidence.omitted_properties` explains known exclusions, redactions, unavailable readings, and limits. A missing captured property alone does not establish that it is absent from the application.

`CaptureOptions` controls collection:

| Option | Default | Maximum |
| --- | ---: | ---: |
| `max_widgets`, including document root | 2,000 | 5,000 |
| `max_string_length`, per widget string | 8,192 | 65,536 |
| `max_property_characters`, selected property budget | 4,194,304 | 8,388,608 |
| `max_attributes`, per widget | 256 | 1,024 |

The property budget counts selected names and JSON-serialized values, including aliases. Oversized strings are omitted whole. Structural paths, capture descriptions, and bounded omission descriptions are stored separately.

Default computed styles are display, visibility, font size, font family, color, and background color. Callers can select up to 64 styles and exclude up to 256 attribute names. Password values and value attributes are redacted. `screenshots=True` records viewport PNGs; callers can disable them.

## Actions and observations

`navigate`, `click`, `fill`, and `fill_secret` execute an action and record its lifecycle. `perform` accepts a custom action kind, description, locator, parameters, and operation callback. A callback containing several operations produces one outer action; wrap individual operations when their separate evidence matters.

The latest capture can remain pending until the next action is known. That action is then recorded as a candidate on the captured observation. Its result observation can also become the next action's source. The candidate scope is unknown because a script's next action does not enumerate the other available interactions.

Selection is recorded before calling the operation. A normal return records accepted submission. An operation exception records rejected submission and its error, then is rethrown. Rejection here describes failure reported by the operation wrapper, such as Playwright failing to click its target. It does not describe a business response such as a denied loan.

The next captured observation completes an accepted execution. `observe()` requests another captured visit. Consecutive test-oracle evaluations create separate visits without inventing action links. Repeated content remains separate visits.

A locator is matched to the captured widget through its structural DOM path. If it cannot be matched, the adapter preserves the locator description and an unspecified widget binding. IDs and paths across observations do not establish that two widgets are logically equivalent.

## Test-oracle results

`checks.has_text`, `has_value`, and `has_attribute` use Playwright's retrying assertions. `checks.timeout_millis` sets their assertion timeout. Each records one final result: passed when the assertion returns, failed for an assertion failure, and error for another exception. The original exception is rethrown after recording.

`checks.evaluate` accepts a rule, assertion callback, actual-reading callback, locator, property name, and optional expected value. The callback owns the assertion's retry behavior. An omitted expectation differs from an explicit JSON null. Readings distinguish a value, confirmed absence, and unknown information.

After the assertion, the helper obtains the reading and captures the page. These are successive browser operations and can observe changes at different moments. A reading failure becomes unknown with its error details. A property result targets its captured widget when identifiable; otherwise it targets the observation and retains the requested target and explanation. Collecting a property does not mean that a test oracle checked it.

## Persistence and ending

`finish(reason)` writes pending observations, execution results, evaluations, and the run ending. Repeated calls are safe. Leaving the context manager also finishes an unfinished session and closes the evidence writer. The caller keeps ownership of the page and browser.

A new store refuses an existing manifest or event log. Earlier successful writes remain available after a later failure. The example reserves timestamped directories using its application and test objective labels; an explicitly supplied directory keeps its exact path.

Consumers can derive matching and abstract models from these concrete traces while retaining the supporting observations.

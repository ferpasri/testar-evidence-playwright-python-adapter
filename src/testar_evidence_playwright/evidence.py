"""Turn explicit script operations into a concrete state-action-state trace."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from threading import get_ident
from typing import Any, Callable
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError, Locator, Page
from testar_evidence import (
    ActionCandidate as CandidateBinding, ActionDefinition, CheckReading, CheckResult, CheckRule, CheckTarget,
    ConcreteState, EvidenceEvent, Execution, JsonlStore, Manifest, Observation, Run,
)

from ._metadata import VERSION as VERSION, run_properties
from .capture import BrowserSnapshot, CaptureOptions, capture_page, identity, target_path
from .checks import PlaywrightChecks


@dataclass(frozen=True)
class ActionCandidate:
    """An action candidate reported by a tool, including its planning parameters."""
    kind: str
    name: str
    locator: Locator | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    def definition(self) -> ActionDefinition:
        description = self.name if self.locator is None else str(self.locator)
        parameters = EvidenceEvent("validation:event", "action_defined",
            ActionDefinition("validation:action", self.kind, self.parameters)).to_dict()["event"]["data"]["properties"]
        return ActionDefinition("playwright:available:" + identity(json.dumps(
            [self.kind, self.name, description, parameters], sort_keys=True)), self.kind,
            {"playwright.description": self.name, "playwright.locator": description,
             "playwright.available_parameters": parameters})


@dataclass
class _PendingObservation:
    id: str
    sequence: int
    snapshot: BrowserSnapshot
    checks: list[CheckResult] = field(default_factory=list)
    candidates: list[CandidateBinding] | None = None
    candidate_scope: str = "unknown"


class PlaywrightEvidence:
    """Record wrapped actions and test oracles; the caller owns the page and browser."""

    def __init__(self, page: Page, options: CaptureOptions, store: JsonlStore, run_id: str):
        self._page = page
        self._options = options
        self._store = store
        self._run_id = run_id
        self._thread = get_ident()
        self._checks = PlaywrightChecks(self)
        self._definitions = {}
        self._events = 0
        self._observations = 0
        self._executions = 0
        self._pending = None
        self._pending_result = None
        self._selected_execution = None
        self._selected_parameters = None
        self._last_observation = None
        self._ending_reason = "closed"
        self._finished = False
        self._closed = False
        self._broken = False
        self._busy = False

    @classmethod
    def start(cls, directory: str | Path, page: Page, options: CaptureOptions | None = None,
              properties: dict[str, Any] | None = None, *, origin: str | None = None,
              test_objective: str | None = None) -> PlaywrightEvidence:
        """Create a new trace at the exact supplied directory, preserving existing files."""
        if page is None:
            raise TypeError("A synchronous Playwright page is required")
        options = CaptureOptions() if options is None else options
        if not isinstance(options, CaptureOptions):
            raise TypeError("options must be CaptureOptions")
        supplied = run_properties(properties, origin=origin, test_objective=test_objective)
        supplied["playwright.initial_url"] = page.url
        identifier = uuid4().hex
        run_id = "playwright-python:run:" + identifier
        run = Run(run_id, "playwright-python", identifier, supplied)
        EvidenceEvent("validation:event", "run_started", run).to_dict()
        store = JsonlStore.create(directory, Manifest("playwright-python:model:" + identifier))
        evidence = cls(page, options, store, run_id)
        try:
            evidence._emit("run_started", run)
        except BaseException:
            store.close()
            raise
        return evidence

    @classmethod
    def start_trace(cls, parent: str | Path, page: Page, *, sut: str = "application",
                    test_objective: str | None = None, sequence: int | None = None,
                    options: CaptureOptions | None = None, properties: dict[str, Any] | None = None,
                    origin: str | None = None) -> PlaywrightEvidence:
        """Reserve a timestamped trace directory beneath the supplied parent."""
        if not isinstance(sut, str) or not sut.strip():
            raise ValueError("SUT label must be a nonblank string")
        if sequence is not None and (type(sequence) is not int or sequence < 1):
            raise ValueError("Sequence must be a positive integer")
        supplied = run_properties(properties, origin=origin, test_objective=test_objective)
        directory = _trace_directory(Path(parent), sut, supplied.get("evidence.test_objective", "run"), sequence)
        supplied["playwright.sut"] = sut
        if sequence is not None:
            supplied["playwright.sequence"] = sequence
        return cls.start(directory, page, options, supplied)

    @property
    def directory(self) -> Path:
        return self._store.directory

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def checks(self) -> PlaywrightChecks:
        return self._checks

    def navigate(self, url: str, **options) -> Any:
        return self.perform("navigate", "Navigate to " + url, None, {"url": url, **options},
            lambda: self._page.goto(url, **options))

    def click(self, locator: Locator, **options) -> Any:
        return self.perform("click", "Click " + str(locator), locator, dict(options),
            lambda: locator.click(**options))

    def fill(self, locator: Locator, value: str, **options) -> Any:
        if not isinstance(value, str):
            raise TypeError("Fill value must be a string")
        return self.perform("fill", "Fill " + str(locator), locator, {"value": value, **options},
            lambda: locator.fill(value, **options))

    def fill_secret(self, locator: Locator, value: str, **options) -> Any:
        if not isinstance(value, str):
            raise TypeError("Fill value must be a string")
        return self.perform("fill", "Fill " + str(locator), locator,
            {**options, "playwright.redacted_parameters": ["value"]}, lambda: locator.fill(value, **options))

    def record_candidates(self, actions: list[ActionCandidate], *, scope: str = "generated") -> str:
        """Capture a visit with the action candidates a tool reports, including an empty list."""
        self._require_active()
        if self._selected_execution is not None:
            raise RuntimeError("Report the selected action's outcome before recording action candidates")
        if scope not in {"generated", "eligible", "unknown"}:
            raise ValueError("Candidate scope must be generated, eligible, or unknown")
        if not isinstance(actions, list) or not all(isinstance(action, ActionCandidate) for action in actions):
            raise TypeError("Actions must be a list of ActionCandidate values")
        definitions = [action.definition() for action in actions]
        self._busy = True
        try:
            if self._pending is not None:
                self._flush()
            self._ensure_pending()
            candidates = []
            resolved = {}
            with self._recording_scope():
                for action, definition in zip(actions, definitions):
                    description = None if action.locator is None else str(action.locator)
                    if description not in resolved:
                        widget = self._pending.snapshot.widget_at(target_path(action.locator))
                        resolved[description] = None if widget is None else widget.id
                    candidate = CandidateBinding(definition.id, resolved[description])
                    if candidate not in candidates:
                        candidates.append(candidate)
            for definition in definitions:
                self._define_action(definition)
            self._pending.candidates = candidates
            self._pending.candidate_scope = scope
            return self._pending.id
        finally:
            self._busy = False

    def select_action(self, kind: str, name: str, locator: Locator | None,
                      parameters: dict[str, Any], *, candidate: ActionCandidate | None = None) -> str:
        """Record the selected action before the caller executes it."""
        self._require_active()
        if self._selected_execution is not None:
            raise RuntimeError("Report the selected action's outcome before selecting another")
        description = name if locator is None else str(locator)
        definition = ActionDefinition("playwright:action:" + identity(json.dumps([kind, name, description])),
            kind, {"playwright.description": name, "playwright.locator": description})
        if candidate is not None:
            if not isinstance(candidate, ActionCandidate):
                raise TypeError("candidate must be ActionCandidate")
            if (kind, name, description) != (candidate.kind, candidate.name,
                    candidate.name if candidate.locator is None else str(candidate.locator)):
                raise ValueError("Selected action must match its candidate description")
            definition = candidate.definition()
        EvidenceEvent("validation:event", "action_defined", definition).to_dict()
        # Validate and copy arguments before any action can affect the application.
        prepared = EvidenceEvent("validation:event", "action_selected",
            Execution("validation:execution", self.run_id, "validation:observation", definition.id,
                parameters=parameters)).to_dict()["event"]["data"]["parameters"]
        self._busy = True
        try:
            self._ensure_pending()
            with self._recording_scope():
                widget = self._pending.snapshot.widget_at(target_path(locator))
            if candidate is not None and self._pending.candidates is not None:
                binding = next((item for item in self._pending.candidates if item.action == definition.id), None)
                if binding is None:
                    raise ValueError("Selected action was not reported for this observation")
                widget = next((item for item in self._pending.snapshot.widgets if item.id == binding.target), None)
            if locator is not None:
                prepared["playwright.locator"] = description
                prepared["playwright.target_resolution"] = "not_captured" if widget is None else "captured_dom_path"
            secret_parameters = {"value", "text", "key", "playwright.positional_arguments",
                "playwright.keyword_arguments"}.intersection(prepared)
            if secret_parameters:
                with self._recording_scope():
                    secret_target = locator is None or self._password_target(locator, widget)
                if secret_target:
                    for parameter in secret_parameters:
                        prepared.pop(parameter)
                    previous = prepared.get("playwright.redacted_parameters", [])
                    prepared["playwright.redacted_parameters"] = sorted(set(previous) | secret_parameters)
            source = self._flush(definition, None if widget is None else widget.id)
            self._executions += 1
            execution = self.run_id + ":execution:" + str(self._executions)
            self._emit("action_selected", Execution(execution, self.run_id, source, definition.id,
                None if widget is None else widget.id, prepared))
            self._selected_execution = execution
            self._selected_parameters = prepared
            return execution
        finally:
            self._busy = False

    def submit_action(self, accepted: bool, error: str | None = None) -> None:
        """Report the execution outcome returned by the caller's tool."""
        self._require_active()
        self._submit_action(accepted, error)

    def _submit_action(self, accepted: bool, error: str | None = None) -> None:
        execution = self._selected_execution
        if execution is None:
            raise RuntimeError("No selected action is awaiting an outcome")
        submission = {"execution": execution, "accepted": accepted, "error": error}
        EvidenceEvent("validation:event", "action_submitted", submission).to_dict()
        self._emit("action_submitted", submission)
        self._selected_execution = None
        self._selected_parameters = None
        if accepted:
            self._pending_result = execution
        else:
            self._ending_reason = "action_failed"

    def perform(self, kind: str, name: str, locator: Locator | None,
                parameters: dict[str, Any], operation: Callable[[], Any], *,
                candidate: ActionCandidate | None = None) -> Any:
        """Record one action, its actual arguments, and its returned or raised outcome."""
        if not callable(operation):
            raise TypeError("Action operation must be callable")
        self.select_action(kind, name, locator, parameters, candidate=candidate)
        self._busy = True
        try:
            try:
                with self._recording_scope():
                    result = operation()
            except BaseException as error:
                self._ending_reason = "action_failed"
                try:
                    self._submit_action(False, self._action_error(error, self._selected_parameters))
                except Exception as recording:
                    error.add_note("Evidence recording also failed: " + str(recording))
                raise
            self._submit_action(True)
            return result
        finally:
            self._busy = False

    def capture(self) -> str:
        """Capture a visit for the next action or reported check, retaining its identity."""
        self._require_active()
        if self._selected_execution is not None:
            raise RuntimeError("Report the action outcome before capturing its result state")
        self._busy = True
        try:
            if self._pending is not None:
                self._flush()
            self._ensure_pending()
            return self._pending.id
        finally:
            self._busy = False

    def observe(self) -> str:
        """Record an additional visit without inventing an action between captures."""
        self._require_active()
        self._busy = True
        try:
            if self._pending is not None:
                self._flush()
            self._ensure_pending()
            return self._flush()
        finally:
            self._busy = False

    def finish(self, reason: str = "completed", *, capture_final: bool = True) -> None:
        """Finish the run, using only saved captures when the page is no longer available."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Run-ending reason must be nonblank text")
        if type(capture_final) is not bool:
            raise TypeError("capture_final must be a boolean")
        if self._finished:
            return
        self._require_active()
        EvidenceEvent("validation:event", "run_finished", {"run": self.run_id, "reason": reason}).to_dict()
        self._busy = True
        try:
            if self._pending is None and capture_final and \
                    (self._pending_result is not None or self._last_observation is None):
                self._ensure_pending()
            if self._pending is not None:
                self._flush()
            self._emit("run_finished", {"run": self.run_id, "reason": reason})
            self._finished = True
        finally:
            self._busy = False

    def close(self) -> None:
        if self._closed:
            return
        if get_ident() != self._thread:
            raise RuntimeError("Use the evidence session on its Playwright thread")
        if self._busy:
            raise RuntimeError("An evidence operation is already running")
        try:
            if not self._finished and not self._broken:
                self.finish(self._ending_reason)
        finally:
            self._store.close()
            self._closed = True

    def __enter__(self) -> PlaywrightEvidence:
        self._require_active()
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        if exception is not None and self._selected_execution is not None and not self._broken:
            try:
                self.submit_action(False, self._action_error(exception, self._selected_parameters))
            except Exception as recording:
                exception.add_note("Evidence recording also failed: " + str(recording))
        if exception is not None and self._ending_reason == "closed":
            self._ending_reason = "script_failed"
        try:
            self.close()
        except Exception as cleanup:
            if exception is None:
                raise
            exception.add_note("Evidence cleanup also failed: " + str(cleanup))

    def _record_check(self, name: str, rule: CheckRule, locator: Locator | None,
                      property: str | None, expected: Any, status: str,
                      actual: CheckReading, details: dict[str, Any], *, reuse_capture: bool = False) -> None:
        if status in {"failed", "error"}:
            self._ending_reason = "check_failed"
        if self._pending is not None and not reuse_capture:
            self._flush()
        self._ensure_pending()
        with self._recording_scope():
            widget = self._pending.snapshot.widget_at(target_path(locator))
        details = dict(details)
        if locator is None:
            target = CheckTarget.observation(self._pending.id) if property is None else \
                CheckTarget.state_property(self._pending.id, property)
        elif widget is not None:
            target = CheckTarget.widget(self._pending.id, widget.id) if property is None else \
                CheckTarget.widget_property(self._pending.id, widget.id, property)
        else:
            target = CheckTarget.observation(self._pending.id)
            details["playwright.requested_target"] = {"locator": str(locator), "property": property or ""}
            details["playwright.target_resolution"] = "The locator did not identify a captured top-document widget."
        check = CheckResult(self.run_id, name, rule, target, status, expected, actual, details)
        EvidenceEvent("validation:event", "checked", check).to_dict()
        self._pending.checks.append(check)

    def _ensure_pending(self) -> None:
        if self._pending is None:
            with self._recording_scope():
                snapshot = capture_page(self._page, self._options)
            self._observations += 1
            self._pending = _PendingObservation(self.run_id + ":observation:" + str(self._observations),
                self._observations, snapshot)

    def _flush(self, definition: ActionDefinition | None = None, target: str | None = None) -> str:
        pending = self._pending
        candidates = [] if pending.candidates is None else list(pending.candidates)
        if definition is not None:
            self._define_action(definition)
            selected = CandidateBinding(definition.id, target)
            if selected not in candidates:
                candidates.append(selected)
        artifacts = []
        if pending.snapshot.screenshot is not None:
            try:
                artifacts.append(self._store.put_artifact(pending.snapshot.screenshot, "image/png"))
            except BaseException:
                self._broken = True
                raise
        state = ConcreteState(pending.id + ":snapshot", pending.snapshot.properties,
            pending.snapshot.widgets, "filtered")
        self._emit("observed", Observation(pending.id, self.run_id, pending.sequence,
            pending.snapshot.timestamp_micros, state, candidates, pending.candidate_scope, artifacts))
        self._last_observation = pending.id
        self._pending = None
        if self._pending_result is not None:
            self._emit("action_result", {"execution": self._pending_result, "observation": pending.id})
            self._pending_result = None
        for check in pending.checks:
            self._emit("checked", check)
        return pending.id

    def _define_action(self, definition: ActionDefinition) -> None:
        previous = self._definitions.get(definition.id)
        if previous is not None and previous != definition:
            raise ValueError("Conflicting action definition: " + definition.id)
        if previous is None:
            self._emit("action_defined", definition)
            self._definitions[definition.id] = definition

    def _emit(self, kind: str, data: Any) -> None:
        sequence = self._events + 1
        try:
            if not self._store.record(EvidenceEvent(self.run_id + ":event:" + str(sequence), kind, data)):
                raise RuntimeError("Duplicate recorder event")
        except BaseException:
            self._broken = True
            raise
        self._events = sequence

    def _require_active(self) -> None:
        if get_ident() != self._thread:
            raise RuntimeError("Use the evidence session on its Playwright thread")
        if self._closed or self._finished or self._broken:
            raise RuntimeError("Evidence session has finished or its writer failed")
        if self._busy:
            raise RuntimeError("An evidence operation is already running; wrap individual actions or one outer action")

    def _recording_scope(self):
        runtime = getattr(self._page, "_evidence_runtime", None)
        return nullcontext() if runtime is None else runtime.suspended()

    @staticmethod
    def _password_target(locator: Locator, widget) -> bool:
        if widget is not None and str(widget.properties.get("playwright.attribute.type", "")).lower() == "password":
            return True
        try:
            if locator.count() != 1:
                return True
            return locator.evaluate(
                "element => element instanceof HTMLInputElement && element.type === 'password'", timeout=1_000)
        except PlaywrightError:
            return True

    @staticmethod
    def _action_error(error: BaseException, parameters: dict) -> str:
        if "playwright.redacted_parameters" in parameters:
            return type(error).__name__ + ": action failed; details omitted because its parameters contain a secret"
        return type(error).__name__ + ": " + str(error)


def _trace_directory(parent: Path, sut: str, test_objective: str, sequence: int | None) -> Path:
    def label(value):
        return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")[:32].rstrip("-_") or "trace"

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
    number = "" if sequence is None else f"_sequence-{sequence}"
    name = f"{label(sut)}_{label(test_objective)}{number}_trace-{timestamp}"
    parent.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        directory = parent / (name if suffix == 1 else f"{name}-{suffix}")
        try:
            directory.mkdir()
            return directory
        except FileExistsError:
            suffix += 1

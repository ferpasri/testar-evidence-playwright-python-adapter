"""Turn explicit script operations into a concrete state-action-state trace."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from threading import get_ident
import time
from typing import Any, Callable
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError, Locator, Page
from testar_evidence import (
    UNSPECIFIED, AvailableAction as AvailableActionBinding, ActionDescription, ObservedValue, OracleResult, Oracle, OracleTarget,
    RecordedState, EvidenceEvent, ActionExecution, ActionExecutionResult, JsonlStore, Manifest, Observation, OracleSeverity, TestRun, TestSequence,
)

from ._metadata import VERSION as VERSION, run_properties
from .capture import BrowserSnapshot, CaptureOptions, capture_page, identity, target_path
from .compound import CompoundOperations
from .checks import PlaywrightChecks
from testar_evidence.codec import dumps


@dataclass(frozen=True)
class AvailableAction:
    """An available action reported by a caller, including its planning parameters."""
    action_type: str
    name: str
    locator: Locator | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    def description(self) -> ActionDescription:
        description = self.name if self.locator is None else str(self.locator)
        parameters = EvidenceEvent("validation:event", "action_described",
            ActionDescription("validation:action", self.action_type, self.parameters)).to_dict()["event"]["data"]["properties"]
        return ActionDescription("playwright:available:" + identity(dumps(
            [self.action_type, self.name, description, parameters], sort_keys=True)), self.action_type,
            {"playwright.description": self.name, "playwright.locator": description,
             "playwright.available_parameters": parameters})


@dataclass
class _PendingObservation:
    id: str
    observation_index: int
    snapshot: BrowserSnapshot
    oracle_results: list[OracleResult] = field(default_factory=list)
    available_actions: list[AvailableActionBinding] | None = None
    available_action_scope: str = "unknown"
    artifacts: list[dict] = field(default_factory=list)
    capture: str = "filtered"


class PlaywrightEvidence:
    """Record wrapped actions and test oracles; the caller owns the page and browser."""

    def __init__(self, page: Page, options: CaptureOptions, store: JsonlStore, run_id: str):
        self._page = page
        self._options = options
        self._store = store
        self._run_id = run_id
        self._sequence_id = run_id + ":sequence:1"
        self._thread = get_ident()
        self._checks = PlaywrightChecks(self)
        self._descriptions = {}
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
              test_objective: str | None = None, sequence_index: int = 1) -> PlaywrightEvidence:
        """Create a new trace at the exact supplied directory, preserving existing files."""
        if page is None:
            raise TypeError("A synchronous Playwright page is required")
        options = CaptureOptions() if options is None else options
        if not isinstance(options, CaptureOptions):
            raise TypeError("options must be CaptureOptions")
        if type(sequence_index) is not int or not 1 <= sequence_index < (1 << 64):
            raise ValueError("Sequence index must be a positive unsigned 64-bit integer")
        supplied = run_properties(properties, origin=origin, test_objective=test_objective)
        supplied["playwright.initial_url"] = page.url
        identifier = uuid4().hex
        run_id = "playwright-python:run:" + identifier
        run = TestRun(run_id, "playwright-python", identifier, supplied)
        EvidenceEvent("validation:event", "test_run_started", run).to_dict()
        store = JsonlStore.create(directory, Manifest("playwright-python:evidence:" + identifier))
        evidence = cls(page, options, store, run_id)
        try:
            evidence._emit("test_run_started", run)
            evidence._emit("test_sequence_started", TestSequence(evidence.sequence_id, run_id,
                sequence_index, str(sequence_index)))
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
        if sequence is not None and (type(sequence) is not int or not 1 <= sequence < (1 << 64)):
            raise ValueError("Sequence must be a positive integer")
        supplied = run_properties(properties, origin=origin, test_objective=test_objective)
        directory = _trace_directory(Path(parent), sut, supplied.get("evidence.test_objective", "run"), sequence)
        supplied["playwright.sut"] = sut
        if sequence is not None:
            supplied["playwright.sequence"] = sequence
        return cls.start(directory, page, options, supplied, sequence_index=sequence or 1)

    @property
    def directory(self) -> Path:
        return self._store.directory

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def sequence_id(self) -> str:
        return self._sequence_id

    @property
    def buffered_observation(self) -> str | None:
        """Return the capture ID available for reuse, or None once it has been written."""
        self._require_active()
        return None if self._pending is None else self._pending.id

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

    def record_available_actions(self, actions: list[AvailableAction], *, scope: str = "generated",
                                 observation: str | None = None) -> str:
        """Report available actions, capturing afresh unless a buffered observation ID is supplied."""
        self._require_active()
        if self._selected_execution is not None:
            raise RuntimeError("Report the selected action's outcome before recording available actions")
        if scope not in {"generated", "eligible", "unknown"}:
            raise ValueError("Available-action scope must be generated, eligible, or unknown")
        if not isinstance(actions, list) or not all(isinstance(action, AvailableAction) for action in actions):
            raise TypeError("Actions must be a list of AvailableAction values")
        if observation is not None:
            self._require_buffered_observation(observation)
        description_records = [action.description() for action in actions]
        self._busy = True
        try:
            if observation is None and self._pending is not None:
                self._flush()
            self._ensure_pending()
            available_actions = []
            resolved = {}
            with self._recording_scope():
                for action, description_record in zip(actions, description_records):
                    description = None if action.locator is None else str(action.locator)
                    if description not in resolved:
                        widget = self._pending.snapshot.widget_at(target_path(action.locator))
                        resolved[description] = None if widget is None else widget.id
                    available_action = AvailableActionBinding(description_record.id, resolved[description])
                    if available_action not in available_actions:
                        available_actions.append(available_action)
            for description_record in description_records:
                self._describe_action(description_record)
            self._pending.available_actions = available_actions
            self._pending.available_action_scope = scope
            return self._pending.id
        finally:
            self._busy = False

    def select_action(self, action_type: str, name: str, locator: Locator | None,
                      parameters: dict[str, Any], *, available_action: AvailableAction | None = None,
                      source_observation: str | None = None) -> str:
        """Record selection, optionally requiring a particular buffered source observation."""
        self._require_active()
        if self._selected_execution is not None:
            raise RuntimeError("Report the selected action's outcome before selecting another")
        if source_observation is not None:
            self._require_buffered_observation(source_observation)
        description = name if locator is None else str(locator)
        description_record = ActionDescription("playwright:action:" + identity(json.dumps([action_type, name, description])),
            action_type, {"playwright.description": name, "playwright.locator": description})
        if available_action is not None:
            if not isinstance(available_action, AvailableAction):
                raise TypeError("available_action must be AvailableAction")
            if (action_type, name, description) != (available_action.action_type, available_action.name,
                    available_action.name if available_action.locator is None else str(available_action.locator)):
                raise ValueError("Selected action must match its available-action description")
            description_record = available_action.description()
        EvidenceEvent("validation:event", "action_described", description_record).to_dict()
        # Validate and copy arguments before any action can affect the application.
        prepared = EvidenceEvent("validation:event", "action_selected",
            ActionExecution("validation:execution", self.sequence_id, "validation:observation", description_record.id,
                parameters=parameters)).to_dict()["event"]["data"]["parameters"]
        self._busy = True
        try:
            self._ensure_pending()
            with self._recording_scope():
                widget = self._pending.snapshot.widget_at(target_path(locator))
            if available_action is not None and self._pending.available_actions is not None:
                binding = next((item for item in self._pending.available_actions if item.action_description == description_record.id), None)
                if binding is None:
                    raise ValueError("Selected action was not reported for this observation")
                widget = next((item for item in self._pending.snapshot.widgets if item.id == binding.target_widget), None)
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
            source = self._flush(description_record, None if widget is None else widget.id)
            self._executions += 1
            execution = self.run_id + ":execution:" + str(self._executions)
            self._emit("action_selected", ActionExecution(execution, self.sequence_id, source, description_record.id,
                None if widget is None else widget.id, prepared))
            self._selected_execution = execution
            self._selected_parameters = prepared
            return execution
        finally:
            self._busy = False

    def report_action_result(self, outcome: str, error: str | None = None) -> None:
        """Report whether the selected operation returned normally or raised an error."""
        self._require_active()
        self._report_action_result(outcome, error)

    def _report_action_result(self, outcome: str, error: str | None = None) -> None:
        execution = self._selected_execution
        if execution is None:
            raise RuntimeError("No selected action is awaiting an outcome")
        result = ActionExecutionResult(execution, outcome, error)
        EvidenceEvent("validation:event", "action_execution_reported", result).to_dict()
        self._emit("action_execution_reported", result)
        self._selected_execution = None
        self._selected_parameters = None
        if outcome == "returned":
            self._pending_result = execution
        else:
            self._ending_reason = "action_failed"

    def perform(self, action_type: str, name: str, locator: Locator | None,
                parameters: dict[str, Any], operation: Callable[[], Any], *,
                available_action: AvailableAction | None = None,
                source_observation: str | None = None, record_operations: bool = False) -> Any:
        """Record one action; opt-in native operation details include a buffered result capture."""
        if not callable(operation):
            raise TypeError("Action operation must be callable")
        if type(record_operations) is not bool:
            raise TypeError("record_operations must be a boolean")
        runtime = getattr(self._page, "_evidence_runtime", None)
        if record_operations and (runtime is None or runtime.is_suspended):
            raise ValueError("Native operation recording requires an active facade page outside automatic recording")
        if record_operations:
            parameters = {**parameters, "playwright.compound_action": True}
        execution = self.select_action(action_type, name, locator, parameters, available_action=available_action,
                                       source_observation=source_observation)
        operations = CompoundOperations(lambda target: target is None or self._password_target(target, None),
            "playwright.redacted_parameters" in self._selected_parameters) if record_operations else None
        self._busy = True
        try:
            try:
                scope = self._recording_scope() if operations is None else runtime.collect_operations(
                    self._page, operations.invoke)
                with scope:
                    result = operation()
            except BaseException as error:
                self._ending_reason = "action_failed"
                try:
                    error_parameters = self._selected_parameters
                    if operations is not None and operations.has_secrets:
                        error_parameters = {"playwright.redacted_parameters": ["operations"]}
                    self._report_action_result("error", self._action_error(error, error_parameters))
                    if operations is not None:
                        self._capture_compound_result(operations.report(execution))
                except Exception as recording:
                    error.add_note("Evidence recording also failed: " + str(recording))
                raise
            self._report_action_result("returned")
            if operations is not None:
                self._capture_compound_result(operations.report(execution))
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
            raise ValueError("TestRun-ending reason must be nonblank text")
        if type(capture_final) is not bool:
            raise TypeError("capture_final must be a boolean")
        if self._finished:
            return
        self._require_active()
        EvidenceEvent("validation:event", "test_run_finished", {"test_run": self.run_id, "reason": reason}).to_dict()
        self._busy = True
        try:
            if self._pending is None and capture_final and \
                    (self._pending_result is not None or self._last_observation is None):
                self._ensure_pending()
            if self._pending is not None:
                self._flush()
            self._emit("test_sequence_finished", {"test_sequence": self.sequence_id, "reason": reason})
            self._emit("test_run_finished", {"test_run": self.run_id, "reason": reason})
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
                self.report_action_result("error", self._action_error(exception, self._selected_parameters))
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

    def _record_oracle_result(self, name: str, oracle: Oracle, locator: Locator | None,
                      property: str | None, expected: Any, status: str,
                      actual: ObservedValue, details: dict[str, Any], *, reuse_capture: bool = False,
                      oracle_severity: OracleSeverity | Any = UNSPECIFIED) -> None:
        if status in {"failed", "error"}:
            self._ending_reason = "check_failed"
        if self._pending is not None and not reuse_capture:
            self._flush()
        self._ensure_pending()
        with self._recording_scope():
            widget = self._pending.snapshot.widget_at(target_path(locator))
        details = dict(details)
        if locator is None:
            target = OracleTarget.observation(self._pending.id) if property is None else \
                OracleTarget.state_property(self._pending.id, property)
        elif widget is not None:
            target = OracleTarget.widget(self._pending.id, widget.id) if property is None else \
                OracleTarget.widget_property(self._pending.id, widget.id, property)
        else:
            target = OracleTarget.observation(self._pending.id)
            details["playwright.requested_target"] = {"locator": str(locator), "property": property or ""}
            details["playwright.target_resolution"] = "The locator did not identify a captured top-document widget."
        check = OracleResult(self.run_id, name, oracle, target, status, test_sequence=self.sequence_id,
            expected_value=expected, observed_value=actual, oracle_details=details, oracle_severity=oracle_severity)
        EvidenceEvent("validation:event", "oracle_result_recorded", check).to_dict()
        self._pending.oracle_results.append(check)

    def _require_buffered_observation(self, observation: str) -> None:
        if not isinstance(observation, str):
            raise TypeError("Observation reference must be a string")
        if self._pending is None or observation != self._pending.id:
            raise ValueError("Observation must be the current buffered capture from this recorder")

    def _capture_compound_result(self, report) -> None:
        try:
            artifact = self._store.put_artifact((dumps(report) + "\n").encode("utf-8"), "application/json")
        except BaseException:
            self._broken = True
            raise
        self._ensure_pending(allow_unavailable=True)
        self._pending.artifacts.append(artifact)

    def _ensure_pending(self, *, allow_unavailable: bool = False) -> None:
        if self._pending is None:
            capture = "filtered"
            with self._recording_scope():
                try:
                    snapshot = capture_page(self._page, self._options)
                except PlaywrightError as error:
                    if not allow_unavailable:
                        raise
                    snapshot = BrowserSnapshot(time.time_ns() // 1_000,
                        {"playwright.capture_error": type(error).__name__}, [], None)
                    capture = "unavailable"
            self._observations += 1
            self._pending = _PendingObservation(self.run_id + ":observation:" + str(self._observations),
                self._observations, snapshot, capture=capture)

    def _flush(self, description_record: ActionDescription | None = None, target: str | None = None) -> str:
        pending = self._pending
        available_actions = [] if pending.available_actions is None else list(pending.available_actions)
        if description_record is not None:
            self._describe_action(description_record)
            selected = AvailableActionBinding(description_record.id, target)
            if selected not in available_actions:
                available_actions.append(selected)
        artifacts = list(pending.artifacts)
        if pending.snapshot.screenshot is not None:
            try:
                artifacts.append(self._store.put_artifact(pending.snapshot.screenshot, "image/png"))
            except BaseException:
                self._broken = True
                raise
        state = RecordedState(pending.id + ":state", pending.snapshot.properties,
            pending.snapshot.widgets, pending.capture)
        self._emit("observation_recorded", Observation(pending.id, self.sequence_id, pending.observation_index,
            pending.snapshot.timestamp_micros, state, pending.available_action_scope, available_actions, artifacts))
        self._last_observation = pending.id
        self._pending = None
        if self._pending_result is not None:
            self._emit("transition_recorded", {"action_execution": self._pending_result, "resulting_observation": pending.id})
            self._pending_result = None
        for check in pending.oracle_results:
            self._emit("oracle_result_recorded", check)
        return pending.id

    def _describe_action(self, description_record: ActionDescription) -> None:
        previous = self._descriptions.get(description_record.id)
        if previous is not None and previous != description_record:
            raise ValueError("Conflicting action description: " + description_record.id)
        if previous is None:
            self._emit("action_described", description_record)
            self._descriptions[description_record.id] = description_record

    def _emit(self, event_type: str, data: Any) -> None:
        sequence = self._events + 1
        try:
            if not self._store.record(EvidenceEvent(self.run_id + ":event:" + str(sequence), event_type, data)):
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

"""Turn explicit script operations into a concrete state-action-state trace."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import get_ident
from typing import Any, Callable
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError, Locator, Page
from testar_evidence import (
    ActionCandidate, ActionDefinition, CheckReading, CheckResult, CheckRule, CheckTarget,
    ConcreteState, EvidenceEvent, Execution, JsonlStore, Manifest, Observation, Run,
)

from .capture import BrowserSnapshot, CaptureOptions, capture_page, identity, target_path
from .checks import PlaywrightChecks

VERSION = "0.2.1"


@dataclass
class _PendingObservation:
    id: str
    sequence: int
    snapshot: BrowserSnapshot
    checks: list[CheckResult] = field(default_factory=list)


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
        self._last_observation = None
        self._ending_reason = "closed"
        self._finished = False
        self._closed = False
        self._broken = False
        self._busy = False

    @classmethod
    def start(cls, directory: str | Path, page: Page, options: CaptureOptions | None = None,
              properties: dict[str, Any] | None = None) -> PlaywrightEvidence:
        """Create a new trace at the exact supplied directory, preserving existing files."""
        if page is None:
            raise TypeError("A synchronous Playwright page is required")
        options = CaptureOptions() if options is None else options
        if not isinstance(options, CaptureOptions):
            raise TypeError("options must be CaptureOptions")
        if properties is not None and not isinstance(properties, dict):
            raise TypeError("Run properties must be a dictionary")
        run_properties = dict(properties or {})
        run_properties.update({"playwright.adapter_version": VERSION, "playwright.initial_url": page.url})
        identifier = uuid4().hex
        run_id = "playwright-python:run:" + identifier
        run = Run(run_id, "playwright-python", identifier, run_properties)
        EvidenceEvent("validation:event", "run_started", run).to_dict()
        store = JsonlStore.create(directory, Manifest("playwright-python:model:" + identifier))
        evidence = cls(page, options, store, run_id)
        try:
            evidence._emit("run_started", run)
        except BaseException:
            store.close()
            raise
        return evidence

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

    def perform(self, kind: str, name: str, locator: Locator | None,
                parameters: dict[str, Any], operation: Callable[[], Any]) -> Any:
        """Record one action, its actual arguments, and its returned or raised outcome."""
        self._require_active()
        if not callable(operation):
            raise TypeError("Action operation must be callable")
        description = name if locator is None else str(locator)
        definition = ActionDefinition("playwright:action:" + identity(json.dumps([kind, name, description])),
            kind, {"playwright.description": name, "playwright.locator": description})
        EvidenceEvent("validation:event", "action_defined", definition).to_dict()
        # Validate and copy arguments before any action can affect the application.
        prepared = EvidenceEvent("validation:event", "action_selected",
            Execution("validation:execution", self.run_id, "validation:observation", definition.id,
                parameters=parameters)).to_dict()["event"]["data"]["parameters"]
        self._busy = True
        try:
            self._ensure_pending()
            widget = self._pending.snapshot.widget_at(target_path(locator))
            prepared["playwright.origin"] = "scripted"
            if locator is not None:
                prepared["playwright.locator"] = description
                prepared["playwright.target_resolution"] = "not_captured" if widget is None else "captured_dom_path"
                if "value" in prepared and self._password_target(locator, widget):
                    prepared.pop("value", None)
                    prepared["playwright.redacted_parameters"] = ["value"]
            source = self._flush(definition, None if widget is None else widget.id)
            self._executions += 1
            execution = self.run_id + ":execution:" + str(self._executions)
            self._emit("action_selected", Execution(execution, self.run_id, source, definition.id,
                None if widget is None else widget.id, prepared))
            try:
                result = operation()
            except BaseException as error:
                self._ending_reason = "action_failed"
                try:
                    self._emit("action_submitted", {"execution": execution, "accepted": False,
                        "error": self._action_error(error, prepared)})
                except Exception as recording:
                    error.add_note("Evidence recording also failed: " + str(recording))
                raise
            self._emit("action_submitted", {"execution": execution, "accepted": True, "error": None})
            self._pending_result = execution
            return result
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

    def finish(self, reason: str = "completed") -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Run-ending reason must be nonblank text")
        if self._finished:
            return
        self._require_active()
        EvidenceEvent("validation:event", "run_finished", {"run": self.run_id, "reason": reason}).to_dict()
        self._busy = True
        try:
            if self._pending is not None or self._pending_result is not None or self._last_observation is None:
                self._ensure_pending()
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
                      actual: CheckReading, details: dict[str, Any]) -> None:
        if status in {"failed", "error"}:
            self._ending_reason = "check_failed"
        if self._pending is not None:
            self._flush()
        self._ensure_pending()
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
            snapshot = capture_page(self._page, self._options)
            self._observations += 1
            self._pending = _PendingObservation(self.run_id + ":observation:" + str(self._observations),
                self._observations, snapshot)

    def _flush(self, definition: ActionDefinition | None = None, target: str | None = None) -> str:
        pending = self._pending
        candidates = []
        if definition is not None:
            previous = self._definitions.get(definition.id)
            if previous is not None and previous != definition:
                raise ValueError("Conflicting action definition: " + definition.id)
            if previous is None:
                self._emit("action_defined", definition)
                self._definitions[definition.id] = definition
            candidates = [ActionCandidate(definition.id, target)]
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
            pending.snapshot.timestamp_micros, state, candidates, "unknown", artifacts))
        self._last_observation = pending.id
        self._pending = None
        if self._pending_result is not None:
            self._emit("action_result", {"execution": self._pending_result, "observation": pending.id})
            self._pending_result = None
        for check in pending.checks:
            self._emit("checked", check)
        return pending.id

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

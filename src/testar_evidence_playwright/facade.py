"""Record synchronous Playwright operations through their native public objects."""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import inspect
from pathlib import Path

from playwright import sync_api as native

from ._metadata import run_properties
from .capture import CaptureOptions
from .evidence import PlaywrightEvidence
from .facade_actions import describe_action


def sync_playwright(*, evidence_path: str | Path = "target/traces", sut: str = "application",
                    test_objective: str | None = None, capture_options: CaptureOptions | None = None,
                    properties: dict | None = None, origin: str | None = None):
    """Configure automatic evidence once, then use the synchronous Playwright API."""
    return _Manager(evidence_path, sut, test_objective, capture_options, properties, origin)


def evidence_for(page: native.Page) -> PlaywrightEvidence:
    """Access this facade page's recorder for additional actions or custom checks."""
    runtime = getattr(page, "_evidence_runtime", None)
    if not isinstance(page, native.Page) or runtime is None:
        raise ValueError("Use a page created by the evidence facade")
    recorder = runtime.recorder_for(page)
    if recorder is None:
        raise RuntimeError("This page's evidence trace has finished")
    return recorder


class _Manager:
    def __init__(self, directory, sut, test_objective, options, properties, origin):
        if not isinstance(sut, str) or not sut.strip():
            raise ValueError("SUT label must be a nonblank string")
        if options is not None and not isinstance(options, CaptureOptions):
            raise TypeError("capture_options must be CaptureOptions")
        supplied = run_properties(properties, origin=origin, test_objective=test_objective)
        self._native = native.sync_playwright()
        self._runtime = _Runtime(Path(directory), sut, test_objective, options, supplied, origin)
        self._started = False
        self._stopped = False

    def __enter__(self):
        if self._started:
            raise RuntimeError("This Playwright manager has already been started")
        self._started = True
        playwright = self._native.__enter__()
        self._runtime.attach(playwright)
        # Native Playwright also installs stop on this individual instance.
        playwright.stop = self.stop
        return playwright

    def start(self):
        return self.__enter__()

    def stop(self):
        self.__exit__(None, None, None)

    def __exit__(self, exception_type, exception, traceback):
        if self._stopped:
            return
        self._stopped = True
        cleanup_error = None
        try:
            self._runtime.finish_all("script_failed" if exception is not None else "completed")
        except Exception as error:
            cleanup_error = error
        try:
            self._native.__exit__(exception_type, exception, traceback)
        except Exception as error:
            if cleanup_error is None:
                cleanup_error = error
            else:
                cleanup_error.add_note("Playwright cleanup also failed: " + str(error))
        if cleanup_error is not None:
            if exception is None:
                raise cleanup_error
            exception.add_note("Evidence cleanup also failed: " + str(cleanup_error))


class _Runtime:
    def __init__(self, directory, sut, test_objective, options, properties, origin):
        self.directory = directory
        self.sut = sut
        self.test_objective = test_objective
        self.options = options
        self.properties = properties
        self.origin = origin
        self._suspensions = 0
        self._recorders = {}
        self._finished = set()
        self._closing = set()
        self._callbacks = []
        self._classes = {}
        self._callback_actions_allowed = False
        self._compound = None

    @property
    def is_suspended(self):
        return self._suspensions > 0

    @contextmanager
    def suspended(self):
        self._suspensions += 1
        try:
            yield
        finally:
            self._suspensions -= 1

    @contextmanager
    def collect_operations(self, page, callback):
        if self._compound is not None or self.is_suspended:
            raise RuntimeError("Compound operation recording cannot be nested")
        self._compound = (page, callback)
        try:
            with self.suspended():
                yield
        finally:
            self._compound = None

    def page_for(self, subject):
        if isinstance(subject, native.Page):
            return subject
        page = getattr(subject, "_evidence_page", None)
        if page is None and isinstance(subject, (native.Locator, native.Frame)):
            with self.suspended():
                page = object.__getattribute__(subject, "page")
        return page

    def recorder_for(self, subject):
        page = self.page_for(subject)
        if page is None or page in self._finished:
            return None
        if page not in self._recorders:
            self.attach(page)
        recorder = self._recorders[page]
        if recorder is None and not page.is_closed():
            with self.suspended():
                recorder = PlaywrightEvidence.start_trace(self.directory, page, sut=self.sut,
                    test_objective=self.test_objective, sequence=list(self._recorders).index(page) + 1,
                    options=self.options, properties=self.properties, origin=self.origin)
            self._recorders[page] = recorder
        return recorder

    def attach(self, value, page=None, visited=None):
        if isinstance(value, (list, tuple, dict)):
            visited = set() if visited is None else visited
            if id(value) in visited:
                return value
            visited.add(id(value))
            if isinstance(value, list):
                for index, item in enumerate(value):
                    value[index] = self.attach(item, page, visited)
            elif isinstance(value, dict):
                for key, item in value.items():
                    value[key] = self.attach(item, page, visited)
            else:
                return tuple(self.attach(item, page, visited) for item in value)
            return value
        base = type(value)
        module = base.__module__
        if not module.startswith("playwright.") or not hasattr(value, "__dict__"):
            return value
        if getattr(value, "_evidence_runtime", None) is self:
            return value
        if isinstance(value, native.Page):
            page = value
        elif isinstance(value, (native.Locator, native.Frame)):
            page = object.__getattribute__(value, "page")
        # Per-instance subclasses retain native isinstance checks and argument mapping.
        instrumented = self._classes.get(base)
        if instrumented is None:
            members = {"__slots__": (), "__getattribute__": _getattribute,
                "__module__": __name__}
            if hasattr(base, "__enter__"):
                def enter(subject, _enter=base.__enter__):
                    runtime = object.__getattribute__(subject, "_evidence_runtime")
                    return runtime.attach(_enter(subject), runtime.page_for(subject))
                members["__enter__"] = enter
            if hasattr(base, "__exit__"):
                def leave(subject, *arguments, _exit=base.__exit__):
                    return _exit(subject, *arguments)
                members["__exit__"] = leave
            instrumented = type("Evidence" + base.__name__, (base,), members)
            self._classes[base] = instrumented
        value.__class__ = instrumented
        object.__setattr__(value, "_evidence_runtime", self)
        object.__setattr__(value, "_evidence_page", page)
        if isinstance(value, native.Page):
            self._recorders[value] = None
            register = object.__getattribute__(value, "on")
            register("close", lambda *arguments: self._page_ended(value))
            register("crash", lambda *arguments: self._page_ended(value))
        return value

    def _page_ended(self, page):
        self._closing.add(page)
        if not self.is_suspended:
            self._drain_closed()

    def _drain_closed(self):
        for page in list(self._closing):
            self._closing.discard(page)
            self.finish_page(page, "page_closed", capture_final=False)

    def finish_page(self, page, reason="completed", *, capture_final=True):
        if page in self._finished:
            return
        try:
            with self.suspended():
                recorder = self.recorder_for(page) if capture_final else self._recorders.get(page)
                if recorder is not None:
                    try:
                        ending = recorder._ending_reason
                        recorder.finish(reason if ending == "closed" else ending,
                            capture_final=capture_final and not page.is_closed())
                    except BaseException as error:
                        try:
                            recorder.close()
                        except Exception as cleanup:
                            error.add_note("Evidence cleanup also failed: " + str(cleanup))
                        raise
                    recorder.close()
        finally:
            self._finished.add(page)

    def finish_all(self, reason="completed"):
        failures = []
        for page in list(self._recorders):
            try:
                self.finish_page(page, reason)
            except Exception as error:
                failures.append(error)
        if failures:
            for error in failures[1:]:
                failures[0].add_note("Another evidence trace failed to close: " + str(error))
            raise failures[0]

    def _finish_subject(self, subject):
        if isinstance(subject, native.Page):
            pages = [subject]
        elif isinstance(subject, native.BrowserContext):
            pages = subject.pages
        elif isinstance(subject, native.Browser):
            pages = [page for context in subject.contexts for page in context.pages]
        else:
            pages = []
        for page in pages:
            self.finish_page(self.attach(page))

    def _callback(self, original, page):
        for callback, wrapped in self._callbacks:
            if callback == original:
                return wrapped

        @wraps(original)
        def callback(*arguments, **options):
            suspensions = self._suspensions
            if self._callback_actions_allowed:
                self._suspensions = 0
            try:
                return original(*(self.attach(value, page) for value in arguments),
                    **{name: self.attach(value, page) for name, value in options.items()})
            finally:
                self._suspensions = suspensions

        self._callbacks.append((original, callback))
        return callback

    def _arguments(self, method, arguments, options, page):
        try:
            supplied = inspect.signature(method).bind_partial(*arguments, **options)
        except (TypeError, ValueError):
            return arguments, options
        for name in ("handler", "callback", "predicate", "f"):
            value = supplied.arguments.get(name)
            if callable(value):
                supplied.arguments[name] = self._callback(value, page)
        return supplied.args, supplied.kwargs

    def invoke(self, subject, name, method, arguments, options):
        page = self.page_for(subject)
        if self.is_suspended:
            if self._compound is not None and self._suspensions == 1:
                with self.suspended():
                    description = describe_action(subject, page, name, method, arguments, options) if page else None
                    if description is not None:
                        if page is not self._compound[0]:
                            raise ValueError("Compound operations must use the recorder's page")
                        result = self._compound[1](description, lambda: method(*arguments, **options))
                        return self.attach(result, page)
            return self.attach(method(*arguments, **options), page)
        with self.suspended():
            arguments, options = self._arguments(method, arguments, options, page)
            cleanup_error = None
            if name == "close":
                try:
                    self._finish_subject(subject)
                except Exception as error:
                    cleanup_error = error
            description = describe_action(subject, page, name, method, arguments, options) if page else None
            recorder = self.recorder_for(subject) if description is not None else None
            previous_callbacks_allowed = self._callback_actions_allowed
            self._callback_actions_allowed = recorder is None and name != "close"
            try:
                if recorder is None:
                    result = method(*arguments, **options)
                else:
                    result = recorder.perform(**description, operation=lambda: method(*arguments, **options))
                    if not page.is_closed():
                        recorder.capture()
                if cleanup_error is not None:
                    raise cleanup_error
                result = self.attach(result, page)
                self._drain_closed()
                return result
            except BaseException as error:
                if cleanup_error is not None and cleanup_error is not error:
                    error.add_note("Evidence cleanup also failed: " + str(cleanup_error))
                try:
                    self._drain_closed()
                except Exception as recording:
                    error.add_note("Evidence cleanup also failed: " + str(recording))
                raise
            finally:
                self._callback_actions_allowed = previous_callbacks_allowed


def _getattribute(subject, name):
    member = object.__getattribute__(subject, name)
    if name.startswith("_"):
        return member
    runtime = object.__getattribute__(subject, "_evidence_runtime")
    if callable(member):
        @wraps(member)
        def operation(*arguments, **options):
            return runtime.invoke(subject, name, member, arguments, options)
        return operation
    return runtime.attach(member, runtime.page_for(subject))

"""Prepare optional run descriptions and the adapter's own identity."""

from typing import Any

ADAPTER_ID = "testar-evidence-playwright-python-adapter"
VERSION = "0.3.0"


def run_properties(properties: dict[str, Any] | None = None, *, origin: str | None = None,
                   test_objective: str | None = None) -> dict[str, Any]:
    if properties is not None and not isinstance(properties, dict):
        raise TypeError("TestRun properties must be a dictionary")
    prepared = dict(properties or {})
    descriptions = {"evidence.origin": origin, "evidence.test_objective": test_objective}
    for key, value in descriptions.items():
        if value is not None:
            if key in prepared and prepared[key] != value:
                raise ValueError("Conflicting run description: " + key)
            prepared[key] = value
        if key in prepared and (not isinstance(prepared[key], str) or not prepared[key].strip()):
            raise ValueError("TestRun descriptions must be nonblank strings: " + key)
    for key, value in {"evidence.adapter": ADAPTER_ID, "evidence.adapter_version": VERSION}.items():
        if key in prepared and prepared[key] != value:
            raise ValueError("Adapter identity is supplied automatically: " + key)
        prepared[key] = value
    return prepared

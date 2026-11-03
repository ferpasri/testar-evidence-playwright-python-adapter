"""Keep native operation details inside an explicitly recorded compound action."""
from typing import Callable


class CompoundOperations:
    def __init__(self, is_secret: Callable, redact_all: bool):
        self.operations = []
        self._is_secret = is_secret
        self._redact_all = redact_all
        self.has_secrets = redact_all

    def invoke(self, description, operation):
        parameters = dict(description["parameters"])
        sensitive = {"value", "text", "key", "playwright.positional_arguments",
                     "playwright.keyword_arguments"}.intersection(parameters)
        if sensitive and (self._redact_all or self._is_secret(description["locator"])):
            self.has_secrets = True
            for name in sensitive:
                parameters.pop(name)
            parameters["playwright.redacted_parameters"] = sorted(sensitive)
        item = {"index": len(self.operations) + 1, "method": description["name"],
                "action_type": description["action_type"],
                "locator": None if description["locator"] is None else str(description["locator"]),
                "parameters": parameters, "outcome": "returned", "error": None}
        self.operations.append(item)
        try:
            return operation()
        except BaseException as error:
            item["outcome"] = "error"
            item["error"] = type(error).__name__ + (
                ": details omitted because the operation concerns a secret value" if self.has_secrets
                else ": " + str(error))
            raise

    def report(self, action_execution):
        return {"format": "compound-action-operations", "version": 1,
                "action_execution": action_execution, "operations": self.operations}

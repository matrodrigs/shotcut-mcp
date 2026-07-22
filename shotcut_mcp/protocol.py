"""Small JSON Schema subset used to enforce the published MCP tool contracts."""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


_CANCELLATION_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "shotcut_mcp_cancellation_event", default=None
)


@contextmanager
def request_cancellation(event: threading.Event):
    token = _CANCELLATION_EVENT.set(event)
    try:
        yield
    finally:
        _CANCELLATION_EVENT.reset(token)


def cancellation_requested() -> bool:
    event = _CANCELLATION_EVENT.get()
    return event is not None and event.is_set()


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    if expected and not any(
        isinstance(item, str) and _matches_type(value, item)
        for item in expected_types
    ):
        errors.append(f"{path} must be of type {expected}.")
        return errors

    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        errors.append(f"{path} must be one of {allowed!r}.")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in value:
                    errors.append(f"{path}.{name} is required.")
        if isinstance(properties, dict):
            for name, item in value.items():
                child_schema = properties.get(name)
                if isinstance(child_schema, dict):
                    errors.extend(schema_errors(item, child_schema, f"{path}.{name}"))
                elif schema.get("additionalProperties") is False:
                    errors.append(f"{path}.{name} is not allowed.")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{path} must contain at least {minimum_items} items.")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{path} must contain at most {maximum_items} items.")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(schema_errors(item, item_schema, f"{path}[{index}]"))

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path} does not match the required pattern.")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path} must be at least {minimum}.")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path} must be at most {maximum}.")

    return errors

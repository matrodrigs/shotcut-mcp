"""Small JSON Schema subset used to enforce the published MCP tool contracts."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CANCELLATION_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "shotcut_mcp_cancellation_event", default=None
)
ProgressCallback = Callable[[float, float | None, str | None], None]


class _ProgressState:
    """Keep one request's optional MCP progress stream monotonic."""

    def __init__(self, callback: ProgressCallback) -> None:
        self.callback = callback
        self.last = -math.inf

    def report(self, progress: float, total: float | None, message: str | None) -> None:
        if not math.isfinite(progress) or progress < 0:
            raise ValueError("Progress values must be finite and non-negative.")
        if total is not None and (
            not math.isfinite(total) or total <= 0 or progress > total
        ):
            raise ValueError("Progress total must be finite and at least progress.")
        if message is not None and not isinstance(message, str):
            raise ValueError("Progress message must be a string.")
        if progress <= self.last:
            return
        self.last = progress
        self.callback(progress, total, message[:500] if message else None)


_PROGRESS_STATE: ContextVar[_ProgressState | None] = ContextVar(
    "shotcut_mcp_progress_state", default=None
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


@contextmanager
def request_progress(callback: ProgressCallback | None):
    """Install an optional request-local MCP progress sink."""

    token = _PROGRESS_STATE.set(_ProgressState(callback) if callback else None)
    try:
        yield
    finally:
        _PROGRESS_STATE.reset(token)


def report_progress(
    progress: int | float,
    total: int | float | None = None,
    message: str | None = None,
) -> bool:
    """Emit progress when the caller supplied a progress token."""

    state = _PROGRESS_STATE.get()
    if state is None:
        return False
    numeric_progress = float(progress)
    numeric_total = float(total) if total is not None else None
    state.report(numeric_progress, numeric_total, message)
    return True


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


def _object_schema_errors(
    value: dict[Any, Any], schema: dict[str, Any], path: str
) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    minimum_properties = schema.get("minProperties")
    maximum_properties = schema.get("maxProperties")
    if isinstance(minimum_properties, int) and len(value) < minimum_properties:
        errors.append(f"{path} must contain at least {minimum_properties} properties.")
    if isinstance(maximum_properties, int) and len(value) > maximum_properties:
        errors.append(f"{path} must contain at most {maximum_properties} properties.")
    if isinstance(required, list):
        errors.extend(
            f"{path}.{name} is required."
            for name in required
            if isinstance(name, str) and name not in value
        )
    property_names = schema.get("propertyNames")
    if isinstance(properties, dict):
        for name, item in value.items():
            if isinstance(property_names, dict):
                errors.extend(
                    schema_errors(
                        name,
                        property_names,
                        f"{path} property {name!r}",
                    )
                )
            child_schema = properties.get(name)
            if isinstance(child_schema, dict):
                errors.extend(schema_errors(item, child_schema, f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{name} is not allowed.")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(
                    schema_errors(
                        item,
                        schema["additionalProperties"],
                        f"{path}.{name}",
                    )
                )
    return errors


def _array_schema_errors(
    value: list[Any], schema: dict[str, Any], path: str
) -> list[str]:
    errors: list[str] = []
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
    return errors


def _string_schema_errors(value: str, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    minimum_length = schema.get("minLength")
    maximum_length = schema.get("maxLength")
    if isinstance(minimum_length, int) and len(value) < minimum_length:
        errors.append(f"{path} must contain at least {minimum_length} characters.")
    if isinstance(maximum_length, int) and len(value) > maximum_length:
        errors.append(f"{path} must contain at most {maximum_length} characters.")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, value) is None:
        errors.append(f"{path} does not match the required pattern.")
    return errors


def _number_schema_errors(
    value: int | float, schema: dict[str, Any], path: str
) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, (int, float)) and value < minimum:
        errors.append(f"{path} must be at least {minimum}.")
    if isinstance(maximum, (int, float)) and value > maximum:
        errors.append(f"{path} must be at most {maximum}.")
    return errors


def _alternative_errors(value: Any, alternatives: object, path: str) -> list[list[str]]:
    if not isinstance(alternatives, list):
        return []
    return [
        schema_errors(value, alternative, path)
        for alternative in alternatives
        if isinstance(alternative, dict)
    ]


def _composition_schema_errors(
    value: Any, schema: dict[str, Any], path: str
) -> list[str]:
    errors: list[str] = []
    alternatives = _alternative_errors(value, schema.get("anyOf"), path)
    if alternatives and all(alternative_errors for alternative_errors in alternatives):
        details = " OR ".join(
            "; ".join(alternative_errors) for alternative_errors in alternatives
        )
        errors.append(f"{path} must match at least one schema in anyOf: {details}")

    alternatives = _alternative_errors(value, schema.get("oneOf"), path)
    matches = sum(not alternative_errors for alternative_errors in alternatives)
    if alternatives and matches != 1:
        if matches == 0:
            details = " OR ".join(
                "; ".join(alternative_errors) for alternative_errors in alternatives
            )
            errors.append(f"{path} must match exactly one schema in oneOf: {details}")
        else:
            errors.append(
                f"{path} matches {matches} schemas in oneOf; exactly one is required."
            )
    return errors


def schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return violations from the JSON Schema subset published by this server."""

    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    if expected and not any(
        isinstance(item, str) and _matches_type(value, item) for item in expected_types
    ):
        return [f"{path} must be of type {expected}."]

    errors: list[str] = []
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        errors.append(f"{path} must be one of {allowed!r}.")

    if isinstance(value, dict):
        errors.extend(_object_schema_errors(value, schema, path))
    elif isinstance(value, list):
        errors.extend(_array_schema_errors(value, schema, path))
    elif isinstance(value, str):
        errors.extend(_string_schema_errors(value, schema, path))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        errors.extend(_number_schema_errors(value, schema, path))

    errors.extend(_composition_schema_errors(value, schema, path))
    return errors

#!/usr/bin/env python3
import importlib
import json
from dataclasses import dataclass
from typing import Any, Callable

MAX_TEXT = 262_144
MAX_OUTPUT = 240_000
MAX_PATTERN = 2_048
MAX_RESULTS = 1_000
MAX_TABLE_COLUMNS = 100
COMMAND_TIMEOUT_SECONDS = 5.0
REGEX_TIMEOUT_SECONDS = 2.0

TEXT_PROPERTY = {"type": "string", "description": f"UTF-8 text, at most {MAX_TEXT} bytes."}
SCALAR_SCHEMA = {"type": ["string", "number", "integer", "boolean", "null"]}


@dataclass(frozen=True)
class Operation:
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


def require_text(arguments: dict[str, Any], name: str = "text") -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value.encode("utf-8")) > MAX_TEXT:
        raise ValueError(f"{name} exceeds {MAX_TEXT} UTF-8 bytes")
    return value


def optional_string(arguments: dict[str, Any], name: str, default: str, max_length: int = 1_000) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return value


def optional_bool(arguments: dict[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def bounded_int(arguments: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    value = arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def choice(arguments: dict[str, Any], name: str, choices: set[str], default: str | None = None) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return value


def string_list(arguments: dict[str, Any], name: str, minimum: int, maximum: int, item_length: int) -> list[str]:
    value = arguments.get(name)
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} must be an array of {minimum} to {maximum} strings")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{name} must contain only strings")
        if len(item.encode("utf-8")) > item_length:
            raise ValueError(f"each {name} entry must be at most {item_length} UTF-8 bytes")
    return value


def optional_module(module_name: str, package: str, feature: str) -> Any:
    """Import a module supplied by a Debian package, naming the package when it is absent."""
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        raise ValueError(
            f"{feature} requires the {package} package, which is not installed on this node"
        ) from error


def output_text(value: str) -> dict[str, Any]:
    return {"text": value}


def word_tokens(text: str) -> list[str]:
    import re
    return re.findall(r"[^\W_]+(?:['\u2019][^\W_]+)?", text, re.UNICODE)


def text_properties(**extra: Any) -> dict[str, Any]:
    return {"text": TEXT_PROPERTY, **extra}


def typed(kind: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, **extra}


def operation_schema(name: str, operation: Operation) -> dict[str, Any]:
    return {
        "type": "object",
        "title": operation.description,
        "properties": {"operation": {"const": name}, **operation.properties},
        "required": ["operation", *operation.required],
        "additionalProperties": False,
    }


def category_schema(operations: dict[str, Operation]) -> dict[str, Any]:
    return {
        "type": "object",
        "oneOf": [operation_schema(name, operation) for name, operation in operations.items()],
    }


def dispatch(operations: dict[str, Operation], category: str, arguments: dict[str, Any]) -> dict[str, Any]:
    operation_name = arguments.get("operation")
    operation = operations.get(operation_name)
    if operation is None:
        raise ValueError(f"unknown {category} operation: {operation_name}; available: {', '.join(operations)}")
    missing = [name for name in operation.required if name not in arguments]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    unknown = sorted(set(arguments) - {"operation", *operation.properties})
    if unknown:
        raise ValueError(f"unknown fields for {category}.{operation_name}: {', '.join(unknown)}")
    result = operation.handler(arguments)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OUTPUT:
        raise ValueError(f"result exceeds {MAX_OUTPUT} UTF-8 bytes")
    return result

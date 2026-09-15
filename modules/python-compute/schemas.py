#!/usr/bin/env python3
"""Tool schemas and bounded argument validation for Python Compute."""
import json
import re
from typing import Any

MAX_CODE_CHARACTERS = 65_536
MAX_INPUT_BYTES = 65_536
MAX_FILES = 10
MAX_FILE_CHARACTERS = 65_536
MAX_FILE_TOTAL_CHARACTERS = 131_072
MIN_TIMEOUT_MS = 1_000
# Absolute ceilings advertised in the tool schema. Each installation resolves its own lower node limits,
# which python_env describe reports and the broker enforces.
MAX_TIMEOUT_MS = 600_000
DEFAULT_TIMEOUT_MS = 60_000
MIN_MEMORY_MB = 128
MAX_MEMORY_MB = 4_096
DEFAULT_MEMORY_MB = 384
MAX_CONCURRENT_CALLS = 4
MAX_CALLS_PER_MINUTE = 120
MIN_STDOUT_BYTES = 1_024
MAX_STDOUT_BYTES = 262_144
DEFAULT_STDOUT_BYTES = 65_536
MAX_ARTIFACTS = 8
MAX_ARTIFACT_BYTES = 1_000_000
MAX_ARTIFACT_TOTAL_BYTES = 1_500_000
MAX_IMPORTS = 20


def _clamp(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def resolve_limits(
    max_timeout_ms: Any = None,
    default_timeout_ms: Any = None,
    max_memory_mb: Any = None,
    default_memory_mb: Any = None,
    concurrent_calls: Any = None,
    calls_per_minute: Any = None,
) -> dict[str, int]:
    """Clamp installation-derived limits into the absolute bounds the tool schema advertises."""
    resolved_max_timeout = _clamp(max_timeout_ms, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS, MAX_TIMEOUT_MS)
    resolved_max_memory = _clamp(max_memory_mb, MIN_MEMORY_MB, MAX_MEMORY_MB, MAX_MEMORY_MB)
    return {
        "maxTimeoutMs": resolved_max_timeout,
        "defaultTimeoutMs": min(_clamp(default_timeout_ms, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS, DEFAULT_TIMEOUT_MS), resolved_max_timeout),
        "maxMemoryMb": resolved_max_memory,
        "defaultMemoryMb": min(_clamp(default_memory_mb, MIN_MEMORY_MB, MAX_MEMORY_MB, DEFAULT_MEMORY_MB), resolved_max_memory),
        "concurrentCalls": _clamp(concurrent_calls, 1, MAX_CONCURRENT_CALLS, 1),
        "callsPerMinute": _clamp(calls_per_minute, 1, MAX_CALLS_PER_MINUTE, 12),
    }

FILE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
IMPORT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,4}$")

RUN_RESULT_NOTE = (
    "Returns exitReason (completed, error, timeout, memory or killed), stdout, stderr, the JSON-safe "
    "result value, a bounded traceback, and base64 artifacts."
)

TOOLS: dict[str, dict[str, Any]] = {
    "python_env": {
        "description": (
            "Report what the node's Python environment provides before code is written: interpreter version and "
            "architecture, sandbox isolation, execution limits, the helper API, and installed modules grouped by "
            "capability. Use operation 'describe' first, 'packages' to filter the module list, and 'check' to confirm "
            "that specific imports work. Packages cannot be added from submitted code, so plan around this inventory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["describe", "packages", "check"],
                    "default": "describe",
                    "description": "describe returns the full environment, packages filters the module inventory, check imports named modules in the sandbox.",
                },
                "query": {"type": "string", "minLength": 1, "maxLength": 80, "description": "Case-insensitive substring filter on module names for operation 'packages'."},
                "group": {"type": "string", "minLength": 1, "maxLength": 40, "description": "Capability group name filter for operation 'packages', such as numeric or plotting."},
                "imports": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_IMPORTS,
                    "items": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": IMPORT_NAME.pattern},
                    "description": "Dotted module names to import for operation 'check', such as scipy.signal.",
                },
                "refresh": {"type": "boolean", "default": False, "description": "Re-scan installed modules instead of using the inventory recorded at installation."},
            },
            "additionalProperties": False,
        },
    },
    "python_run": {
        "description": (
            "Run submitted Python 3 code on the node in a fresh sandboxed process and return its output. The code has "
            "no network access, an empty writable working directory, and a wall-clock and memory limit; state is not "
            "kept between calls. Assign to the helper vanta (vanta.result, vanta.emit_image, vanta.emit_table, "
            "vanta.emit_text, vanta.emit_json, vanta.emit_file) to return values and rendered content, or end with a "
            "bare expression. Open matplotlib figures are captured as PNG automatically. Call python_env first when "
            "the required modules are uncertain. " + RUN_RESULT_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "minLength": 1, "maxLength": MAX_CODE_CHARACTERS, "description": "Python 3 source executed as __main__ in an empty working directory."},
                "inputs": {"type": "object", "description": "JSON values bound to the name inputs and to vanta.inputs inside the sandbox.", "additionalProperties": True},
                "files": {
                    "type": "array",
                    "maxItems": MAX_FILES,
                    "description": "Small UTF-8 text files written into the working directory before execution.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": FILE_NAME.pattern},
                            "text": {"type": "string", "maxLength": MAX_FILE_CHARACTERS},
                        },
                        "required": ["name", "text"],
                        "additionalProperties": False,
                    },
                },
                "timeoutMs": {"type": "integer", "minimum": MIN_TIMEOUT_MS, "maximum": MAX_TIMEOUT_MS, "description": "Wall-clock limit for the submitted code. The node may cap this lower; python_env describe reports the value in force."},
                "memoryMb": {"type": "integer", "minimum": MIN_MEMORY_MB, "maximum": MAX_MEMORY_MB, "description": "Address-space limit for the sandboxed process. Defaults and ceilings are sized from the node's memory; python_env describe reports them."},
                "artifacts": {"type": "boolean", "default": True, "description": "Return emitted files and captured figures. Set false to keep responses small."},
                "maxStdoutBytes": {"type": "integer", "minimum": MIN_STDOUT_BYTES, "maximum": MAX_STDOUT_BYTES, "default": DEFAULT_STDOUT_BYTES, "description": "Bytes of stdout and of stderr retained before truncation."},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}


def _reject_unknown(arguments: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"unknown argument: {unknown[0]}")


def _integer(arguments: dict[str, Any], name: str, minimum: int, maximum: int, default: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(arguments: dict[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def validate_env(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"operation", "query", "group", "imports", "refresh"})
    operation = arguments.get("operation", "describe")
    if operation not in ("describe", "packages", "check"):
        raise ValueError("operation must be describe, packages or check")
    request: dict[str, Any] = {"operation": operation, "refresh": _boolean(arguments, "refresh", False)}
    for name in ("query", "group"):
        value = arguments.get(name)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            if len(value) > 80:
                raise ValueError(f"{name} is too long")
            request[name] = value.strip()
    if operation == "check":
        imports = arguments.get("imports")
        if not isinstance(imports, list) or not imports:
            raise ValueError("imports is required for operation check")
        if len(imports) > MAX_IMPORTS:
            raise ValueError(f"imports accepts at most {MAX_IMPORTS} names")
        names = []
        for item in imports:
            if not isinstance(item, str) or not IMPORT_NAME.match(item):
                raise ValueError(f"invalid module name: {item!r}")
            names.append(item)
        request["imports"] = names
    elif "imports" in arguments:
        raise ValueError("imports is only valid for operation check")
    return request


def validate_run(arguments: dict[str, Any], limits: dict[str, int]) -> dict[str, Any]:
    _reject_unknown(arguments, {"code", "inputs", "files", "timeoutMs", "memoryMb", "artifacts", "maxStdoutBytes"})
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    if len(code) > MAX_CODE_CHARACTERS:
        raise ValueError(f"code exceeds {MAX_CODE_CHARACTERS} characters")
    if "\0" in code:
        raise ValueError("code must not contain null bytes")

    inputs = arguments.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    encoded_inputs = json.dumps(inputs, ensure_ascii=False)
    if len(encoded_inputs.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError(f"inputs exceed {MAX_INPUT_BYTES} bytes")

    files = arguments.get("files", [])
    if not isinstance(files, list):
        raise ValueError("files must be an array")
    if len(files) > MAX_FILES:
        raise ValueError(f"files accepts at most {MAX_FILES} entries")
    validated_files = []
    total_characters = 0
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("each file must be an object")
        _reject_unknown(entry, {"name", "text"})
        name = entry.get("name")
        text = entry.get("text")
        if not isinstance(name, str) or not FILE_NAME.match(name):
            raise ValueError(f"invalid file name: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate file name: {name}")
        if not isinstance(text, str):
            raise ValueError(f"file {name} must provide text")
        if len(text) > MAX_FILE_CHARACTERS:
            raise ValueError(f"file {name} exceeds {MAX_FILE_CHARACTERS} characters")
        total_characters += len(text)
        if total_characters > MAX_FILE_TOTAL_CHARACTERS:
            raise ValueError(f"files exceed {MAX_FILE_TOTAL_CHARACTERS} characters in total")
        seen.add(name)
        validated_files.append({"name": name, "text": text})

    memory_mb = _integer(arguments, "memoryMb", MIN_MEMORY_MB, limits["maxMemoryMb"], limits["defaultMemoryMb"])
    return {
        "code": code,
        "inputs": inputs,
        "files": validated_files,
        "timeoutMs": _integer(arguments, "timeoutMs", MIN_TIMEOUT_MS, limits["maxTimeoutMs"], limits["defaultTimeoutMs"]),
        "memoryMb": memory_mb,
        "artifacts": _boolean(arguments, "artifacts", True),
        "maxStdoutBytes": _integer(arguments, "maxStdoutBytes", MIN_STDOUT_BYTES, MAX_STDOUT_BYTES, DEFAULT_STDOUT_BYTES),
        "maxArtifacts": MAX_ARTIFACTS,
        "maxArtifactBytes": MAX_ARTIFACT_BYTES,
        "maxArtifactTotalBytes": MAX_ARTIFACT_TOTAL_BYTES,
    }


def self_test() -> None:
    assert list(TOOLS) == ["python_env", "python_run"]
    for tool in TOOLS.values():
        assert tool["inputSchema"]["additionalProperties"] is False

    node = resolve_limits(max_memory_mb=1176, default_memory_mb=784, concurrent_calls=2, calls_per_minute=24)
    assert node == {"maxTimeoutMs": MAX_TIMEOUT_MS, "defaultTimeoutMs": DEFAULT_TIMEOUT_MS, "maxMemoryMb": 1176, "defaultMemoryMb": 784, "concurrentCalls": 2, "callsPerMinute": 24}
    small = resolve_limits(max_memory_mb=600, default_memory_mb=400)
    assert small["concurrentCalls"] == 1 and small["callsPerMinute"] == 12
    # Out-of-range, missing and malformed installation values must clamp instead of failing open.
    assert resolve_limits(max_memory_mb=99_999)["maxMemoryMb"] == MAX_MEMORY_MB
    assert resolve_limits(max_memory_mb=1, default_memory_mb=1)["maxMemoryMb"] == MIN_MEMORY_MB
    assert resolve_limits(default_memory_mb=4_000, max_memory_mb=512)["defaultMemoryMb"] == 512
    assert resolve_limits(max_timeout_ms=30_000)["defaultTimeoutMs"] == 30_000
    assert resolve_limits(concurrent_calls=99)["concurrentCalls"] == MAX_CONCURRENT_CALLS
    assert resolve_limits(max_memory_mb="not-a-number")["maxMemoryMb"] == MAX_MEMORY_MB
    assert resolve_limits() == resolve_limits(None, None, None, None, None, None)

    run = validate_run({"code": "1 + 1"}, node)
    assert run["timeoutMs"] == DEFAULT_TIMEOUT_MS
    assert run["memoryMb"] == 784
    assert run["artifacts"] is True
    assert validate_run({"code": "x", "memoryMb": 1_000}, node)["memoryMb"] == 1_000

    for invalid, message in [
        ({}, "code must be"),
        ({"code": "  "}, "code must be"),
        ({"code": "x", "inputs": []}, "inputs must be an object"),
        ({"code": "x", "timeoutMs": 999}, "timeoutMs must be between"),
        ({"code": "x", "timeoutMs": MAX_TIMEOUT_MS + 1}, "timeoutMs must be between"),
        ({"code": "x", "memoryMb": 64}, "memoryMb must be between"),
        # The node ceiling, not the advertised schema maximum, is what a call is held to.
        ({"code": "x", "memoryMb": 2_048}, "memoryMb must be between 128 and 1176"),
        ({"code": "x", "artifacts": "yes"}, "artifacts must be a boolean"),
        ({"code": "x", "files": [{"name": "../escape", "text": ""}]}, "invalid file name"),
        ({"code": "x", "files": [{"name": "a.txt", "text": ""}, {"name": "a.txt", "text": ""}]}, "duplicate file name"),
        ({"code": "x", "shell": True}, "unknown argument"),
        ({"code": "x" * (MAX_CODE_CHARACTERS + 1)}, "exceeds"),
    ]:
        try:
            validate_run(invalid, node)
        except ValueError as error:
            assert message in str(error), f"{invalid} -> {error}"
        else:
            raise AssertionError(f"accepted invalid run arguments: {invalid}")

    assert validate_env({})["operation"] == "describe"
    assert validate_env({"operation": "check", "imports": ["scipy.signal"]})["imports"] == ["scipy.signal"]
    for invalid in [
        {"operation": "eval"},
        {"operation": "check"},
        {"operation": "check", "imports": ["os; rm -rf /"]},
        {"operation": "check", "imports": ["a"] * (MAX_IMPORTS + 1)},
        {"operation": "describe", "imports": ["json"]},
        {"operation": "packages", "query": ""},
    ]:
        try:
            validate_env(invalid)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid env arguments: {invalid}")


if __name__ == "__main__":
    self_test()

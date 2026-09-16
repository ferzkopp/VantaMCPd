#!/usr/bin/env python3
"""In-sandbox harness for Python Compute.

Runs inside bubblewrap with no network and a private filesystem view. It executes the submitted code,
captures output at file-descriptor level, converts the returned value to bounded JSON, and collects
emitted artifacts. Nothing here is reachable from outside the sandbox.
"""
import base64
import csv
import io
import json
import os
import signal
import sys
import time
import traceback
import types
from datetime import date, datetime, time as time_of_day, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

REQUEST_PATH = "/vanta-request.json"
WORK_DIR = "/work"
RESULT_PATH = "/work/.vanta-result.json"
STDOUT_PATH = "/work/.vanta-stdout"
STDERR_PATH = "/work/.vanta-stderr"
ARTIFACT_DIR = "/work/.vanta-artifacts"

MAX_STRING_CHARACTERS = 10_000
MAX_COLLECTION_ITEMS = 200
MAX_TABLE_ROWS = 500
MAX_DEPTH = 6
MAX_TRACEBACK_CHARACTERS = 4_000
RESERVED_NAMES = (".vanta-result.json", ".vanta-stdout", ".vanta-stderr", ".vanta-artifacts")

IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF8", "image/gif", "gif"),
)


class ExecutionTimeout(Exception):
    pass


def _raise_timeout(_signal_number, _frame):
    raise ExecutionTimeout()


def _truncate(text: str, limit: int = MAX_STRING_CHARACTERS) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [truncated, {len(text)} characters]"


def jsonable(value, depth: int = 0):
    """Convert an arbitrary Python value to bounded JSON-safe data."""
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else repr(value)
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)[:4_096]
        return {"__type__": "bytes", "length": len(bytes(value)), "base64": base64.b64encode(raw).decode("ascii")}
    if isinstance(value, (datetime, date, time_of_day, timedelta, Decimal, UUID, Path)):
        return str(value)
    if isinstance(value, complex):
        return {"__type__": "complex", "real": value.real, "imag": value.imag}
    if depth >= MAX_DEPTH:
        return _truncate(repr(value), 500)

    module = type(value).__module__.split(".")[0]
    if module == "numpy":
        return _jsonable_numpy(value, depth)
    if module == "pandas":
        converted = _jsonable_pandas(value, depth)
        if converted is not None:
            return converted

    if isinstance(value, dict):
        items = list(value.items())[:MAX_COLLECTION_ITEMS]
        result = {str(key)[:200]: jsonable(item, depth + 1) for key, item in items}
        if len(value) > MAX_COLLECTION_ITEMS:
            result["__truncated__"] = f"{len(value)} keys"
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)[:MAX_COLLECTION_ITEMS]
        converted = [jsonable(item, depth + 1) for item in items]
        if len(value) > MAX_COLLECTION_ITEMS:
            converted.append(f"... [truncated, {len(value)} items]")
        return converted
    if hasattr(value, "__dataclass_fields__"):
        return {name: jsonable(getattr(value, name, None), depth + 1) for name in list(value.__dataclass_fields__)[:MAX_COLLECTION_ITEMS]}
    return {"__type__": f"{type(value).__module__}.{type(value).__name__}", "repr": _truncate(repr(value), 1_000)}


def _jsonable_numpy(value, depth: int):
    if hasattr(value, "shape") and hasattr(value, "dtype") and hasattr(value, "size"):
        if getattr(value, "ndim", 0) == 0:
            return jsonable(value.item(), depth + 1)
        flat_limit = 2_000
        data = value.ravel()[:flat_limit].tolist() if value.size > flat_limit else value.tolist()
        return {
            "__type__": "ndarray",
            "dtype": str(value.dtype),
            "shape": list(value.shape),
            "data": jsonable(data, depth + 1),
            "truncated": bool(value.size > flat_limit),
        }
    if hasattr(value, "item"):
        try:
            return jsonable(value.item(), depth + 1)
        except Exception:
            pass
    return _truncate(repr(value), 1_000)


def _jsonable_pandas(value, depth: int):
    name = type(value).__name__
    if name == "DataFrame":
        head = value.head(MAX_TABLE_ROWS)
        return {
            "__type__": "DataFrame",
            "shape": [int(value.shape[0]), int(value.shape[1])],
            "columns": [str(column) for column in value.columns[:MAX_COLLECTION_ITEMS]],
            "dtypes": {str(column): str(dtype) for column, dtype in list(value.dtypes.items())[:MAX_COLLECTION_ITEMS]},
            "rows": [{str(key): jsonable(item, depth + 2) for key, item in record.items()} for record in head.to_dict(orient="records")],
            "truncated": bool(value.shape[0] > MAX_TABLE_ROWS),
        }
    if name == "Series":
        head = value.head(MAX_TABLE_ROWS)
        return {
            "__type__": "Series",
            "name": None if value.name is None else str(value.name),
            "dtype": str(value.dtype),
            "length": int(value.shape[0]),
            "values": [jsonable(item, depth + 2) for item in head.tolist()],
            "index": [str(item) for item in head.index.tolist()],
            "truncated": bool(value.shape[0] > MAX_TABLE_ROWS),
        }
    return None


class Vanta:
    """Helper injected into submitted code as `vanta`."""

    def __init__(self, inputs: dict, limits: dict):
        self.inputs = inputs
        self.artifact_paths = limits.get("artifactPaths", {})
        self._limits = limits
        self._result = None
        self._has_result = False
        self._artifacts: list[dict] = []
        self._bytes = 0

    def result(self, value) -> None:
        """Set the value returned to the caller."""
        self._result = value
        self._has_result = True

    def emit_text(self, text: str, name: str | None = None) -> str:
        return self._store(str(text).encode("utf-8"), name, "text", "text/plain", "txt")

    def emit_json(self, value, name: str | None = None) -> str:
        payload = json.dumps(jsonable(value), ensure_ascii=False, indent=2).encode("utf-8")
        return self._store(payload, name, "json", "application/json", "json")

    def emit_table(self, rows, name: str | None = None) -> str:
        return self._store(self._table_bytes(rows), name, "table", "text/csv", "csv")

    def emit_image(self, image, name: str | None = None) -> str:
        payload, mime_type, extension = self._image_bytes(image)
        return self._store(payload, name, "image", mime_type, extension)

    def emit_file(self, path, name: str | None = None) -> str:
        resolved = Path(WORK_DIR, path).resolve()
        if not str(resolved).startswith(WORK_DIR + os.sep):
            raise ValueError("emit_file accepts paths inside the working directory only")
        payload = resolved.read_bytes()
        mime_type, extension = "application/octet-stream", resolved.suffix.lstrip(".") or "bin"
        for signature, signature_mime, signature_extension in IMAGE_SIGNATURES:
            if payload.startswith(signature):
                mime_type, extension = signature_mime, signature_extension
        kind = "image" if mime_type.startswith("image/") else "file"
        return self._store(payload, name or resolved.name, kind, mime_type, extension)

    def _table_bytes(self, rows) -> bytes:
        if hasattr(rows, "to_csv"):
            return str(rows.to_csv(index=False)).encode("utf-8")
        items = list(rows)[:MAX_TABLE_ROWS]
        buffer = io.StringIO()
        if items and isinstance(items[0], dict):
            columns: list[str] = []
            for item in items:
                for key in item:
                    if str(key) not in columns:
                        columns.append(str(key))
            writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for item in items:
                writer.writerow({str(key): value for key, value in item.items()})
        else:
            writer = csv.writer(buffer)
            for item in items:
                writer.writerow(list(item) if isinstance(item, (list, tuple)) else [item])
        return buffer.getvalue().encode("utf-8")

    def _image_bytes(self, image) -> tuple[bytes, str, str]:
        if isinstance(image, (bytes, bytearray)):
            payload = bytes(image)
            for signature, mime_type, extension in IMAGE_SIGNATURES:
                if payload.startswith(signature):
                    return payload, mime_type, extension
            raise ValueError("emit_image accepts PNG, JPEG or GIF bytes")
        buffer = io.BytesIO()
        if hasattr(image, "savefig"):
            image.savefig(buffer, format="png", dpi=100, bbox_inches="tight")
        elif hasattr(image, "save") and hasattr(image, "mode"):
            image.convert("RGB" if image.mode not in ("RGB", "RGBA", "L") else image.mode).save(buffer, format="PNG")
        elif hasattr(image, "get_figure"):
            image.get_figure().savefig(buffer, format="png", dpi=100, bbox_inches="tight")
        else:
            raise ValueError("emit_image accepts a matplotlib figure, a PIL image, or image bytes")
        return buffer.getvalue(), "image/png", "png"

    def _store(self, payload: bytes, name: str | None, kind: str, mime_type: str, extension: str) -> str:
        if len(self._artifacts) >= self._limits["maxArtifacts"]:
            raise ValueError(f"at most {self._limits['maxArtifacts']} artifacts can be emitted")
        if len(payload) > self._limits["maxArtifactBytes"]:
            raise ValueError(f"artifact exceeds {self._limits['maxArtifactBytes']} bytes")
        if self._bytes + len(payload) > self._limits["maxArtifactTotalBytes"]:
            raise ValueError(f"artifacts exceed {self._limits['maxArtifactTotalBytes']} bytes in total")
        safe = "".join(character if character.isalnum() or character in "._-" else "_" for character in (name or f"artifact-{len(self._artifacts) + 1}.{extension}"))
        safe = safe[:64] or f"artifact-{len(self._artifacts) + 1}.{extension}"
        if not safe.lower().endswith(f".{extension}") and "." not in safe:
            safe = f"{safe}.{extension}"
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        stored = os.path.join(ARTIFACT_DIR, f"{len(self._artifacts):02d}-{safe}")
        with open(stored, "wb") as handle:
            handle.write(payload)
        self._bytes += len(payload)
        self._artifacts.append({"name": safe, "kind": kind, "mimeType": mime_type, "bytes": len(payload), "path": stored})
        return safe


def _capture_figures(vanta: Vanta) -> None:
    pyplot = sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return
    for number in list(pyplot.get_fignums()):
        if len(vanta._artifacts) >= vanta._limits["maxArtifacts"]:
            break
        try:
            vanta.emit_image(pyplot.figure(number), f"figure-{number}.png")
        except Exception:
            continue
    try:
        pyplot.close("all")
    except Exception:
        pass


def _format_traceback(error: BaseException) -> str:
    entries = traceback.extract_tb(error.__traceback__)
    submitted = [entry for entry in entries if entry.filename == "<submitted>"]
    lines = traceback.format_list(submitted or entries[-1:])
    lines.extend(traceback.format_exception_only(type(error), error))
    return _truncate("".join(lines), MAX_TRACEBACK_CHARACTERS)


def _run_check(imports: list[str]) -> dict:
    results = []
    for name in imports:
        started = time.monotonic()
        try:
            __import__(name)
            module = sys.modules.get(name)
            version = getattr(module, "__version__", None)
            if version is None and "." in name:
                version = getattr(sys.modules.get(name.split(".")[0]), "__version__", None)
            results.append({"module": name, "available": True, "version": None if version is None else str(version)[:40], "importMs": round((time.monotonic() - started) * 1000)})
        except Exception as error:
            results.append({"module": name, "available": False, "error": _truncate(f"{type(error).__name__}: {error}", 300)})
    return {"status": "completed", "imports": results}


def _run_code(request: dict) -> dict:
    import ast

    vanta = Vanta(request.get("inputs", {}), request)
    module = types.ModuleType("vanta")
    for attribute in ("result", "emit_text", "emit_json", "emit_table", "emit_image", "emit_file"):
        setattr(module, attribute, getattr(vanta, attribute))
    module.inputs = vanta.inputs
    module.artifact_paths = vanta.artifact_paths
    sys.modules["vanta"] = module

    namespace: dict = {"__name__": "__main__", "__builtins__": __builtins__, "vanta": module, "inputs": vanta.inputs, "artifact_paths": vanta.artifact_paths}
    status = "completed"
    error_value = None
    started = time.monotonic()
    try:
        tree = ast.parse(request["code"], filename="<submitted>", mode="exec")
        trailing = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        exec(compile(tree, "<submitted>", "exec"), namespace)
        if trailing is not None:
            value = eval(compile(ast.Expression(trailing.value), "<submitted>", "eval"), namespace)
            if value is not None and not vanta._has_result:
                vanta.result(value)
    except ExecutionTimeout:
        status = "timeout"
        error_value = {"type": "Timeout", "message": f"execution exceeded {request['timeoutMs']} ms", "traceback": ""}
    except MemoryError as failure:
        status = "error"
        error_value = {"type": "MemoryError", "message": f"the {request['memoryMb']} MB memory limit was reached", "traceback": _format_traceback(failure)}
    except BaseException as failure:  # SystemExit and KeyboardInterrupt are reported, never propagated
        status = "error"
        error_value = {"type": type(failure).__name__, "message": _truncate(str(failure), 1_000), "traceback": _format_traceback(failure)}

    signal.setitimer(signal.ITIMER_REAL, 0)
    if request.get("artifacts", True):
        try:
            _capture_figures(vanta)
        except Exception:
            pass
    return {
        "status": status,
        "durationMs": round((time.monotonic() - started) * 1000),
        "hasResult": vanta._has_result,
        "result": jsonable(vanta._result) if vanta._has_result else None,
        "error": error_value,
        "artifacts": vanta._artifacts,
    }


def main() -> None:
    with open(REQUEST_PATH, "r", encoding="utf-8") as handle:
        request = json.load(handle)

    stdout_file = open(STDOUT_PATH, "wb")
    stderr_file = open(STDERR_PATH, "wb")
    saved_stdout, saved_stderr = os.dup(1), os.dup(2)
    os.dup2(stdout_file.fileno(), 1)
    os.dup2(stderr_file.fileno(), 2)
    sys.stdout = open(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
    sys.stderr = open(2, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, request.get("timeoutMs", 30_000) / 1000)
    try:
        if request.get("mode") == "check":
            result = _run_check(request["imports"])
        else:
            result = _run_code(request)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        stdout_file.close()
        stderr_file.close()

    with open(RESULT_PATH, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, default=lambda value: _truncate(repr(value), 500))


def self_test() -> None:
    assert jsonable("x" * (MAX_STRING_CHARACTERS + 10)).endswith("characters]")
    assert jsonable({"a": [1, 2, {"b": (3, 4)}]}) == {"a": [1, 2, {"b": [3, 4]}]}
    assert jsonable(list(range(MAX_COLLECTION_ITEMS + 5)))[-1].startswith("... [truncated")
    assert jsonable(b"abc")["base64"] == base64.b64encode(b"abc").decode("ascii")
    assert jsonable(Decimal("1.5")) == "1.5"
    assert jsonable(float("inf")) == "inf"
    assert jsonable({1: "a"}) == {"1": "a"}
    assert jsonable(object())["__type__"].endswith("object")

    limits = {"maxArtifacts": 2, "maxArtifactBytes": 100, "maxArtifactTotalBytes": 150}
    vanta = Vanta({}, limits)
    assert vanta._table_bytes([{"a": 1, "b": 2}, {"a": 3, "b": 4}]).decode("utf-8").splitlines()[0] == "a,b"
    assert vanta._table_bytes([[1, 2], [3, 4]]).decode("utf-8").splitlines()[0] == "1,2"
    for payload, expected in [(b"\x89PNG\r\n\x1a\n data", "image/png"), (b"\xff\xd8\xff data", "image/jpeg")]:
        assert vanta._image_bytes(payload)[1] == expected
    for invalid in [b"not-an-image", object()]:
        try:
            vanta._image_bytes(invalid)
        except ValueError:
            continue
        raise AssertionError("accepted an unsupported image value")
    assert _format_traceback(ValueError("boom")).strip().endswith("ValueError: boom")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    else:
        main()

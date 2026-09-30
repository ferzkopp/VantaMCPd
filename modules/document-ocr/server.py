#!/usr/bin/env python3
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from artifact_protocol import available, copy_verified, publish_reserved, release, reserve

try:
    import fcntl
except ImportError:
    fcntl = None

VERSION = "0.1.1"
IMAGE = "localhost/vantamcpd-document-ocr:0.1.0"
STATE = "/var/lib/vantamcpd-document-ocr"
MODEL_DIR = os.path.join(STATE, "models")
if hasattr(os, "getuid"):
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
ARTIFACT_ROOT = os.environ.get("VANTA_ARTIFACT_ROOT")
MAX_INPUT = 50_000_000
MAX_RESULT = 8_000_000
MAX_INLINE = 2_000_000
MAX_ERROR = 500
PROTOCOL_VERSION = "2025-06-18"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# PaddleX progress and model-cache chatter that never explains a failure.
NOISE = re.compile(r"^(Connectivity check|Checking connectivity|No model hoster|Creating model:|Model files already exist|"
                   r"Using official model|Fetching \d+ files|Traceback \(most recent call last\)|File \"|\^+$)")

TOOLS = {
    "ocr_environment": {
        "description": "Report the OCR model, GPU profile, supported files, artifact availability, and processing limits.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "ocr_extract": {
        "description": "Extract printed text and line coordinates from a bounded PNG, JPEG, WebP, or PDF artifact. PDFs may run in the background; large output should be stored as an artifact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifactId": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "pages": {"type": "array", "minItems": 1, "maxItems": 20,
                          "items": {"type": "integer", "minimum": 1, "maximum": 20}, "uniqueItems": True},
                "language": {"type": "string", "enum": ["auto", "en"], "default": "auto"},
                "outputMode": {"type": "string", "enum": ["inline", "artifact"], "default": "inline"},
                "retentionDays": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
            },
            "required": ["artifactId"],
            "additionalProperties": False,
        },
    },
}


def validate(name: str, arguments: Any) -> dict[str, Any]:
    if name not in TOOLS or not isinstance(arguments, dict):
        raise ValueError("unknown OCR tool or invalid arguments")
    if name == "ocr_environment":
        if arguments:
            raise ValueError("ocr_environment takes no arguments")
        return {}
    if set(arguments) - {"artifactId", "pages", "language", "outputMode", "retentionDays"}:
        raise ValueError("unknown OCR argument")
    artifact_id = arguments.get("artifactId")
    if not isinstance(artifact_id, str) or re.fullmatch(r"[0-9a-f]{32}", artifact_id) is None:
        raise ValueError("artifactId is invalid")
    pages = arguments.get("pages", [])
    if not isinstance(pages, list) or len(pages) > 20 or any(type(page) is not int or page < 1 or page > 20 for page in pages) or len(set(pages)) != len(pages):
        raise ValueError("pages must be distinct page numbers from 1 to 20")
    mode = arguments.get("outputMode", "inline")
    language = arguments.get("language", "auto")
    retention = arguments.get("retentionDays", 7)
    if mode not in ("inline", "artifact") or language not in ("auto", "en") or type(retention) is not int or not 1 <= retention <= 90:
        raise ValueError("language, outputMode, or retentionDays is invalid")
    return {"artifactId": artifact_id, "pages": pages, "language": language,
            "outputMode": mode, "retentionDays": retention}


def container_argv(call_dir: str) -> list[str]:
    return ["podman", "--cgroup-manager=cgroupfs", "run", "--rm", "--cidfile", os.path.join(call_dir, "container.id"),
            # No --read-only: NVIDIA CDI hooks write ldcache and symlinks into the rootfs; --rm discards that layer.
            "--network=none", "--tmpfs", "/tmp:rw,size=256m", "--device", "nvidia.com/gpu=all",
            "--security-opt", "no-new-privileges", "--cap-drop=all", "--pids-limit=128", "--memory=6g", "--cpus=3",
            # No --userns=keep-id: without overlay ID shifting it chown-copies the whole ~50 GB image per call.
            "-e", "PADDLE_PDX_CACHE_HOME=/models",
            "-e", "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True", "-e", "HOME=/work",
            "-v", f"{os.path.join(call_dir, 'inputs')}:/inputs:ro",
            "-v", f"{os.path.join(call_dir, 'work')}:/work:rw",
            "-v", f"{MODEL_DIR}:/models:ro", IMAGE]


def clean(text: str) -> str:
    text = "".join(char if char.isprintable() else " " for char in ANSI.sub("", text))
    return " ".join(text.split())[:MAX_ERROR]


def worker_failure(work: str, returncode: int, diagnostics: bytes) -> str:
    error_file = os.path.join(work, "error.json")
    if not os.path.islink(error_file) and os.path.isfile(error_file) and os.path.getsize(error_file) <= 4 * MAX_ERROR:
        try:
            with open(error_file, encoding="utf-8") as handle:
                message = json.load(handle).get("error")
            if isinstance(message, str) and clean(message):
                return "OCR failed: " + clean(message)
        except (ValueError, AttributeError):
            pass
    if returncode == 137:
        return "OCR worker was killed (exit 137), most likely by the 6 GB container memory limit; select fewer pages"
    lines = [line for line in (clean(raw) for raw in diagnostics.decode("utf-8", "replace").splitlines())
             if line and not NOISE.match(line)]
    return f"OCR worker failed (exit {returncode}): " + (lines[-1] if lines else "no diagnostic output")


def extract(arguments: dict[str, Any], background: bool = False) -> dict[str, Any]:
    if fcntl is None:
        raise RuntimeError("document-ocr requires Linux advisory locks")
    if not available(ARTIFACT_ROOT):
        raise ValueError("shared artifact storage is unavailable on this node")
    os.makedirs(os.path.join(STATE, "calls"), mode=0o700, exist_ok=True)
    with open(os.path.join(STATE, "gpu.lock"), "a+b") as gpu_lock:
        try:
            fcntl.flock(gpu_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("document-ocr is already using this GPU; retry shortly") from error
        with tempfile.TemporaryDirectory(prefix="ocr-", dir=os.path.join(STATE, "calls")) as call_dir:
            inputs = os.path.join(call_dir, "inputs")
            work = os.path.join(call_dir, "work")
            os.mkdir(inputs, mode=0o700)
            os.mkdir(work, mode=0o700)
            source = os.path.join(inputs, "document")
            copy_verified(ARTIFACT_ROOT, arguments["artifactId"], source, MAX_INPUT)
            with open(source, "rb") as handle:
                header = handle.read(16)
            if not (header.startswith(b"%PDF-") or header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff") or header.startswith(b"RIFF") and header[8:12] == b"WEBP"):
                raise ValueError("input must be a PDF, PNG, JPEG, or WebP")
            with open(os.path.join(work, "request.json"), "x", encoding="utf-8") as handle:
                json.dump({"pages": arguments["pages"], "language": arguments["language"]}, handle)
            reservation_id = None
            try:
                if arguments["outputMode"] == "artifact":
                    reservation_id = reserve(ARTIFACT_ROOT, "document-ocr", MAX_RESULT)
                started = time.monotonic()
                timeout = 900 if background else 125
                with open(os.path.join(call_dir, "worker.log"), "w+b") as diagnostics:
                    try:
                        process = subprocess.run(container_argv(call_dir), stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=diagnostics, timeout=timeout, check=False)
                    except subprocess.TimeoutExpired as error:
                        hint = "select fewer pages" if background else "retry with execution: background or select fewer pages"
                        raise ValueError(f"OCR worker exceeded the {timeout}-second limit; {hint}") from error
                    finally:
                        cidfile = os.path.join(call_dir, "container.id")
                        if os.path.isfile(cidfile):
                            try:
                                subprocess.run(["podman", "stop", "--ignore", "-t", "0", "--cidfile", cidfile],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, timeout=15)
                            except (OSError, subprocess.TimeoutExpired):
                                pass
                    if diagnostics.tell() > 64_000:
                        raise ValueError("OCR worker exceeded the diagnostic output limit")
                    if process.returncode:
                        diagnostics.seek(max(0, diagnostics.tell() - 4000))
                        raise ValueError(worker_failure(work, process.returncode, diagnostics.read()))
                result_file = os.path.join(work, "result.json")
                if os.path.islink(result_file) or not os.path.isfile(result_file) or os.path.getsize(result_file) > MAX_RESULT:
                    raise ValueError("OCR result is missing or too large")
                with open(result_file, "rb") as handle:
                    result_bytes = handle.read(MAX_RESULT + 1)
                result = json.loads(result_bytes)
                if not isinstance(result, dict) or not isinstance(result.get("pages"), list) or not isinstance(result.get("pageCount"), int):
                    raise ValueError("OCR worker returned an invalid result")
                result["durationMs"] = round((time.monotonic() - started) * 1000)
                result["sourceArtifactId"] = arguments["artifactId"]
                if arguments["outputMode"] == "artifact":
                    artifact = publish_reserved(ARTIFACT_ROOT, reservation_id, "document-ocr", [{"source": result_file,
                        "name": "ocr-result.json", "mimeType": "application/json"}], arguments["retentionDays"])[0]
                    reservation_id = None
                    return {"artifact": artifact, "pageCount": result["pageCount"], "model": result["model"],
                            "sourceArtifactId": arguments["artifactId"], "durationMs": result["durationMs"]}
                if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_INLINE:
                    raise ValueError("OCR result exceeds the inline limit; use artifact output")
                return result
            finally:
                if reservation_id:
                    release(ARTIFACT_ROOT, reservation_id)


def interrupted(_signal: int, _frame: Any) -> None:
    raise KeyboardInterrupt()


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "vanta-document-ocr", "version": VERSION},
                      "instructions": "Use artifact IDs for local PDF and image inputs; URLs and filesystem paths are not accepted."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": name, **tool, "annotations": {"readOnlyHint": name == "ocr_environment",
                        "destructiveHint": False, "openWorldHint": False}} for name, tool in TOOLS.items()]}
        elif method == "tools/call":
            parameters = message.get("params", {})
            name = parameters.get("name")
            arguments = validate(name, parameters.get("arguments", {}))
            meta = parameters.get("_meta")
            background = isinstance(meta, dict) and meta.get("vantamcpd/execution") == "background"
            value = ({"model": "PP-OCRv5-mobile", "formats": ["pdf", "png", "jpeg", "webp"],
                      "maxPages": 20, "maxPixelsPerPage": 16_000_000, "maxInputBytes": MAX_INPUT,
                      "artifactAvailable": available(ARTIFACT_ROOT)} if name == "ocr_environment" else extract(arguments, background))
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            result = {"content": [{"type": "text", "text": text}], "structuredContent": value}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as error:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(error)}}


def main() -> None:
    if sys.argv[1:] == ["--self-test"]:
        assert validate("ocr_extract", {"artifactId": "a" * 32})["pages"] == []
        assert handle_request({"id": 1, "method": "tools/list"})["result"]["tools"][1]["name"] == "ocr_extract"
        return
    signal.signal(signal.SIGTERM, interrupted)
    for raw in sys.stdin:
        try:
            response = handle_request(json.loads(raw))
            if response is not None:
                print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
        except Exception as error:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(error)}}), flush=True)


if __name__ == "__main__":
    main()
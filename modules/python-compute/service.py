#!/usr/bin/env python3
"""Python Compute broker.

A long-lived systemd service owned by an unprivileged system user. It accepts length-prefixed JSON
requests on a Unix socket from the VantaMCPd SSH user only, serializes execution, and runs each call in
a fresh sandbox. Submitted code never runs in this process.
"""
import json
import logging
import os
import socket
import socketserver
import struct
import sys
import threading
import time
from collections import deque
from hashlib import sha256
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import inventory
import schemas
from sandbox import Sandbox

SOCKET_PATH = "/run/vantamcpd-python/python.sock"
MAX_REQUEST_BYTES = 524_288
MAX_RESPONSE_BYTES = 2_000_000

INSTALL_DIR = os.environ.get("VANTA_PYTHON_INSTALL_DIR", "/opt/vantamcpd/modules/python-compute/current")
STATE_DIR = os.environ.get("VANTA_PYTHON_STATE_DIR", "/var/lib/vantamcpd-python")
BUNDLE = os.environ.get("VANTA_PYTHON_BUNDLE", "science")
# Sized from the node's own memory and cores at installation, then clamped to the advertised absolutes.
LIMITS = schemas.resolve_limits(
    max_timeout_ms=os.environ.get("VANTA_PYTHON_MAX_TIMEOUT_MS"),
    default_timeout_ms=os.environ.get("VANTA_PYTHON_DEFAULT_TIMEOUT_MS"),
    max_memory_mb=os.environ.get("VANTA_PYTHON_MAX_MEMORY_MB"),
    default_memory_mb=os.environ.get("VANTA_PYTHON_DEFAULT_MEMORY_MB"),
    concurrent_calls=os.environ.get("VANTA_PYTHON_CONCURRENT_CALLS"),
    calls_per_minute=os.environ.get("VANTA_PYTHON_CALLS_PER_MINUTE"),
)
ACTIVE_CALLS = threading.BoundedSemaphore(LIMITS["concurrentCalls"])
RATE_LOCK = threading.Lock()
RECENT_CALLS: deque[float] = deque()

HELPERS = [
    {"call": "vanta.result(value)", "description": "Return a value to the caller. A trailing bare expression does the same."},
    {"call": "vanta.emit_text(text, name=None)", "description": "Attach a UTF-8 text artifact."},
    {"call": "vanta.emit_json(value, name=None)", "description": "Attach a JSON artifact."},
    {"call": "vanta.emit_table(rows, name=None)", "description": "Attach a CSV artifact from dict rows, row lists, or a pandas DataFrame."},
    {"call": "vanta.emit_image(figure, name=None)", "description": "Attach a PNG from a matplotlib figure, a PIL image, or image bytes."},
    {"call": "vanta.emit_file(path, name=None)", "description": "Attach a file the code wrote inside the working directory."},
    {"call": "vanta.inputs / inputs", "description": "The JSON object supplied as the inputs argument."},
]

logging.basicConfig(level=logging.INFO, format="python-compute %(levelname)s %(message)s")
LOGGER = logging.getLogger("python-compute")
ARTIFACT_ROOT = os.environ.get("VANTA_ARTIFACT_ROOT")
SANDBOX = Sandbox(INSTALL_DIR, STATE_DIR, LIMITS["defaultMemoryMb"], ARTIFACT_ROOT)


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(min(remaining, 65_536))
        if not chunk:
            raise ValueError("client closed the request")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise PermissionError("SO_PEERCRED is unavailable")
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def _check_rate_limit(now: float) -> None:
    with RATE_LOCK:
        while RECENT_CALLS and now - RECENT_CALLS[0] >= 60:
            RECENT_CALLS.popleft()
        if len(RECENT_CALLS) >= LIMITS["callsPerMinute"]:
            raise ValueError(f"python-compute accepts at most {LIMITS['callsPerMinute']} calls per minute")
        RECENT_CALLS.append(now)


def _environment(refresh: bool) -> dict[str, Any]:
    if not refresh:
        try:
            with open(os.path.join(INSTALL_DIR, "environment.json"), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            pass
    return inventory.describe(BUNDLE)


def _limits() -> dict[str, Any]:
    return {
        "timeoutMs": {"minimum": schemas.MIN_TIMEOUT_MS, "maximum": LIMITS["maxTimeoutMs"], "default": LIMITS["defaultTimeoutMs"]},
        "memoryMb": {"minimum": schemas.MIN_MEMORY_MB, "maximum": LIMITS["maxMemoryMb"], "default": LIMITS["defaultMemoryMb"]},
        "codeCharacters": schemas.MAX_CODE_CHARACTERS,
        "inputBytes": schemas.MAX_INPUT_BYTES,
        "files": {"maximum": schemas.MAX_FILES, "totalCharacters": schemas.MAX_FILE_TOTAL_CHARACTERS},
        "stdoutBytes": {"default": schemas.DEFAULT_STDOUT_BYTES, "maximum": schemas.MAX_STDOUT_BYTES},
        "artifacts": {"maximum": schemas.MAX_ARTIFACTS, "bytesEach": schemas.MAX_ARTIFACT_BYTES, "bytesTotal": schemas.MAX_ARTIFACT_TOTAL_BYTES},
        "sharedArtifacts": {"available": bool(ARTIFACT_ROOT), "inputsBytes": schemas.MAX_ARTIFACT_INPUT_BYTES, "storedBytesEach": schemas.MAX_STORED_ARTIFACT_BYTES, "storedBytesTotal": schemas.MAX_STORED_ARTIFACT_TOTAL_BYTES},
        "responseBytes": MAX_RESPONSE_BYTES,
        "callsPerMinute": LIMITS["callsPerMinute"],
        "concurrentCalls": LIMITS["concurrentCalls"],
        "sizedFrom": "the node's memory and CPU cores at installation",
    }


def _describe(refresh: bool) -> dict[str, Any]:
    environment = _environment(refresh)
    return {
        **environment,
        "isolation": SANDBOX.isolation,
        "limits": _limits(),
        "helpers": HELPERS,
        "usage": (
            "Submit code with python_run. Each call starts a fresh interpreter with no network access, an empty "
            "working directory, and no state from earlier calls. Import only the modules listed here."
        ),
    }


def _packages(query: str | None, group: str | None, refresh: bool) -> dict[str, Any]:
    environment = _environment(refresh)
    groups = []
    for entry in environment.get("groups", []):
        if group and group.lower() not in entry["name"].lower():
            continue
        packages = [package for package in entry["packages"] if not query or query.lower() in package["module"].lower()]
        if packages:
            groups.append({**entry, "packages": packages})
    return {
        "bundle": environment.get("bundle"),
        "python": environment.get("python"),
        "groups": groups,
        "moduleCount": sum(len(entry["packages"]) for entry in groups),
        "filters": {"query": query, "group": group},
    }


def execute(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if action == "ping":
        return {"ok": True, "isolation": SANDBOX.isolation.get("level")}
    if action == "python_env":
        request = schemas.validate_env(arguments)
        operation = request["operation"]
        if operation == "describe":
            return _describe(request["refresh"])
        if operation == "packages":
            return _packages(request.get("query"), request.get("group"), request["refresh"])
        return SANDBOX.check(request["imports"])
    if action == "python_run":
        request = schemas.validate_run(arguments, LIMITS)
        result = SANDBOX.run(request)
        result["request"] = {"timeoutMs": request["timeoutMs"], "memoryMb": request["memoryMb"]}
        # A call that hit a limit needs to see every limit, so the next attempt can be sized correctly.
        if result.get("exitReason") != "completed":
            result["limits"] = _limits()
        return result
    raise ValueError(f"unknown action: {action}")


def _summary(action: str, arguments: Any) -> str:
    """Submitted code can contain sensitive data, so only its digest and length are logged."""
    if action != "python_run" or not isinstance(arguments, dict) or not isinstance(arguments.get("code"), str):
        return "-"
    code = arguments["code"].encode("utf-8")
    return f"sha256:{sha256(code).hexdigest()[:12]} bytes={len(code)}"


class RequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        started = time.monotonic()
        action = "unknown"
        arguments: dict[str, Any] = {}
        try:
            if _peer_uid(self.request) != EXPECTED_UID:
                raise PermissionError("caller is not authorized to use python-compute")
            size = struct.unpack("!I", _receive_exact(self.request, 4))[0]
            if size < 2 or size > MAX_REQUEST_BYTES:
                raise ValueError("request size is invalid")
            message = json.loads(_receive_exact(self.request, size))
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            action = message.get("action")
            arguments = message.get("arguments", {})
            if action not in ("ping", "python_env", "python_run"):
                raise ValueError("unknown broker action")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            if action == "ping":
                result = execute(action, arguments)
            else:
                _check_rate_limit(time.monotonic())
                if not ACTIVE_CALLS.acquire(timeout=5):
                    raise ValueError(f"python-compute is already running {LIMITS['concurrentCalls']} call(s); retry shortly")
                try:
                    result = execute(action, arguments)
                finally:
                    ACTIVE_CALLS.release()
            response = {"ok": True, "result": result}
            LOGGER.info("action=%s code=%s ok=true duration_ms=%d", action, _summary(action, arguments), round((time.monotonic() - started) * 1000))
        except Exception as error:
            response = {"ok": False, "error": str(error)}
            LOGGER.warning("action=%s code=%s ok=false duration_ms=%d error=%s", action, _summary(action, arguments), round((time.monotonic() - started) * 1000), type(error).__name__)
        payload = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = json.dumps({"ok": False, "error": "the result exceeded the response size limit; request fewer artifacts or less output"}, separators=(",", ":")).encode("utf-8")
        self.request.sendall(struct.pack("!I", len(payload)) + payload)


class BrokerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 4


def main() -> None:
    SANDBOX.purge_stale_calls()
    SANDBOX.probe()
    LOGGER.info("sandbox verified: %s", SANDBOX.isolation["level"])
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o755, exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    with BrokerServer(SOCKET_PATH, RequestHandler) as server:
        os.chown(SOCKET_PATH, -1, CLIENT_GID)
        os.chmod(SOCKET_PATH, 0o660)
        LOGGER.info("broker ready bundle=%s memory_mb=%d/%d concurrency=%d", BUNDLE, LIMITS["defaultMemoryMb"], LIMITS["maxMemoryMb"], LIMITS["concurrentCalls"])
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    EXPECTED_UID = int(os.environ["VANTA_PYTHON_CALLER_UID"])
    CLIENT_GID = int(os.environ["VANTA_PYTHON_CLIENT_GID"])
    main()

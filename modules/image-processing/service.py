#!/usr/bin/env python3
import hashlib
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
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import artifact_io
import processing
import schemas
from sandbox import Sandbox

SOCKET_PATH = "/run/vantamcpd-image/image.sock"
MAX_REQUEST_BYTES = 10_000_000
MAX_RESPONSE_BYTES = 2_400_000
MODULE_DIR = os.environ.get("VANTA_IMAGE_INSTALL_DIR", "/opt/vantamcpd/modules/image-processing/current")
STATE_DIR = os.environ.get("VANTA_IMAGE_STATE_DIR", "/var/lib/vantamcpd-image")
BACKEND = os.environ.get("VANTA_IMAGE_BACKEND", "auto")
MEMORY_MB = int(os.environ.get("VANTA_IMAGE_MEMORY_MB", "384"))
TIMEOUT_MS = int(os.environ.get("VANTA_IMAGE_TIMEOUT_MS", "60000"))
CONCURRENT_CALLS = int(os.environ.get("VANTA_IMAGE_CONCURRENT_CALLS", "1"))
CALLS_PER_MINUTE = int(os.environ.get("VANTA_IMAGE_CALLS_PER_MINUTE", "12"))
ARTIFACT_ROOT = os.environ.get("VANTA_ARTIFACT_ROOT")

ACTIVE_CALLS = threading.BoundedSemaphore(CONCURRENT_CALLS)
RATE_LOCK = threading.Lock()
RECENT_CALLS: deque[float] = deque()
SANDBOX = Sandbox(MODULE_DIR, STATE_DIR, MEMORY_MB, TIMEOUT_MS, ARTIFACT_ROOT)

logging.basicConfig(level=logging.INFO, format="image-processing %(levelname)s %(message)s")
LOGGER = logging.getLogger("image-processing")


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
        if len(RECENT_CALLS) >= CALLS_PER_MINUTE:
            raise ValueError(f"image-processing accepts at most {CALLS_PER_MINUTE} calls per minute")
        RECENT_CALLS.append(now)


def _limits() -> dict[str, Any]:
    return {
        "inlineBytesEach": schemas.MAX_INLINE_BYTES,
        "artifactInputBytesTotal": schemas.MAX_ARTIFACT_BYTES,
        "outputBytes": schemas.MAX_OUTPUT_BYTES,
        "width": schemas.MAX_DIMENSION,
        "height": schemas.MAX_DIMENSION,
        "pixels": schemas.MAX_PIXELS,
        "sources": schemas.MAX_SOURCES,
        "edits": schemas.MAX_EDITS,
        "memoryMb": MEMORY_MB,
        "timeoutMs": TIMEOUT_MS,
        "concurrentCalls": CONCURRENT_CALLS,
        "callsPerMinute": CALLS_PER_MINUTE,
    }


def execute(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if action == "ping":
        return {"ok": True}
    request = schemas.validate(action, arguments)
    if action == "image_environment":
        return processing.environment(BACKEND, artifact_io.available(ARTIFACT_ROOT), _limits())
    return SANDBOX.run(action, request, BACKEND)


def _summary(action: str, arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return "-"
    references = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("artifactId"), str):
                references.append("artifact:" + value["artifactId"][:12])
            elif isinstance(value.get("data"), str):
                references.append("inline:" + hashlib.sha256(value["data"].encode("ascii", errors="ignore")).hexdigest()[:12])
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(arguments)
    return f"action={action} sources={','.join(references[:8]) or '-'}"


class RequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        started = time.monotonic()
        action = "unknown"
        arguments: dict[str, Any] = {}
        try:
            if _peer_uid(self.request) != EXPECTED_UID:
                raise PermissionError("caller is not authorized to use image-processing")
            size = struct.unpack("!I", _receive_exact(self.request, 4))[0]
            if size < 2 or size > MAX_REQUEST_BYTES:
                raise ValueError("request size is invalid")
            message = json.loads(_receive_exact(self.request, size))
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            action = message.get("action")
            arguments = message.get("arguments", {})
            if action not in ("ping", *schemas.TOOLS):
                raise ValueError("unknown broker action")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            if action == "ping":
                result = execute(action, arguments)
            else:
                _check_rate_limit(time.monotonic())
                if not ACTIVE_CALLS.acquire(timeout=5):
                    raise ValueError(f"image-processing is already running {CONCURRENT_CALLS} call(s); retry shortly")
                try:
                    result = execute(action, arguments)
                finally:
                    ACTIVE_CALLS.release()
            response = {"ok": True, "result": result}
            LOGGER.info("%s ok=true duration_ms=%d", _summary(action, arguments), round((time.monotonic() - started) * 1000))
        except Exception as error:
            response = {"ok": False, "error": str(error)}
            LOGGER.warning("%s ok=false duration_ms=%d error=%s", _summary(action, arguments), round((time.monotonic() - started) * 1000), type(error).__name__)
        payload = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = json.dumps({"ok": False, "error": "result exceeded the response limit; use artifact output"}, separators=(",", ":")).encode("utf-8")
        self.request.sendall(struct.pack("!I", len(payload)) + payload)


if hasattr(socketserver, "UnixStreamServer"):
    class BrokerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        request_queue_size = 4
else:
    class BrokerServer:
        def __init__(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("Unix-domain socket servers are unavailable on this platform")


def self_test() -> None:
    assert _limits()["pixels"] == schemas.MAX_PIXELS
    summary = _summary("image_inspect", {"source": {"data": "secret-inline-image", "mimeType": "image/png"}})
    assert "secret-inline-image" not in summary and "inline:" in summary
    assert execute("ping", {}) == {"ok": True}


def main() -> None:
    SANDBOX.purge_stale_calls()
    SANDBOX.probe()
    processing.environment(BACKEND, artifact_io.available(ARTIFACT_ROOT), _limits())
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o755, exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    with BrokerServer(SOCKET_PATH, RequestHandler) as server:
        os.chown(SOCKET_PATH, -1, CLIENT_GID)
        os.chmod(SOCKET_PATH, 0o660)
        LOGGER.info("broker ready backend=%s memory_mb=%d concurrency=%d", BACKEND, MEMORY_MB, CONCURRENT_CALLS)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    else:
        EXPECTED_UID = int(os.environ["VANTA_IMAGE_CALLER_UID"])
        CLIENT_GID = int(os.environ["VANTA_IMAGE_CLIENT_GID"])
        main()
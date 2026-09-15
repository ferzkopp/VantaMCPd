#!/usr/bin/env python3
import json
import logging
import os
import socket
import socketserver
import struct
import threading
import time
from collections import deque
from typing import Any

from browser import execute
from network_policy import sanitize_url

SOCKET_PATH = "/run/vantamcpd-browser/browser.sock"
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 262_144
MAX_CALLS_PER_MINUTE = 20
ACTIVE_CALLS = threading.BoundedSemaphore(1)
RATE_LOCK = threading.Lock()
RECENT_CALLS: deque[float] = deque()
EXPECTED_UID = int(os.environ["VANTA_BROWSER_CALLER_UID"])

logging.basicConfig(level=logging.INFO, format="browser-retrieval %(levelname)s %(message)s")
LOGGER = logging.getLogger("browser-retrieval")


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
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
        if len(RECENT_CALLS) >= MAX_CALLS_PER_MINUTE:
            raise ValueError("browser retrieval rate limit exceeded")
        RECENT_CALLS.append(now)


def _safe_log_url(arguments: Any) -> str:
    if not isinstance(arguments, dict) or not isinstance(arguments.get("url"), str):
        return "[none]"
    return sanitize_url(arguments["url"])


class BrowserRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        started = time.monotonic()
        action = "unknown"
        arguments: dict[str, Any] = {}
        try:
            if _peer_uid(self.request) != EXPECTED_UID:
                raise PermissionError("browser broker caller is not authorized")
            size = struct.unpack("!I", _receive_exact(self.request, 4))[0]
            if size < 2 or size > MAX_REQUEST_BYTES:
                raise ValueError("browser broker request size is invalid")
            request = json.loads(_receive_exact(self.request, size))
            if not isinstance(request, dict):
                raise ValueError("browser broker request must be an object")
            action = request.get("action")
            arguments = request.get("arguments", {})
            if action not in {"ping", "web_retrieve", "web_query", "web_tables"}:
                raise ValueError("unknown browser broker action")
            if not isinstance(arguments, dict):
                raise ValueError("browser arguments must be an object")
            if action != "ping":
                _check_rate_limit(time.monotonic())
                if not ACTIVE_CALLS.acquire(timeout=2):
                    raise ValueError("browser retrieval queue is full")
                try:
                    result = execute(action, arguments)
                finally:
                    ACTIVE_CALLS.release()
            else:
                result = execute(action, arguments)
            response = {"ok": True, "result": result}
            LOGGER.info("action=%s url=%s ok=true duration_ms=%d", action, _safe_log_url(arguments), round((time.monotonic() - started) * 1000))
        except Exception as error:
            response = {"ok": False, "error": str(error)}
            LOGGER.warning("action=%s url=%s ok=false duration_ms=%d error=%s", action, _safe_log_url(arguments), round((time.monotonic() - started) * 1000), type(error).__name__)
        payload = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = json.dumps({"ok": False, "error": "browser broker response exceeded its size limit"}, separators=(",", ":")).encode("utf-8")
        self.request.sendall(struct.pack("!I", len(payload)) + payload)


class BrowserServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 2


def main() -> None:
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o755, exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    with BrowserServer(SOCKET_PATH, BrowserRequestHandler) as server:
        os.chown(SOCKET_PATH, -1, int(os.environ["VANTA_BROWSER_CALLER_GID"]))
        os.chmod(SOCKET_PATH, 0o660)
        LOGGER.info("broker ready")
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import json
import logging
import os
import select
import socket
import socketserver
import struct
import threading
import time
from collections import deque
from typing import Any

from browser import execute
from download import download
from network_policy import sanitize_url

SOCKET_PATH = "/run/vantamcpd-browser/browser.sock"
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 262_144
MAX_CALLS_PER_MINUTE = 20
ACTIVE_CALLS = threading.BoundedSemaphore(1)
# A background download may wait minutes on Retry-After, so it must not hold the interactive slot.
BACKGROUND_CALLS = threading.BoundedSemaphore(1)
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


def _client_gone(connection: socket.socket) -> bool:
    """The adapter sends nothing after its request, so a readable socket means it closed."""
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        return bool(readable) and connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


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
            execution = request.get("execution", "immediate")
            if action not in {"ping", "web_retrieve", "web_discover_links", "web_query", "web_tables", "web_download"}:
                raise ValueError("unknown browser broker action")
            if not isinstance(arguments, dict):
                raise ValueError("browser arguments must be an object")
            if execution not in ("immediate", "background") or (execution == "background" and action != "web_download"):
                raise ValueError("invalid execution mode")
            if action == "ping":
                result = execute(action, arguments)
            elif execution == "background":
                _check_rate_limit(time.monotonic())
                if not BACKGROUND_CALLS.acquire(blocking=False):
                    raise ValueError("browser-retrieval is already running a background download on this node")
                try:
                    result = download(arguments, background=True, abandoned=lambda: _client_gone(self.request))
                finally:
                    BACKGROUND_CALLS.release()
            else:
                _check_rate_limit(time.monotonic())
                if not ACTIVE_CALLS.acquire(timeout=2):
                    raise ValueError("browser retrieval queue is full")
                try:
                    result = download(arguments, abandoned=lambda: _client_gone(self.request)) if action == "web_download" else execute(action, arguments)
                finally:
                    ACTIVE_CALLS.release()
            response = {"ok": True, "result": result}
            LOGGER.info("action=%s execution=%s url=%s ok=true duration_ms=%d", action, execution, _safe_log_url(arguments), round((time.monotonic() - started) * 1000))
        except Exception as error:
            response = {"ok": False, "error": str(error)}
            details = getattr(error, "details", None)
            if isinstance(details, dict) and details:
                response["details"] = details
            LOGGER.warning("action=%s url=%s ok=false duration_ms=%d error=%s status=%s", action, _safe_log_url(arguments), round((time.monotonic() - started) * 1000), type(error).__name__, (details or {}).get("status", "-"))
        payload = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = json.dumps({"ok": False, "error": "browser broker response exceeded its size limit"}, separators=(",", ":")).encode("utf-8")
        try:
            self.request.sendall(struct.pack("!I", len(payload)) + payload)
        except OSError:
            LOGGER.info("action=%s caller disconnected before the response", action)


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

#!/usr/bin/env python3
import json
import logging
import os
import socket
import socketserver
import struct
import threading
from typing import Any

from store import from_environment

SOCKET_PATH = "/run/vantamcpd-artifacts/artifacts.sock"
MAX_REQUEST_BYTES = 800_000
MAX_RESPONSE_BYTES = 800_000
EXPECTED_UID = int(os.environ["VANTA_ARTIFACT_CALLER_UID"])
STORE = from_environment()
STORE.initialize()

logging.basicConfig(level=logging.INFO, format="artifact-storage %(levelname)s %(message)s")
LOGGER = logging.getLogger("artifact-storage")


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = connection.recv(size)
        if not chunk:
            raise ValueError("client closed the request")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _peer_uid(connection: socket.socket) -> int:
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def execute(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if action == "ping":
        return {"protocolVersion": 1}
    operation, method = action.split("_", 1)
    handlers = {
        ("upload", "begin"): STORE.begin,
        ("upload", "append"): STORE.append,
        ("upload", "commit"): STORE.commit,
        ("upload", "abort"): STORE.abort,
        ("fetch", "info"): STORE.info,
        ("fetch", "read"): STORE.read,
        ("artifact", "list"): STORE.list,
        ("artifact", "delete"): STORE.delete,
        ("artifact", "update"): STORE.update,
    }
    handler = handlers.get((operation, method))
    if handler is None:
        raise ValueError("unknown artifact broker action")
    return handler(arguments)


class ArtifactRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        action = "unknown"
        try:
            if _peer_uid(self.request) != EXPECTED_UID:
                raise PermissionError("artifact broker caller is not authorized")
            size = struct.unpack("!I", _receive_exact(self.request, 4))[0]
            if size < 2 or size > MAX_REQUEST_BYTES:
                raise ValueError("artifact broker request size is invalid")
            request = json.loads(_receive_exact(self.request, size))
            action = request.get("action")
            arguments = request.get("arguments", {})
            if not isinstance(action, str) or not isinstance(arguments, dict):
                raise ValueError("artifact broker request is invalid")
            response = {"ok": True, "result": execute(action, arguments)}
            LOGGER.info("action=%s ok=true", action)
        except Exception as error:
            response = {"ok": False, "error": str(error)}
            LOGGER.warning("action=%s ok=false error=%s", action, type(error).__name__)
        payload = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = json.dumps({"ok": False, "error": "artifact broker response exceeded its size limit"}).encode()
        self.request.sendall(struct.pack("!I", len(payload)) + payload)


class ArtifactServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 8


def _gc_loop(interval_seconds: int) -> None:
    while True:
        threading.Event().wait(interval_seconds)
        try:
            result = STORE.gc()
            if result["artifactsRemoved"] or result["uploadsRemoved"]:
                LOGGER.info("gc artifacts=%d uploads=%d", result["artifactsRemoved"], result["uploadsRemoved"])
        except Exception:
            LOGGER.exception("artifact GC failed")


def main() -> None:
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o755, exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    interval = int(os.environ.get("VANTA_ARTIFACT_GC_INTERVAL_MINUTES", "15")) * 60
    threading.Thread(target=_gc_loop, args=(interval,), daemon=True).start()
    with ArtifactServer(SOCKET_PATH, ArtifactRequestHandler) as server:
        os.chown(SOCKET_PATH, -1, int(os.environ["VANTA_ARTIFACT_CALLER_GID"]))
        os.chmod(SOCKET_PATH, 0o660)
        LOGGER.info("broker ready")
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
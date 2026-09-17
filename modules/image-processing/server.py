#!/usr/bin/env python3
import json
import os
import socket
import struct
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from schemas import TOOLS, self_test as schemas_self_test

PROTOCOL_VERSION = "2025-06-18"
VERSION = "0.2.5"
SOCKET_PATH = "/run/vantamcpd-image/image.sock"
MAX_BROKER_MESSAGE = 2_400_000
MAX_RESPONSE_BYTES = 3_900_000

INSTRUCTIONS = (
    "Inputs must be PNG, JPEG, WebP, or GIF images supplied as inline base64 or artifact IDs; filesystem paths "
    "and URLs are not accepted. Prefer artifact output for results near or above 1 MiB. GIF operations use the first frame."
)


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(min(remaining, 65_536))
        if not chunk:
            raise ValueError("image-processing broker closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def broker_call(action: str, arguments: dict[str, Any], timeout: float = 140.0) -> dict[str, Any]:
    payload = json.dumps({"action": action, "arguments": arguments}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > 10_000_000:
        raise ValueError("request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(SOCKET_PATH)
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        size = struct.unpack("!I", _receive_exact(connection, 4))[0]
        if size < 2 or size > MAX_BROKER_MESSAGE:
            raise ValueError("broker response size is invalid")
        response = json.loads(_receive_exact(connection, size))
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = response.get("error") if isinstance(response, dict) else None
        raise ValueError(str(detail or "image-processing call failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("broker returned an invalid result")
    return result


def tool_result(value: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    result = {"content": [{"type": "text", "text": text}], "structuredContent": value}
    if len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("result exceeds the MCP response limit; use artifact output")
    return result


def listed_tools() -> list[dict[str, Any]]:
    read_only = {"image_environment", "image_inspect"}
    return [
        {
            "name": name,
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
            "annotations": {
                "readOnlyHint": name in read_only,
                "destructiveHint": False,
                "idempotentHint": name in read_only,
                "openWorldHint": False,
            },
        }
        for name, tool in TOOLS.items()
    ]


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        if method == "initialize":
            broker_call("ping", {}, timeout=5.0)
            result = {
                "protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vanta-image-processing", "version": VERSION},
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            broker_call("ping", {}, timeout=5.0)
            result = {}
        elif method == "tools/list":
            result = {"tools": listed_tools()}
        elif method == "tools/call":
            parameters = message.get("params", {})
            name = parameters.get("name")
            if name not in TOOLS:
                raise ValueError(f"unknown tool: {name}")
            arguments = parameters.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            result = tool_result(broker_call(name, arguments))
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as error:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(error)}}


def self_test() -> None:
    schemas_self_test()
    assert list(TOOLS) == ["image_environment", "image_inspect", "image_edit", "image_compose", "image_compare"]
    advertised = listed_tools()
    assert advertised[0]["annotations"]["readOnlyHint"] is True
    assert advertised[2]["annotations"]["readOnlyHint"] is False
    assert all(tool["annotations"]["openWorldHint"] is False for tool in advertised)
    assert json.loads(tool_result({"ok": True})["content"][0]["text"])["ok"] is True
    unknown = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "image_shell", "arguments": {}}})
    assert unknown["result"]["isError"] is True
    assert handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
        return
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    for raw_line in sys.stdin:
        try:
            message = json.loads(raw_line)
            response = handle_request(message)
            if response is not None:
                print(json.dumps(response, separators=(",", ":"), ensure_ascii=False), flush=True)
        except Exception as error:
            print(f"image-processing protocol error: {error}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
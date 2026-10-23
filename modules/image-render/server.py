#!/usr/bin/env python3
"""MCP stdio adapter for Image Render.

VantaMCPd launches this over SSH as the configured cluster user. It speaks MCP on stdin/stdout and forwards
calls to the local broker socket; it never parses or renders scenes itself.
"""
import json
import os
import socket
import struct
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from schemas import TOOLS, self_test as schemas_self_test

PROTOCOL_VERSION = "2025-06-18"
VERSION = "0.3.1"
SOCKET_PATH = "/run/vantamcpd-render/render.sock"
MAX_REQUEST_BYTES = 4_200_000
MAX_BROKER_MESSAGE = 3_900_000
MAX_RESPONSE_BYTES = 3_950_000
IMMEDIATE_SOCKET_TIMEOUT = 1_850.0
# The broker enforces the node's background ceiling; this only has to outlast the longest allowed render.
BACKGROUND_SOCKET_TIMEOUT = 86_400 + 300.0
READ_ONLY = {"render_environment", "render_docs", "render_library", "render_check"}

INSTRUCTIONS = (
    "Image Render ray traces POV-Ray 3.7 scenes. Call render_environment first, verify unfamiliar syntax with "
    "render_docs and render_library (declarations finds predefined textures and colors), iterate with render_check, "
    "then render_scene. Reference staged artifacts by bare name through assets[] or as \"artifact:<id>\" string "
    "literals. For assets beyond the installed starter set, use render_polyhaven with browser-retrieval web_download "
    "and credit Poly Haven. Submit large renders with execution=\"background\"."
)


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(min(remaining, 65_536))
        if not chunk:
            raise ValueError("image-render broker closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def broker_call(action: str, arguments: dict[str, Any], timeout: float = IMMEDIATE_SOCKET_TIMEOUT, execution: str = "immediate") -> dict[str, Any]:
    request: dict[str, Any] = {"action": action, "arguments": arguments}
    if execution != "immediate":
        request["execution"] = execution
    payload = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("request is too large; stage large scenes and files as artifacts")
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
        raise ValueError(str(detail or "image-render call failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("broker returned an invalid result")
    return result


def tool_result(value: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    result = {"content": [{"type": "text", "text": text}]}
    if len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("result exceeds the MCP response limit; use artifact output or read less text")
    if value.get("ok") is False:
        result["isError"] = True
    return result


def listed_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
            "annotations": {
                "readOnlyHint": name in READ_ONLY,
                "destructiveHint": False,
                "idempotentHint": name in READ_ONLY,
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
                "serverInfo": {"name": "vanta-image-render", "version": VERSION},
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
            meta = parameters.get("_meta")
            if isinstance(meta, dict) and meta.get("vantamcpd/execution") == "background":
                result = tool_result(broker_call(name, arguments, timeout=BACKGROUND_SOCKET_TIMEOUT, execution="background"))
            else:
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
    assert list(TOOLS) == ["render_environment", "render_docs", "render_library", "render_polyhaven", "render_check", "render_scene", "render_benchmark"]
    advertised = {tool["name"]: tool for tool in listed_tools()}
    assert advertised["render_docs"]["annotations"]["readOnlyHint"] is True
    assert advertised["render_scene"]["annotations"]["readOnlyHint"] is False
    assert all(tool["annotations"]["openWorldHint"] is False for tool in advertised.values())
    assert all(tool["inputSchema"].get("type") == "object" for tool in advertised.values()), "MCP requires object input schemas"
    assert tool_result({"ok": False, "diagnostics": []})["isError"] is True
    unknown = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "render_shell", "arguments": {}}})
    assert unknown["result"]["isError"] is True
    assert handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle_request({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})["error"]["code"] == -32601


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
            print(f"image-render protocol error: {error}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

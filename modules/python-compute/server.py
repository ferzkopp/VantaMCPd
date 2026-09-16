#!/usr/bin/env python3
"""MCP stdio adapter for Python Compute.

VantaMCPd launches this over SSH as the configured cluster user. It speaks MCP on stdin/stdout and
forwards validated calls to the local broker socket; it never executes submitted code itself.
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
VERSION = "0.2.2"
SOCKET_PATH = "/run/vantamcpd-python/python.sock"
MAX_BROKER_MESSAGE = 2_000_000
MAX_RESPONSE_BYTES = 2_000_000
# Headroom for the JSON-RPC envelope written around the tool content on stdout.
RESPONSE_ENVELOPE_RESERVE = 256

INSTRUCTIONS = (
    "Call python_env before writing code to learn which modules the node provides. Submitted code runs in a "
    "sandbox without network access and keeps no state between calls."
)


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(min(remaining, 65_536))
        if not chunk:
            raise ValueError("python-compute broker closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def broker_call(action: str, arguments: dict[str, Any], timeout: float = 650.0) -> dict[str, Any]:
    payload = json.dumps({"action": action, "arguments": arguments}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > 524_288:
        raise ValueError("request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(SOCKET_PATH)
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        size = struct.unpack("!I", _receive_exact(connection, 4))[0]
        if size > MAX_BROKER_MESSAGE:
            raise ValueError("broker response is too large")
        response = json.loads(_receive_exact(connection, size))
    if not isinstance(response, dict):
        raise ValueError("broker returned an invalid response")
    if response.get("ok") is not True:
        raise ValueError(str(response.get("error", "python-compute call failed")))
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("broker returned an invalid result")
    return result


def encoded_response(value: dict[str, Any]) -> tuple[dict[str, Any], int]:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    content = {"content": [{"type": "text", "text": text}]}
    serialized = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return content, len(serialized.encode("utf-8")) + RESPONSE_ENVELOPE_RESERVE


def tool_result(value: dict[str, Any]) -> dict[str, Any]:
    value["responseLimitBytes"] = MAX_RESPONSE_BYTES
    value["responseBytes"] = 0
    value["responseLimitPercent"] = 0
    content, response_bytes = encoded_response(value)
    # JSON-escaping the result inflates it, so the reported size is the transport cost, not the raw result.
    for _ in range(5):
        if response_bytes == value["responseBytes"]:
            break
        value["responseBytes"] = response_bytes
        value["responseLimitPercent"] = round(response_bytes * 100 / MAX_RESPONSE_BYTES, 2)
        content, response_bytes = encoded_response(value)
    if response_bytes > MAX_RESPONSE_BYTES:
        raise ValueError(f"result needs {response_bytes} bytes and exceeds the {MAX_RESPONSE_BYTES}-byte MCP response limit; print less output or emit fewer artifacts")
    return content


def listed_tools() -> list[dict[str, Any]]:
    annotations = {
        "python_env": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "python_run": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    }
    return [{"name": name, "description": tool["description"], "inputSchema": tool["inputSchema"], "annotations": annotations[name]} for name, tool in TOOLS.items()]


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
                "serverInfo": {"name": "vanta-python-compute", "version": VERSION},
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
        # Only tools/call reports failure as tool content; other methods must fail as JSON-RPC errors.
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(error)}}


def self_test() -> None:
    schemas_self_test()
    assert list(TOOLS) == ["python_env", "python_run"]
    for tool in listed_tools():
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["openWorldHint"] is False
        assert len(tool["description"]) > 100
    assert listed_tools()[0]["annotations"]["readOnlyHint"] is True
    assert listed_tools()[1]["annotations"]["readOnlyHint"] is False
    sized = json.loads(tool_result({"ok": True})["content"][0]["text"])
    assert sized["responseBytes"] > 0
    assert sized["responseLimitBytes"] == MAX_RESPONSE_BYTES
    try:
        tool_result({"stdout": "x" * MAX_RESPONSE_BYTES})
    except ValueError:
        pass
    else:
        raise AssertionError("an oversized result was accepted")
    unknown = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "python_eval"}})
    assert unknown["result"]["isError"] is True
    assert handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle_request({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})["error"]["code"] == -32601
    assert handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"][0]["name"] == "python_env"


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
            print(f"python-compute protocol error: {error}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

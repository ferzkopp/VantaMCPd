#!/usr/bin/env python3
import json
import socket
import struct
import sys
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
VERSION = "0.1.1"
SOCKET_PATH = "/run/vantamcpd-artifacts/artifacts.sock"
MAX_MESSAGE_BYTES = 800_000

ARTIFACT_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
UPLOAD_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}

TOOLS = {
    "artifact_upload": {
        "description": "Upload one immutable artifact in bounded sequential chunks. Call begin, append until complete, then commit; abort discards an unfinished upload.",
        "inputSchema": {
            "type": "object",
            "oneOf": [
                {"properties": {"operation": {"const": "begin"}, "name": {"type": "string", "minLength": 1, "maxLength": 128}, "bytes": {"type": "integer", "minimum": 0}, "mimeType": {"type": "string", "maxLength": 200}, "producer": {"type": "string", "maxLength": 64, "default": "agent"}, "retentionDays": {"type": "integer", "minimum": 1}, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}, "required": ["operation", "name", "bytes"], "additionalProperties": False},
                {"properties": {"operation": {"const": "append"}, "uploadId": UPLOAD_ID, "offset": {"type": "integer", "minimum": 0}, "data": {"type": "string", "maxLength": 699060}, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}, "required": ["operation", "uploadId", "offset", "data"], "additionalProperties": False},
                {"properties": {"operation": {"const": "commit"}, "uploadId": UPLOAD_ID}, "required": ["operation", "uploadId"], "additionalProperties": False},
                {"properties": {"operation": {"const": "abort"}, "uploadId": UPLOAD_ID}, "required": ["operation", "uploadId"], "additionalProperties": False},
            ],
        },
    },
    "artifact_fetch": {
        "description": "Inspect artifact metadata or download a bounded byte range as base64 with a chunk checksum.",
        "inputSchema": {
            "type": "object",
            "oneOf": [
                {"properties": {"operation": {"const": "info"}, "artifactId": ARTIFACT_ID}, "required": ["operation", "artifactId"], "additionalProperties": False},
                {"properties": {"operation": {"const": "read"}, "artifactId": ARTIFACT_ID, "offset": {"type": "integer", "minimum": 0, "default": 0}, "length": {"type": "integer", "minimum": 1, "maximum": 524288, "default": 524288}}, "required": ["operation", "artifactId"], "additionalProperties": False},
            ],
        },
    },
    "artifact_list": {
        "description": "List unexpired artifacts and quota usage with bounded cursor pagination.",
        "inputSchema": {"type": "object", "properties": {"cursor": ARTIFACT_ID, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}, "producer": {"type": "string", "maxLength": 64}}, "additionalProperties": False},
    },
    "artifact_delete": {
        "description": "Permanently delete one artifact after explicit confirmation.",
        "inputSchema": {"type": "object", "properties": {"artifactId": ARTIFACT_ID, "confirm": {"const": True}}, "required": ["artifactId", "confirm"], "additionalProperties": False},
    },
    "artifact_update": {
        "description": "Set an artifact's remaining retention period within the configured maximum.",
        "inputSchema": {"type": "object", "properties": {"artifactId": ARTIFACT_ID, "retentionDays": {"type": "integer", "minimum": 1}}, "required": ["artifactId", "retentionDays"], "additionalProperties": False},
    },
}


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = connection.recv(size)
        if not chunk:
            raise ValueError("artifact broker closed the connection")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def broker_call(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"action": action, "arguments": arguments}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("artifact request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(115)
        connection.connect(SOCKET_PATH)
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        size = struct.unpack("!I", _receive_exact(connection, 4))[0]
        if size > MAX_MESSAGE_BYTES:
            raise ValueError("artifact broker response is too large")
        response = json.loads(_receive_exact(connection, size))
    if response.get("ok") is not True:
        raise ValueError(str(response.get("error", "artifact operation failed")))
    return response["result"]


def listed_tools() -> list[dict[str, Any]]:
    tools = []
    for name, tool in TOOLS.items():
        destructive = name == "artifact_delete"
        read_only = name in {"artifact_fetch", "artifact_list"}
        tools.append({**tool, "name": name, "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive, "idempotentHint": read_only, "openWorldHint": False}})
    return tools


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        if method == "initialize":
            broker_call("ping", {})
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION), "capabilities": {"tools": {}}, "serverInfo": {"name": "vanta-artifact-storage", "version": VERSION}}
        elif method == "ping":
            broker_call("ping", {})
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
            operation = arguments.get("operation")
            action = f"{name.removeprefix('artifact_')}_{operation}" if operation else name
            value = broker_call(action, arguments)
            text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            result = {"content": [{"type": "text", "text": text}], "structuredContent": value}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as error:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(error)}}


def self_test() -> None:
    assert list(TOOLS) == ["artifact_upload", "artifact_fetch", "artifact_list", "artifact_delete", "artifact_update"]
    assert TOOLS["artifact_upload"]["inputSchema"]["oneOf"][1]["properties"]["data"]["maxLength"] < MAX_MESSAGE_BYTES
    assert next(tool for tool in listed_tools() if tool["name"] == "artifact_delete")["annotations"]["destructiveHint"] is True


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = handle(message)
            if response is not None:
                print(json.dumps(response, separators=(",", ":"), ensure_ascii=False), flush=True)
        except Exception as error:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(error)}}), flush=True)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
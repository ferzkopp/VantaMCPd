#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from typing import Any

from corpus import get_paper, info, search, self_test, verify, connect

PROTOCOL_VERSION = "2025-06-18"
VERSION = "0.1.0"


def database_path() -> Path:
    data_directory = os.environ.get("VANTA_MODULE_DATA_DIR")
    if not data_directory:
        raise ValueError("VANTA_MODULE_DATA_DIR is not set")
    database = Path(data_directory) / "corpus.db"
    if not database.is_file():
        raise ValueError("corpus database is not provisioned")
    return database


TOOLS = {
    "corpus_search": {
        "description": "Search the installed scientific metadata corpus using bounded BM25 full-text ranking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "category": {"type": "string", "maxLength": 40},
                "publishedFrom": {"type": "string", "description": "Inclusive YYYY-MM-DD date."},
                "publishedTo": {"type": "string", "description": "Inclusive YYYY-MM-DD date."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "offset": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0}
            },
            "required": ["query"],
            "additionalProperties": False
        },
        "handler": lambda arguments: search(database_path(), arguments),
    },
    "corpus_get": {
        "description": "Return one exact arXiv metadata record from the installed corpus.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "maxLength": 100}},
            "required": ["id"],
            "additionalProperties": False
        },
        "handler": lambda arguments: get_paper(database_path(), arguments.get("id")),
    },
    "corpus_info": {
        "description": "Describe the installed corpus profile, provenance, record counts, size, and refresh cutoff.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": lambda _arguments: info(database_path()),
    },
}


def tool_result(value: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value}


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vanta-corpus-search", "version": VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": tool["description"], "inputSchema": tool["inputSchema"]} for name, tool in TOOLS.items()]}
        elif method == "tools/call":
            parameters = message.get("params", {})
            tool = TOOLS.get(parameters.get("name"))
            if tool is None:
                raise ValueError(f"unknown tool: {parameters.get('name')}")
            arguments = parameters.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            result = tool_result(tool["handler"](arguments))
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as error:
        return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
        if os.environ.get("VANTA_MODULE_DATA_DIR"):
            with connect(database_path(), readonly=True) as connection:
                verify(connection)
        print("ok")
        return
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = handle_request(message)
        except Exception as error:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(error)}}
        if response is not None:
            print(json.dumps(response, separators=(",", ":"), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
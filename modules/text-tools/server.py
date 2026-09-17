#!/usr/bin/env python3
import json
import sys
from typing import Any

from operations import CATEGORIES, category_tools

PROTOCOL_VERSION = "2025-06-18"
VERSION = "0.5.3"

TOOLS = category_tools()


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
                "serverInfo": {"name": "vanta-text-tools", "version": VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": tool["description"], "inputSchema": tool["inputSchema"]} for name, tool in TOOLS.items()]}
        elif method == "tools/call":
            params = message.get("params", {})
            name = params.get("name")
            tool = TOOLS.get(name)
            if tool is None:
                raise ValueError(f"unknown tool: {name}")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            result = tool_result(tool["handler"](arguments))
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as error:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": str(error)}], "isError": True},
        }


def self_test() -> None:
    assert len(CATEGORIES) == 12 and set(TOOLS) == set(CATEGORIES)

    def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return TOOLS[tool]["handler"](arguments)

    def call_optional(tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Optional Debian packages may be absent; the operation must then name the missing one."""
        try:
            return call(tool, arguments)
        except ValueError as error:
            if "which is not installed on this node" not in str(error):
                raise
            return None

    assert call("text_transform", {"operation": "case_convert", "text": "hello world", "style": "camel"})["text"] == "helloWorld"
    assert call("data_convert", {"operation": "json_format", "text": '{"ok":true}', "compact": True})["text"] == '{"ok":true}'
    assert call("text_security", {"operation": "digest", "text": "abc", "algorithm": "sha256"})["hex"].startswith("ba7816")
    assert call("developer_text", {"operation": "regex_extract", "text": "id=12 id=34", "pattern": r"id=(\d+)"})["matches"][1]["groups"] == ["34"]
    assert call("data_convert", {"operation": "csv_normalize", "text": "a;b\n1;2"})["text"] == "a,b\n1,2\n"
    # Ragged rows defeat csv.Sniffer, and they are exactly what padding is for.
    assert call("data_convert", {"operation": "csv_normalize", "text": "a;b;c\n1;2"})["text"] == "a,b,c\n1,2,\n"
    assert call("data_convert", {"operation": "kv_to_json", "text": "level=info code:200"})["value"][0]["fields"]["code"] == "200"
    assert call("data_convert", {"operation": "html_to_json", "text": "<h1>Hello</h1><a href='/x'>Next</a><script>bad()</script>"})["text"] == "Hello\nNext"

    assert call("text_transform", {"operation": "line_slice", "text": "a\nb\nc\nd", "start": 2, "end": 3})["text"] == "b\nc"
    assert call("text_transform", {"operation": "replace_literal", "text": "a.b.c", "search": ".", "replacement": "-"})["text"] == "a-b-c"
    assert call("text_transform", {"operation": "ascii_fold", "text": "café straße"})["text"] == "cafe strasse"
    assert call("text_transform", {"operation": "bytes_humanize", "bytes": 1536})["text"] == "1.5 KiB"
    assert call("text_transform", {"operation": "number_format", "value": 1234567.891, "decimals": 2})["text"] == "1,234,567.89"
    assert call("text_transform", {"operation": "truncate", "text": "one two three", "maxCharacters": 9})["text"] == "one two\u2026"
    assert call("text_analyze", {"operation": "text_inspect", "text": "a\r\nb\n"})["lineEndings"] == "mixed"
    assert call("text_analyze", {"operation": "duplicate_lines", "text": "a\nb\na"})["items"][0]["count"] == 2
    assert len(call("text_analyze", {"operation": "chunk", "text": "one\n\ntwo\n\nthree", "maxCharacters": 100})["items"]) == 1
    # "beta" is more frequent but appears in both documents, so IDF must rank "alpha" first.
    assert call("text_analyze", {"operation": "tfidf", "documents": ["alpha alpha beta", "beta beta beta"]})["items"][0]["terms"][0]["term"] == "alpha"
    assert call("text_extract", {"operation": "semvers", "text": "v1.2.3 and 4.5.6-rc.1"})["items"] == ["1.2.3", "4.5.6-rc.1"]

    assert call("data_convert", {"operation": "jsonl_to_json", "text": '{"a":1}\n{"a":2}'})["records"] == 2
    assert call("data_convert", {"operation": "json_flatten", "text": '{"a":{"b":[1,2]}}'})["value"] == {"a.b[0]": 1, "a.b[1]": 2}
    assert call("data_convert", {"operation": "json_unflatten", "text": '{"a.b[0]":1,"a.b[1]":2}'})["value"] == {"a": {"b": [1, 2]}}
    assert call("data_convert", {"operation": "json_diff", "text": '{"a":1}', "otherText": '{"a":2,"b":3}'})["changed"] == 1
    assert call("data_convert", {"operation": "json_merge", "text": '{"a":{"x":1}}', "otherText": '{"a":{"y":2}}'})["value"] == {"a": {"x": 1, "y": 2}}
    assert call("data_convert", {"operation": "env_to_json", "text": 'export A=1\nB="two"'})["value"] == {"A": "1", "B": "two"}
    assert call("data_convert", {"operation": "json_schema_infer", "text": '{"a":1}'})["value"]["properties"]["a"]["type"] == "integer"
    assert call("data_convert", {"operation": "json_to_ini", "text": '{"main":{"name":"test"}}'})["text"].startswith("[main]")
    parsed_yaml = call_optional("data_convert", {"operation": "yaml_to_json", "text": "name: test\nitems:\n  - 1\n"})
    assert parsed_yaml is None or parsed_yaml["value"] == {"name": "test", "items": [1]}
    rendered_yaml = call_optional("data_convert", {"operation": "json_to_yaml", "text": '{"name":"test"}'})
    assert rendered_yaml is None or "name: test" in rendered_yaml["text"]

    scanned = call("text_security", {"operation": "secret_scan", "text": "aws_key = AKIAIOSFODNN7EXAMPLE"})
    assert scanned["items"][0]["type"] == "aws-access-key-id" and "AKIAIOSFODNN7EXAMPLE" not in json.dumps(scanned)
    assert call("text_security", {"operation": "redact", "text": "mail a@b.com and a@b.com", "categories": ["emails"]})["text"] == "mail [EMAIL_1] and [EMAIL_1]"
    assert call("text_security", {"operation": "redact", "text": "from 10.0.0.5", "categories": ["ip_addresses"]})["text"] == "from [IP_1]"
    assert call("text_security", {"operation": "password_strength", "text": "aaaa"})["verdict"] == "very weak"

    assert call("developer_text", {"operation": "regex_test", "pattern": r"^a(\d+)", "samples": ["a12", "b"]})["matchedSamples"] == 1
    assert call("developer_text", {"operation": "regex_test", "pattern": "a(", "samples": ["a"]})["valid"] is False
    assert call("developer_text", {"operation": "strip_comments", "text": "x = 1  # note\n"})["removed"] == 1
    assert call("document_process", {"operation": "split_sections", "text": "# T\n\n## A\n\nbody\n\n## B\n", "level": 2})["count"] == 3
    assert call("document_process", {"operation": "tables_extract", "text": "| a | b |\n| --- | ---: |\n| 1 | 2 |\n"})["items"][0]["rows"] == [{"a": "1", "b": "2"}]
    assert call("document_process", {"operation": "links_extract", "text": "[x](https://e.com)"})["items"][0]["absolute"] is True
    assert call("document_process", {"operation": "heading_shift", "text": "# A\n", "by": 1})["text"] == "## A"

    assert call("datetime_text", {"operation": "parse", "text": "2026-09-13T12:00:00Z"})["epochSeconds"] == 1789300800.0
    converted = call_optional("datetime_text", {"operation": "convert_timezone", "text": "2026-09-13T12:00:00Z", "toTimezone": "Europe/Berlin"})
    assert converted is None or converted["text"].startswith("2026-09-13T14:00:00")
    assert call("datetime_text", {"operation": "difference", "text": "2026-01-01T00:00:00Z", "otherText": "2026-01-02T00:00:00Z", "unit": "days"})["days"] == 1
    assert call("datetime_text", {"operation": "duration_humanize", "seconds": 3661, "parts": 3})["text"] == "1h 1m 1s"
    assert call("datetime_text", {"operation": "duration_parse", "text": "1h 30m"})["seconds"] == 5400.0

    rendered_html = call_optional("document_process", {"operation": "markdown_to_html", "text": "# Title\n"})
    assert rendered_html is None or "<h1>Title</h1>" in rendered_html["text"]
    plural = call_optional("text_transform", {"operation": "pluralize", "text": "index"})
    assert plural is None or plural["text"] in {"indices", "indexes"}
    passphrase = call_optional("text_generate", {"operation": "passphrase", "words": 4})
    assert passphrase is None or passphrase["text"].count("-") == 3
    toml_rendered = call_optional("data_convert", {"operation": "json_to_toml", "text": '{"name":"test"}'})
    assert toml_rendered is None or 'name = "test"' in toml_rendered["text"]


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
            print(f"text-tools protocol error: {error}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
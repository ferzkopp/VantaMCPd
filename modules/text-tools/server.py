#!/usr/bin/env python3
import csv
import html
from html.parser import HTMLParser
import io
import json
import multiprocessing
import re
import sys
from typing import Any

from operations import CATEGORIES, category_tools

PROTOCOL_VERSION = "2025-06-18"
MAX_TEXT = 262_144
MAX_PATTERN = 2_048
MAX_RESULTS = 1_000
REGEX_TIMEOUT_SECONDS = 2.0


def require_text(arguments: dict[str, Any]) -> str:
    value = arguments.get("text")
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    if len(value.encode("utf-8")) > MAX_TEXT:
        raise ValueError(f"text exceeds {MAX_TEXT} UTF-8 bytes")
    return value


def regex_worker(pattern: str, text: str, flags: int, limit: int, output: multiprocessing.Queue) -> None:
    try:
        matches = []
        for match in re.finditer(pattern, text, flags):
            matches.append({
                "match": match.group(0),
                "groups": list(match.groups()),
                "namedGroups": match.groupdict(),
                "start": match.start(),
                "end": match.end(),
            })
            if len(matches) >= limit:
                break
        output.put({"matches": matches, "truncated": len(matches) >= limit})
    except Exception as error:
        output.put({"error": str(error)})


def regex_extract(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f"pattern exceeds {MAX_PATTERN} characters")
    limit = arguments.get("maxMatches", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_RESULTS:
        raise ValueError(f"maxMatches must be an integer from 1 to {MAX_RESULTS}")

    flags = 0
    requested_flags = arguments.get("flags", [])
    if not isinstance(requested_flags, list) or not all(isinstance(item, str) for item in requested_flags):
        raise ValueError("flags must be an array of strings")
    allowed = {"ignoreCase": re.IGNORECASE, "multiline": re.MULTILINE, "dotAll": re.DOTALL}
    unknown = sorted(set(requested_flags) - set(allowed))
    if unknown:
        raise ValueError(f"unsupported regex flags: {', '.join(unknown)}")
    for name in requested_flags:
        flags |= allowed[name]

    output: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(target=regex_worker, args=(pattern, text, flags, limit, output))
    process.start()
    process.join(REGEX_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join()
        raise ValueError("regex evaluation exceeded 2 seconds")
    if output.empty():
        raise ValueError("regex worker failed without a result")
    result = output.get()
    if "error" in result:
        raise ValueError(result["error"])
    return result


def csv_normalize(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    delimiter = arguments.get("delimiter")
    if delimiter is not None and (not isinstance(delimiter, str) or len(delimiter) != 1):
        raise ValueError("delimiter must be one character")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","

    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if len(rows) > 10_000:
        raise ValueError("CSV exceeds 10000 rows")
    width = max((len(row) for row in rows), default=0)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(row + [""] * (width - len(row)) for row in rows)
    return {"csv": output.getvalue(), "inputDelimiter": delimiter, "rows": len(rows), "columns": width}


def log_parse_kv(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    records = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = {}
        for match in re.finditer(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_.-]*)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)", line):
            fields[match.group(1)] = match.group(2).strip("\"'")
        records.append({"line": line_number, "fields": fields, "raw": line})
        if len(records) >= MAX_RESULTS:
            break
    return {"records": records, "truncated": len(records) >= MAX_RESULTS}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.heading: str | None = None
        self.text: list[str] = []
        self.headings: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.current_link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading = tag
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.current_link = {"href": href, "text": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"} and self.hidden:
            self.hidden -= 1
        if tag == self.heading:
            self.heading = None
        if tag == "a" and self.current_link is not None:
            self.links.append(self.current_link)
            self.current_link = None

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        self.text.append(cleaned)
        if self.heading:
            self.headings.append({"level": self.heading, "text": cleaned})
        if self.current_link is not None:
            self.current_link["text"] = " ".join(filter(None, [self.current_link["text"], cleaned]))


def html_extract(arguments: dict[str, Any]) -> dict[str, Any]:
    source = require_text(arguments)
    parser = TextExtractor()
    parser.feed(source)
    parser.close()
    return {
        "text": html.unescape("\n".join(parser.text)),
        "headings": parser.headings[:MAX_RESULTS],
        "links": parser.links[:MAX_RESULTS],
        "truncated": len(parser.headings) > MAX_RESULTS or len(parser.links) > MAX_RESULTS,
    }


TOOLS = {
    "regex_extract": {
        "description": "Extract bounded regular-expression matches and capture groups from text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "pattern": {"type": "string"},
                "flags": {"type": "array", "items": {"enum": ["ignoreCase", "multiline", "dotAll"]}},
                "maxMatches": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
            },
            "required": ["text", "pattern"],
            "additionalProperties": False,
        },
        "handler": regex_extract,
    },
    "csv_normalize": {
        "description": "Normalize delimiter-separated text to RFC-style comma-separated CSV.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "delimiter": {"type": "string", "minLength": 1, "maxLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": csv_normalize,
    },
    "log_parse_kv": {
        "description": "Parse key=value and key:value fields from bounded log lines.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": log_parse_kv,
    },
    "html_extract": {
        "description": "Extract visible text, headings, and links from caller-provided HTML.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": html_extract,
    },
}
TOOLS.update(category_tools())


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
                "serverInfo": {"name": "vanta-text-tools", "version": "0.2.0"},
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
    assert regex_extract({"text": "id=12 id=34", "pattern": r"id=(\d+)"})["matches"][1]["groups"] == ["34"]
    assert csv_normalize({"text": "a;b\n1;2"})["csv"] == "a,b\n1,2\n"
    assert log_parse_kv({"text": "level=info code:200"})["records"][0]["fields"]["code"] == "200"
    assert html_extract({"text": "<h1>Hello</h1><a href='/x'>Next</a><script>bad()</script>"})["text"] == "Hello\nNext"
    assert len(CATEGORIES) == 11
    assert TOOLS["text_transform"]["handler"]({"operation": "case_convert", "text": "hello world", "style": "camel"})["text"] == "helloWorld"
    assert TOOLS["data_convert"]["handler"]({"operation": "json_format", "text": '{"ok":true}', "compact": True})["text"] == '{"ok":true}'
    assert TOOLS["text_security"]["handler"]({"operation": "digest", "text": "abc", "algorithm": "sha256"})["hex"].startswith("ba7816")


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
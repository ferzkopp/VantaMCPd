#!/usr/bin/env python3
import base64
import binascii
import configparser
import csv
import hashlib
import hmac as hmac_module
import html
from html.parser import HTMLParser
import io
import json
import math
import os
import re
import secrets
import string
import tempfile
import tomllib
import urllib.parse
import uuid as uuid_module
import xml.etree.ElementTree as element_tree
import zlib
from collections import Counter
from typing import Any

import artifact_io

from operation_common import (
    MAX_RESULTS,
    MAX_TABLE_COLUMNS,
    Operation,
    bounded_int,
    choice,
    optional_bool,
    optional_module,
    optional_string,
    output_text,
    require_text,
    string_list,
    text_properties,
    typed,
)


def codec(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    operation = arguments["operation"]
    action = choice(arguments, "action", {"encode", "decode"}, "encode")
    try:
        if operation == "base64":
            if action == "encode":
                value = base64.b64encode(text.encode()).decode()
            else:
                value = base64.b64decode(text, validate=True).decode("utf-8")
        elif operation == "url":
            value = urllib.parse.quote(text, safe=optional_string(arguments, "safe", "", 100)) if action == "encode" else urllib.parse.unquote(text)
        elif operation == "html":
            value = html.escape(text, quote=True) if action == "encode" else html.unescape(text)
        elif operation == "unicode":
            value = text.encode("unicode_escape").decode("ascii") if action == "encode" else text.encode("ascii").decode("unicode_escape")
        elif operation == "hex":
            value = text.encode().hex() if action == "encode" else bytes.fromhex(text).decode("utf-8")
        else:
            if action == "encode":
                value = " ".join(f"{byte:08b}" for byte in text.encode())
            else:
                compact = "".join(text.split())
                if len(compact) % 8 or set(compact) - {"0", "1"}:
                    raise ValueError("binary input must contain complete 8-bit groups")
                value = bytes(int(compact[index:index + 8], 2) for index in range(0, len(compact), 8)).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise ValueError(f"invalid {operation} input: {error}") from error
    return output_text(value)


def jwt_decode(arguments: dict[str, Any]) -> dict[str, Any]:
    token = require_text(arguments)
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have exactly three dot-separated segments")

    def decode_part(value: str) -> Any:
        padding = "=" * (-len(value) % 4)
        return json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))

    try:
        header, payload = decode_part(parts[0]), decode_part(parts[1])
    except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JWT: {error}") from error
    return {"header": header, "payload": payload, "signatureVerified": False, "warning": "Signature is not verified."}


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}") from error


def json_format(arguments: dict[str, Any]) -> dict[str, Any]:
    value = parse_json(require_text(arguments))
    indent = bounded_int(arguments, "indent", 2, 0, 8)
    if optional_bool(arguments, "compact", False):
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=optional_bool(arguments, "sortKeys", False))
    else:
        rendered = json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=optional_bool(arguments, "sortKeys", False))
    return {"text": rendered, "value": value}


def parse_csv(arguments: dict[str, Any]) -> tuple[list[str], list[dict[str, str]]]:
    text = require_text(arguments)
    delimiter = optional_string(arguments, "delimiter", ",", 1)
    if len(delimiter) != 1:
        raise ValueError("delimiter must be one character")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV requires a header row")
    if len(reader.fieldnames) > MAX_TABLE_COLUMNS:
        raise ValueError(f"table exceeds {MAX_TABLE_COLUMNS} columns")
    rows = []
    for row in reader:
        if len(rows) >= MAX_RESULTS:
            raise ValueError(f"table exceeds {MAX_RESULTS} rows")
        if None in row:
            raise ValueError("CSV row has more fields than the header")
        rows.append({key: value or "" for key, value in row.items()})
    return reader.fieldnames, rows


def csv_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    fields, rows = parse_csv(arguments)
    return {"value": rows, "text": json.dumps(rows, ensure_ascii=False, indent=2), "columns": fields, "rows": len(rows)}


CSV_DELIMITERS = ",;\t|"


def sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=CSV_DELIMITERS).delimiter
    except csv.Error:
        pass
    # The Sniffer gives up when rows have different field counts - which is exactly the ragged input
    # normalization exists for - so fall back to the candidate that appears on the most lines.
    lines = [line for line in sample.splitlines() if line.strip()]
    counts = {candidate: sum(line.count(candidate) for line in lines) for candidate in CSV_DELIMITERS}
    best = max(CSV_DELIMITERS, key=lambda candidate: counts[candidate])
    return best if counts[best] else ","


def csv_normalize(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    delimiter = arguments.get("delimiter")
    if delimiter is None:
        delimiter = sniff_delimiter(text[:8192])
    elif not isinstance(delimiter, str) or len(delimiter) != 1:
        raise ValueError("delimiter must be one character")

    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if len(rows) > MAX_RESULTS:
        raise ValueError(f"table exceeds {MAX_RESULTS} rows")
    width = max((len(row) for row in rows), default=0)
    if width > MAX_TABLE_COLUMNS:
        raise ValueError(f"table exceeds {MAX_TABLE_COLUMNS} columns")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    # Short rows are padded so every record has the same width as the widest one.
    writer.writerows(row + [""] * (width - len(row)) for row in rows)
    return {"text": output.getvalue(), "inputDelimiter": delimiter, "rows": len(rows), "columns": width}


MAX_CSV_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_CSV_ARTIFACT_ROWS = 1_000_000


def _artifact_csv_settings(arguments: dict[str, Any]) -> tuple[str, str | None, int, int, int]:
    artifact_id = optional_string(arguments, "artifactId", "", 32)
    delimiter = arguments.get("delimiter")
    if delimiter is not None and (not isinstance(delimiter, str) or len(delimiter) != 1):
        raise ValueError("delimiter must be one character")
    max_rows = bounded_int(arguments, "maxRows", 100_000, 1, MAX_CSV_ARTIFACT_ROWS)
    output_budget = bounded_int(arguments, "outputBudgetBytes", MAX_CSV_ARTIFACT_BYTES, 1, MAX_CSV_ARTIFACT_BYTES)
    retention_days = bounded_int(arguments, "retentionDays", 7, 1, 90)
    return artifact_id, delimiter, max_rows, output_budget, retention_days


def csv_normalize_artifact(arguments: dict[str, Any]) -> dict[str, Any]:
    artifact_id, delimiter, max_rows, output_budget, retention_days = _artifact_csv_settings(arguments)
    store_root = artifact_io.root()
    source, _metadata = artifact_io.verified_source(store_root, artifact_id, MAX_CSV_ARTIFACT_BYTES)
    with open(source, "r", encoding="utf-8", errors="strict", newline="") as handle:
        sample = handle.read(8192)
        delimiter = delimiter or sniff_delimiter(sample)
        handle.seek(0)
        reader = csv.reader(handle, delimiter=delimiter)
        rows = 0
        width = 0
        for row in reader:
            rows += 1
            if rows > max_rows:
                raise ValueError(f"CSV exceeds maxRows={max_rows}")
            width = max(width, len(row))
            if width > MAX_TABLE_COLUMNS:
                raise ValueError(f"table exceeds {MAX_TABLE_COLUMNS} columns")
    with tempfile.TemporaryDirectory() as directory:
        output_path = os.path.join(directory, "normalized.csv")
        with open(source, "r", encoding="utf-8", errors="strict", newline="") as reader_handle, open(output_path, "w", encoding="utf-8", newline="") as writer_handle:
            reader = csv.reader(reader_handle, delimiter=delimiter)
            writer = csv.writer(writer_handle, lineterminator="\n")
            for row in reader:
                writer.writerow(row + [""] * (width - len(row)))
                if writer_handle.tell() > output_budget:
                    raise ValueError("normalized CSV exceeds outputBudgetBytes")
        artifact = artifact_io.publish_file(store_root, output_path, "normalized.csv", "text/csv", output_budget, retention_days)
    return {"artifact": artifact, "sourceArtifactId": artifact_id, "inputDelimiter": delimiter, "rows": rows, "columns": width}


def csv_to_json_artifact(arguments: dict[str, Any]) -> dict[str, Any]:
    artifact_id, delimiter, max_rows, output_budget, retention_days = _artifact_csv_settings(arguments)
    delimiter = delimiter or ","
    store_root = artifact_io.root()
    source, _metadata = artifact_io.verified_source(store_root, artifact_id, MAX_CSV_ARTIFACT_BYTES)
    with tempfile.TemporaryDirectory() as directory:
        output_path = os.path.join(directory, "converted.json")
        rows = 0
        with open(source, "r", encoding="utf-8", errors="strict", newline="") as reader_handle, open(output_path, "w", encoding="utf-8", newline="") as writer_handle:
            reader = csv.DictReader(reader_handle, delimiter=delimiter)
            if not reader.fieldnames:
                raise ValueError("CSV requires a header row")
            if len(reader.fieldnames) > MAX_TABLE_COLUMNS:
                raise ValueError(f"table exceeds {MAX_TABLE_COLUMNS} columns")
            writer_handle.write("[")
            for row in reader:
                rows += 1
                if rows > max_rows:
                    raise ValueError(f"CSV exceeds maxRows={max_rows}")
                if None in row:
                    raise ValueError("CSV row has more fields than the header")
                if rows > 1:
                    writer_handle.write(",")
                json.dump({key: value or "" for key, value in row.items()}, writer_handle, ensure_ascii=False, separators=(",", ":"))
                if writer_handle.tell() > output_budget:
                    raise ValueError("converted JSON exceeds outputBudgetBytes")
            writer_handle.write("]\n")
        artifact = artifact_io.publish_file(store_root, output_path, "converted.json", "application/json", output_budget, retention_days)
    return {"artifact": artifact, "sourceArtifactId": artifact_id, "rows": rows, "columns": reader.fieldnames}


KEY_VALUE_FIELD = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_.-]*)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)")


def kv_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    limit = bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS)
    records = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = {match.group(1): match.group(2).strip("\"'") for match in KEY_VALUE_FIELD.finditer(line)}
        records.append({"line": line_number, "fields": fields, "raw": line})
        if len(records) >= limit:
            break
    return {"value": records, "text": json.dumps(records, ensure_ascii=False, indent=2), "count": len(records), "truncated": len(records) >= limit}


class HtmlContent(HTMLParser):
    """Conservative visible-text extraction; nothing is rendered, fetched, or executed."""

    HIDDEN_TAGS = {"script", "style", "noscript", "template"}
    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.heading: str | None = None
        self.text: list[str] = []
        self.headings: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.current_link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.HIDDEN_TAGS:
            self.hidden += 1
        if tag in self.HEADING_TAGS:
            self.heading = tag
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.current_link = {"href": href, "text": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN_TAGS and self.hidden:
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


def html_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    source = require_text(arguments)
    limit = bounded_int(arguments, "maxResults", MAX_RESULTS, 1, MAX_RESULTS)
    parser = HtmlContent()
    parser.feed(source)
    parser.close()
    value = {
        "text": html.unescape("\n".join(parser.text)),
        "headings": parser.headings[:limit],
        "links": parser.links[:limit],
    }
    return {**value, "value": value, "truncated": len(parser.headings) > limit or len(parser.links) > limit}


def object_rows(value: Any) -> tuple[list[str], list[dict[str, Any]]]:
    if not isinstance(value, list) or len(value) > MAX_RESULTS or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"JSON must be an array of at most {MAX_RESULTS} objects")
    fields = list(dict.fromkeys(key for row in value for key in row))
    if len(fields) > MAX_TABLE_COLUMNS or not all(isinstance(field, str) for field in fields):
        raise ValueError(f"table must have at most {MAX_TABLE_COLUMNS} string column names")
    for row in value:
        if any(isinstance(cell, (dict, list)) for cell in row.values()):
            raise ValueError("table cells must be scalar values")
    return fields, value


def json_to_csv(arguments: dict[str, Any]) -> dict[str, Any]:
    fields, rows = object_rows(parse_json(require_text(arguments)))
    delimiter = optional_string(arguments, "delimiter", ",", 1)
    if len(delimiter) != 1:
        raise ValueError("delimiter must be one character")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, delimiter=delimiter, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return {"text": output.getvalue(), "columns": fields, "rows": len(rows)}


def toml_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        value = tomllib.loads(require_text(arguments))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"invalid TOML: {error}") from error
    return {"value": value, "text": json.dumps(value, ensure_ascii=False, indent=2)}


def ini_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(require_text(arguments))
    except configparser.Error as error:
        raise ValueError(f"invalid INI: {error}") from error
    value = {section: dict(parser[section]) for section in parser.sections()}
    if parser.defaults():
        value = {"DEFAULT": dict(parser.defaults()), **value}
    return {"value": value, "text": json.dumps(value, ensure_ascii=False, indent=2)}


def xml_node(element: element_tree.Element, depth: int = 0) -> dict[str, Any]:
    if depth > 64:
        raise ValueError("XML exceeds 64 levels")
    return {
        "tag": element.tag,
        "attributes": element.attrib,
        "text": (element.text or "").strip(),
        "children": [xml_node(child, depth + 1) for child in element],
    }


def xml_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("DTD and entity declarations are not allowed")
    try:
        value = xml_node(element_tree.fromstring(text))
    except element_tree.ParseError as error:
        raise ValueError(f"invalid XML: {error}") from error
    return {"value": value, "text": json.dumps(value, ensure_ascii=False, indent=2)}


def query_convert(arguments: dict[str, Any]) -> dict[str, Any]:
    operation = arguments["operation"]
    if operation == "query_to_json":
        pairs = urllib.parse.parse_qsl(require_text(arguments), keep_blank_values=True, strict_parsing=optional_bool(arguments, "strict", False), max_num_fields=MAX_RESULTS)
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                value[key] = value[key] + [item] if isinstance(value[key], list) else [value[key], item]
            else:
                value[key] = item
        return {"value": value, "text": json.dumps(value, ensure_ascii=False, indent=2)}
    value = parse_json(require_text(arguments))
    if not isinstance(value, dict) or len(value) > MAX_RESULTS:
        raise ValueError(f"JSON must be an object with at most {MAX_RESULTS} keys")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, (str, int, float, bool, list, type(None))):
            raise ValueError("query values must be scalar or arrays of scalars")
        if isinstance(item, list) and (len(item) > MAX_RESULTS or any(isinstance(entry, (dict, list)) for entry in item)):
            raise ValueError("query arrays must contain at most 1000 scalar values")
    return output_text(urllib.parse.urlencode(value, doseq=True))


def jsonl_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    records = []
    for number, line in enumerate(require_text(arguments).splitlines(), 1):
        if not line.strip():
            continue
        if len(records) >= MAX_RESULTS:
            raise ValueError(f"JSONL exceeds {MAX_RESULTS} records")
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on line {number}: {error.msg}") from error
    return {"value": records, "text": json.dumps(records, ensure_ascii=False, indent=2), "records": len(records)}


def json_to_jsonl(arguments: dict[str, Any]) -> dict[str, Any]:
    value = parse_json(require_text(arguments))
    if not isinstance(value, list):
        raise ValueError("JSONL output requires a JSON array")
    if len(value) > MAX_RESULTS:
        raise ValueError(f"array exceeds {MAX_RESULTS} records")
    lines = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in value]
    return {"text": "\n".join(lines), "records": len(lines)}


def flatten_value(value: Any, separator: str, prefix: str, into: dict[str, Any]) -> None:
    if isinstance(value, dict) and value:
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("object keys must be strings")
            flatten_value(item, separator, f"{prefix}{separator}{key}" if prefix else key, into)
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            flatten_value(item, separator, f"{prefix}[{index}]", into)
    else:
        into[prefix] = value
    if len(into) > MAX_RESULTS:
        raise ValueError(f"flattened object exceeds {MAX_RESULTS} keys")


def json_flatten(arguments: dict[str, Any]) -> dict[str, Any]:
    separator = optional_string(arguments, "separator", ".", 4)
    if not separator:
        raise ValueError("separator must be non-empty")
    flattened: dict[str, Any] = {}
    flatten_value(parse_json(require_text(arguments)), separator, "", flattened)
    return {"value": flattened, "text": json.dumps(flattened, ensure_ascii=False, indent=2), "keys": len(flattened)}


FLAT_INDEX = re.compile(r"\[(\d+)\]")


def flat_key_tokens(key: str, separator: str) -> list[Any]:
    tokens: list[Any] = []
    for segment in key.split(separator):
        base = segment.split("[", 1)[0]
        if base:
            tokens.append(base)
        tokens.extend(int(index) for index in FLAT_INDEX.findall(segment))
    if not tokens:
        raise ValueError(f"key {key!r} has no path segments")
    return tokens


def json_unflatten(arguments: dict[str, Any]) -> dict[str, Any]:
    separator = optional_string(arguments, "separator", ".", 4)
    flattened = parse_json(require_text(arguments))
    if not isinstance(flattened, dict):
        raise ValueError("unflattening requires a JSON object of flat keys")

    paths = [(flat_key_tokens(key, separator), value) for key, value in flattened.items() if isinstance(key, str)]
    if len(paths) != len(flattened):
        raise ValueError("object keys must be strings")
    root: Any = [] if paths and isinstance(paths[0][0][0], int) else {}
    for tokens, value in paths:
        cursor = root
        for position, token in enumerate(tokens[:-1]):
            default: Any = [] if isinstance(tokens[position + 1], int) else {}
            if isinstance(token, int):
                if not isinstance(cursor, list):
                    raise ValueError("conflicting object and array shapes in flattened keys")
                while len(cursor) <= token:
                    cursor.append(None)
                if cursor[token] is None:
                    cursor[token] = default
                cursor = cursor[token]
            else:
                if not isinstance(cursor, dict):
                    raise ValueError("conflicting object and array shapes in flattened keys")
                if cursor.get(token) is None:
                    cursor[token] = default
                cursor = cursor[token]
        last = tokens[-1]
        if isinstance(last, int):
            if not isinstance(cursor, list):
                raise ValueError("conflicting object and array shapes in flattened keys")
            while len(cursor) <= last:
                cursor.append(None)
            cursor[last] = value
        else:
            if not isinstance(cursor, dict):
                raise ValueError("conflicting object and array shapes in flattened keys")
            cursor[last] = value
    return {"value": root, "text": json.dumps(root, ensure_ascii=False, indent=2)}


def diff_json(left: Any, right: Any, path: str, changes: list[dict[str, Any]]) -> None:
    if len(changes) > MAX_RESULTS:
        raise ValueError(f"difference exceeds {MAX_RESULTS} entries")
    if isinstance(left, dict) and isinstance(right, dict):
        for key in dict.fromkeys([*left, *right]):
            child = f"{path}.{key}" if path else str(key)
            if key not in right:
                changes.append({"path": child, "change": "removed", "before": left[key]})
            elif key not in left:
                changes.append({"path": child, "change": "added", "after": right[key]})
            else:
                diff_json(left[key], right[key], child, changes)
    elif isinstance(left, list) and isinstance(right, list):
        for index in range(max(len(left), len(right))):
            child = f"{path}[{index}]"
            if index >= len(right):
                changes.append({"path": child, "change": "removed", "before": left[index]})
            elif index >= len(left):
                changes.append({"path": child, "change": "added", "after": right[index]})
            else:
                diff_json(left[index], right[index], child, changes)
    elif left != right or type(left) is not type(right):
        changes.append({"path": path or "$", "change": "changed", "before": left, "after": right})


def json_diff(arguments: dict[str, Any]) -> dict[str, Any]:
    left = parse_json(require_text(arguments))
    right = parse_json(require_text(arguments, "otherText"))
    changes: list[dict[str, Any]] = []
    diff_json(left, right, "", changes)
    counts = Counter(change["change"] for change in changes)
    return {"items": changes, "count": len(changes), "added": counts["added"], "removed": counts["removed"], "changed": counts["changed"], "identical": not changes}


def merge_values(left: Any, right: Any, arrays: str, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("merge exceeds 64 levels")
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merge_values(left[key], value, arrays, depth + 1) if key in left else value
        return merged
    if isinstance(left, list) and isinstance(right, list) and arrays == "concat":
        if len(left) + len(right) > MAX_RESULTS:
            raise ValueError(f"concatenated array exceeds {MAX_RESULTS} items")
        return [*left, *right]
    return right


def json_merge(arguments: dict[str, Any]) -> dict[str, Any]:
    left = parse_json(require_text(arguments))
    right = parse_json(require_text(arguments, "otherText"))
    value = merge_values(left, right, choice(arguments, "arrays", {"replace", "concat"}, "replace"))
    return {"value": value, "text": json.dumps(value, ensure_ascii=False, indent=2)}


ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def env_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, str] = {}
    for number, line in enumerate(require_text(arguments).splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if not match:
            if optional_bool(arguments, "strict", False):
                raise ValueError(f"line {number} is not a KEY=VALUE assignment")
            continue
        raw = match.group(2)
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
            if line.strip()[-1] == '"':
                raw = raw.encode().decode("unicode_escape")
        else:
            raw = raw.split(" #", 1)[0].rstrip()
        value[match.group(1)] = raw
        if len(value) > MAX_RESULTS:
            raise ValueError(f"environment exceeds {MAX_RESULTS} entries")
    return {"value": value, "text": json.dumps(value, ensure_ascii=False, indent=2), "keys": len(value)}


JSON_TYPES = {type(None): "null", bool: "boolean", int: "integer", float: "number", str: "string", list: "array", dict: "object"}


def infer_schema(value: Any, depth: int = 0) -> dict[str, Any]:
    if depth > 32:
        raise ValueError("schema inference exceeds 32 levels")
    kind = JSON_TYPES[type(value)]
    if kind == "object":
        properties = {key: infer_schema(item, depth + 1) for key, item in value.items()}
        if len(properties) > MAX_TABLE_COLUMNS:
            raise ValueError(f"object exceeds {MAX_TABLE_COLUMNS} properties")
        return {"type": "object", "properties": properties, "required": sorted(value), "additionalProperties": False}
    if kind == "array":
        if not value:
            return {"type": "array"}
        merged = infer_schema(value[0], depth + 1)
        for item in value[1:]:
            merged = merge_schemas(merged, infer_schema(item, depth + 1))
        return {"type": "array", "items": merged}
    return {"type": kind}


def merge_schemas(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    if left == right:
        return left
    if left.get("type") == right.get("type") == "object":
        keys = dict.fromkeys([*left.get("properties", {}), *right.get("properties", {})])
        properties = {}
        for key in keys:
            in_left = left.get("properties", {}).get(key)
            in_right = right.get("properties", {}).get(key)
            properties[key] = merge_schemas(in_left, in_right) if in_left and in_right else (in_left or in_right)
        required = sorted(set(left.get("required", [])) & set(right.get("required", [])))
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    if left.get("type") == right.get("type") == "array":
        if "items" in left and "items" in right:
            return {"type": "array", "items": merge_schemas(left["items"], right["items"])}
        return {"type": "array", "items": left.get("items") or right.get("items", {})}
    types = sorted({*(left["type"] if isinstance(left.get("type"), list) else [left.get("type")]), *(right["type"] if isinstance(right.get("type"), list) else [right.get("type")])} - {None})
    return {"type": types[0] if len(types) == 1 else types}


def json_schema_infer(arguments: dict[str, Any]) -> dict[str, Any]:
    value = parse_json(require_text(arguments))
    samples = value if isinstance(value, list) and optional_bool(arguments, "samplesAreArray", False) else [value]
    if not samples:
        raise ValueError("at least one sample is required")
    schema = infer_schema(samples[0])
    for sample in samples[1:]:
        schema = merge_schemas(schema, infer_schema(sample))
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
    return {"value": schema, "text": json.dumps(schema, ensure_ascii=False, indent=2)}


def yaml_to_json(arguments: dict[str, Any]) -> dict[str, Any]:
    yaml = optional_module("yaml", "python3-yaml", "YAML parsing")
    text = require_text(arguments)
    try:
        documents = list(yaml.safe_load_all(text)) if optional_bool(arguments, "allDocuments", False) else [yaml.safe_load(text)]
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML: {error}") from error
    value = documents if optional_bool(arguments, "allDocuments", False) else documents[0]
    return {"value": value, "text": json.dumps(value, ensure_ascii=False, indent=2, default=str), "documents": len(documents)}


def json_to_yaml(arguments: dict[str, Any]) -> dict[str, Any]:
    yaml = optional_module("yaml", "python3-yaml", "YAML rendering")
    value = parse_json(require_text(arguments))
    rendered = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=optional_bool(arguments, "sortKeys", False),
        indent=bounded_int(arguments, "indent", 2, 2, 8),
    )
    return {"text": rendered, "value": value}


def json_to_ini(arguments: dict[str, Any]) -> dict[str, Any]:
    value = parse_json(require_text(arguments))
    if not isinstance(value, dict) or not all(isinstance(section, dict) for section in value.values()):
        raise ValueError("INI output requires an object whose values are objects of scalars")
    parser = configparser.ConfigParser(interpolation=None)
    for section, entries in value.items():
        if any(isinstance(item, (dict, list)) for item in entries.values()):
            raise ValueError("INI values must be scalar")
        parser[section] = {key: "" if item is None else str(item).lower() if isinstance(item, bool) else str(item) for key, item in entries.items()}
    output = io.StringIO()
    parser.write(output)
    return {"text": output.getvalue(), "sections": len(value)}


def json_to_toml(arguments: dict[str, Any]) -> dict[str, Any]:
    tomli_w = optional_module("tomli_w", "python3-tomli-w", "TOML rendering")
    value = parse_json(require_text(arguments))
    if not isinstance(value, dict):
        raise ValueError("TOML output requires a JSON object")
    try:
        rendered = tomli_w.dumps(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"value cannot be represented as TOML: {error}") from error
    return {"text": rendered, "value": value}


SECRET_PATTERNS: list[tuple[str, str]] = [
    ("private-key-block", r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    ("aws-access-key-id", r"(?<![A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}(?![A-Z0-9])"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    ("slack-token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("json-web-token", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    ("bearer-token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ("basic-auth-url", r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]+@"),
    ("credential-assignment", r"(?i)\b(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\b\s*[:=]\s*[\"']?([^\s\"',;]{8,})"),
]


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


def secret_scan(arguments: dict[str, Any]) -> dict[str, Any]:
    """Report likely credentials by location and masked preview; the secret itself is never echoed."""
    text = require_text(arguments)
    limit = bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS)
    minimum_entropy = arguments.get("minEntropy", 4.0)
    if not isinstance(minimum_entropy, (int, float)) or isinstance(minimum_entropy, bool) or not 0 <= minimum_entropy <= 8:
        raise ValueError("minEntropy must be a number from 0 to 8")
    starts = [match.start() for match in re.finditer(r"^", text, re.MULTILINE)]

    def locate(offset: int) -> dict[str, int]:
        line = max(index for index, start in enumerate(starts) if start <= offset)
        return {"line": line + 1, "column": offset - starts[line] + 1}

    findings: list[dict[str, Any]] = []
    covered: list[tuple[int, int]] = []
    for kind, pattern in SECRET_PATTERNS:
        for match in re.finditer(pattern, text):
            captured = match.group(match.lastindex) if match.lastindex else match.group(0)
            findings.append({"type": kind, **locate(match.start()), "length": len(captured), "preview": mask_secret(captured)})
            covered.append((match.start(), match.end()))

    if optional_bool(arguments, "includeHighEntropy", True):
        for match in re.finditer(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{20,}(?![A-Za-z0-9+/=_-])", text):
            candidate = match.group(0)
            entropy = shannon_entropy(candidate)
            if entropy < minimum_entropy or any(start <= match.start() < end for start, end in covered):
                continue
            findings.append({"type": "high-entropy-string", **locate(match.start()), "length": len(candidate), "preview": mask_secret(candidate), "entropy": round(entropy, 3)})

    findings.sort(key=lambda item: (item["line"], item["column"]))
    return {"items": findings[:limit], "count": min(len(findings), limit), "truncated": len(findings) > limit, "clean": not findings}


REDACTION_PATTERNS: dict[str, str] = {
    "emails": r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])",
    "ip_addresses": r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])",
    "urls": r"https?://[^\s<>\"']+",
    "card_numbers": r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])",
}

REDACTION_LABELS: dict[str, str] = {
    "emails": "EMAIL",
    "ip_addresses": "IP",
    "urls": "URL",
    "card_numbers": "CARD",
    "secrets": "SECRET",
}


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, character in enumerate(reversed(digits)):
        value = int(character)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact(arguments: dict[str, Any]) -> dict[str, Any]:
    """Replace sensitive spans with stable numbered placeholders so the result stays diffable."""
    text = require_text(arguments)
    available = [*REDACTION_PATTERNS, "secrets"]
    categories = string_list(arguments, "categories", 1, len(available), 32) if "categories" in arguments else available
    unknown = sorted(set(categories) - set(available))
    if unknown:
        raise ValueError(f"unsupported redaction categories: {', '.join(unknown)}; available: {', '.join(available)}")

    spans: list[tuple[int, int, str]] = []
    for category in categories:
        if category == "secrets":
            for _kind, pattern in SECRET_PATTERNS:
                spans.extend((match.start(), match.end(), REDACTION_LABELS["secrets"]) for match in re.finditer(pattern, text))
            continue
        for match in re.finditer(REDACTION_PATTERNS[category], text):
            if category == "card_numbers":
                digits = re.sub(r"\D", "", match.group(0))
                if not 13 <= len(digits) <= 19 or not luhn_valid(digits):
                    continue
            spans.append((match.start(), match.end(), REDACTION_LABELS[category]))

    spans.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, label in spans:
        if merged and start < merged[-1][1]:
            continue
        merged.append((start, end, label))

    assigned: dict[str, str] = {}
    counts: Counter[str] = Counter()
    pieces = []
    cursor = 0
    for start, end, label in merged:
        original = text[start:end]
        if original not in assigned:
            counts[label] += 1
            assigned[original] = f"[{label}_{counts[label]}]"
        pieces.append(text[cursor:start])
        pieces.append(assigned[original])
        cursor = end
    pieces.append(text[cursor:])
    return {"text": "".join(pieces), "redactions": len(merged), "distinctValues": len(assigned), "byCategory": dict(counts)}


SEQUENTIAL = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiop", "asdfghjkl", "zxcvbnm")


def password_strength(arguments: dict[str, Any]) -> dict[str, Any]:
    password = require_text(arguments)
    if len(password) > 4_096:
        raise ValueError("text exceeds 4096 characters")
    classes = {
        "lowercase": (26, any(character.islower() for character in password)),
        "uppercase": (26, any(character.isupper() for character in password)),
        "digits": (10, any(character.isdigit() for character in password)),
        "symbols": (33, any(not character.isalnum() and not character.isspace() for character in password)),
        "whitespace": (1, any(character.isspace() for character in password)),
        "other": (100, any(ord(character) > 127 for character in password)),
    }
    pool = sum(size for size, present in classes.values() if present)
    entropy = len(password) * math.log2(pool) if pool else 0.0
    warnings = []
    lowered = password.casefold()
    if len(set(password)) <= max(1, len(password) // 3):
        warnings.append("few distinct characters")
    if re.search(r"(.)\1{2,}", password):
        warnings.append("contains a run of three or more identical characters")
    if any(sequence[index:index + 4] in lowered for sequence in SEQUENTIAL for index in range(len(sequence) - 3)):
        warnings.append("contains a keyboard or alphabet sequence")
    if re.fullmatch(r"[A-Za-z]+\d{0,4}[!?.]?", password):
        warnings.append("matches the common word-then-digits shape")
    verdict = "very weak" if entropy < 28 else "weak" if entropy < 36 else "reasonable" if entropy < 60 else "strong" if entropy < 128 else "very strong"
    return {
        "length": len(password),
        "characterClasses": sorted(name for name, (_size, present) in classes.items() if present),
        "poolSize": pool,
        "entropyBits": round(entropy, 2),
        "verdict": verdict,
        "warnings": warnings,
        "note": "Entropy assumes independent random characters; it overstates human-chosen passwords.",
    }


WORDLIST_PATHS = ("/usr/share/dict/words", "/usr/share/dict/american-english", "/usr/share/dict/british-english")


def load_wordlist(minimum: int, maximum: int) -> list[str]:
    for path in WORDLIST_PATHS:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as handle:
            words = [word.strip() for word in handle]
        selected = [word for word in words if word.isascii() and word.isalpha() and word.islower() and minimum <= len(word) <= maximum]
        if len(selected) >= 1_000:
            return selected
    raise ValueError("passphrase generation requires the wamerican package, which is not installed on this node")


def security_operation(arguments: dict[str, Any]) -> dict[str, Any]:
    operation = arguments["operation"]
    text = require_text(arguments)
    if operation == "uuid_validate":
        try:
            parsed = uuid_module.UUID(text.strip())
            return {"valid": True, "canonical": str(parsed), "version": parsed.version, "variant": parsed.variant}
        except ValueError:
            return {"valid": False}
    algorithm = choice(arguments, "algorithm", {"md5", "sha1", "sha256", "sha512", "blake2b", "crc32" if operation == "checksum" else "sha256"})
    if operation == "hmac":
        key = require_text(arguments, "key")
        if algorithm not in {"md5", "sha1", "sha256", "sha512"}:
            raise ValueError("HMAC algorithm must be md5, sha1, sha256, or sha512")
        value = hmac_module.new(key.encode(), text.encode(), algorithm).hexdigest()
    elif algorithm == "crc32":
        value = f"{zlib.crc32(text.encode()) & 0xffffffff:08x}"
    else:
        value = hashlib.new(algorithm, text.encode()).hexdigest()
    result = {"algorithm": algorithm, "hex": value}
    if algorithm in {"md5", "sha1"}:
        result["warning"] = f"{algorithm.upper()} is not suitable for security-sensitive uses."
    return result


def generate(arguments: dict[str, Any]) -> dict[str, Any]:
    operation = arguments["operation"]
    if operation == "uuid":
        version = bounded_int(arguments, "version", 4, 4, 4)
        count = bounded_int(arguments, "count", 1, 1, 100)
        values = [str(uuid_module.uuid4()) for _ in range(count)]
        return {"items": values, "text": "\n".join(values), "version": version}
    if operation == "password":
        length = bounded_int(arguments, "length", 24, 8, 256)
        alphabet = string.ascii_letters + string.digits
        if optional_bool(arguments, "symbols", True):
            alphabet += "!#$%&()*+,-./:;<=>?@[]^_{|}~"
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        return {"text": value, "length": length}
    if operation == "passphrase":
        words = bounded_int(arguments, "words", 5, 3, 20)
        separator = optional_string(arguments, "separator", "-", 4)
        vocabulary = load_wordlist(bounded_int(arguments, "minWordLength", 4, 3, 8), bounded_int(arguments, "maxWordLength", 9, 4, 16))
        chosen = [secrets.choice(vocabulary) for _ in range(words)]
        if optional_bool(arguments, "capitalize", False):
            chosen = [word.capitalize() for word in chosen]
        value = separator.join(chosen)
        return {"text": value, "words": words, "vocabularySize": len(vocabulary), "entropyBits": round(words * math.log2(len(vocabulary)), 2)}
    paragraphs = bounded_int(arguments, "paragraphs", 1, 1, 20)
    sentences = bounded_int(arguments, "sentencesPerParagraph", 5, 1, 20)
    vocabulary = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna aliqua".split()
    output = []
    for _ in range(paragraphs):
        paragraph = []
        for _ in range(sentences):
            count = secrets.choice(range(6, 13))
            words = [secrets.choice(vocabulary) for _ in range(count)]
            paragraph.append(" ".join(words).capitalize() + ".")
        output.append(" ".join(paragraph))
    return output_text("\n\n".join(output))


def markdown_cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", "<br>")


MARKDOWN_RULES = {"left": ":---", "right": "---:", "center": ":---:", "default": "---"}


def table_transform(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments["operation"] == "to_markdown" and choice(arguments, "format", {"json", "csv"}, "json") == "csv":
        fields, rows = parse_csv(arguments)
    else:
        fields, rows = object_rows(parse_json(require_text(arguments)))
    if arguments["operation"] == "to_markdown":
        align = choice(arguments, "align", set(MARKDOWN_RULES), "default")
        lines = [
            "| " + " | ".join(markdown_cell(field) for field in fields) + " |",
            "| " + " | ".join(MARKDOWN_RULES[align] for _ in fields) + " |",
        ]
        lines.extend("| " + " | ".join(markdown_cell(row.get(field)) for field in fields) + " |" for row in rows)
        return {"text": "\n".join(lines), "rows": len(rows), "columns": fields}
    column = optional_string(arguments, "column", "", 1_000)
    if column not in fields:
        raise ValueError(f"unknown column: {column}")
    numeric = optional_bool(arguments, "numeric", False)
    descending = optional_bool(arguments, "descending", False)
    try:
        key = (lambda row: float(row.get(column, 0))) if numeric else (lambda row: str(row.get(column, "")).casefold())
        rows.sort(key=key, reverse=descending)
    except (TypeError, ValueError) as error:
        raise ValueError("numeric table sorting requires numeric values") from error
    return {"value": rows, "text": json.dumps(rows, ensure_ascii=False, indent=2), "rows": len(rows), "columns": fields}


CODEC_OPERATIONS = {
    name: Operation(f"{name.title()} encode or decode UTF-8 text.", text_properties(action={"enum": ["encode", "decode"]}), ("text",), codec)
    for name in ("base64", "url", "html", "unicode", "hex", "binary")
}
CODEC_OPERATIONS["url"] = Operation("URL encode or decode UTF-8 text.", text_properties(action={"enum": ["encode", "decode"]}, safe=typed("string", maxLength=100)), ("text",), codec)
CODEC_OPERATIONS["jwt_decode"] = Operation("Decode JWT header and payload without verifying its signature.", text_properties(), ("text",), jwt_decode)

DATA_OPERATIONS = {
    "json_format": Operation("Parse and format or compact JSON.", text_properties(indent=typed("integer", minimum=0, maximum=8), compact=typed("boolean"), sortKeys=typed("boolean")), ("text",), json_format),
    "csv_normalize": Operation("Normalize delimiter-separated text to RFC-style comma-separated CSV.", text_properties(delimiter=typed("string", minLength=1, maxLength=1)), ("text",), csv_normalize),
    "csv_to_json": Operation("Convert headered CSV to an array of objects.", text_properties(delimiter=typed("string", minLength=1, maxLength=1)), ("text",), csv_to_json),
    "csv_normalize_artifact": Operation("Normalize a shared CSV artifact and publish the result as a new artifact.", {"artifactId": typed("string", pattern="^[0-9a-f]{32}$"), "delimiter": typed("string", minLength=1, maxLength=1), "maxRows": typed("integer", minimum=1, maximum=MAX_CSV_ARTIFACT_ROWS), "outputBudgetBytes": typed("integer", minimum=1, maximum=MAX_CSV_ARTIFACT_BYTES), "retentionDays": typed("integer", minimum=1, maximum=90)}, ("artifactId",), csv_normalize_artifact),
    "csv_to_json_artifact": Operation("Convert a shared CSV artifact to a new streamed JSON artifact.", {"artifactId": typed("string", pattern="^[0-9a-f]{32}$"), "delimiter": typed("string", minLength=1, maxLength=1), "maxRows": typed("integer", minimum=1, maximum=MAX_CSV_ARTIFACT_ROWS), "outputBudgetBytes": typed("integer", minimum=1, maximum=MAX_CSV_ARTIFACT_BYTES), "retentionDays": typed("integer", minimum=1, maximum=90)}, ("artifactId",), csv_to_json_artifact),
    "json_to_csv": Operation("Convert an array of flat objects to CSV.", text_properties(delimiter=typed("string", minLength=1, maxLength=1)), ("text",), json_to_csv),
    "kv_to_json": Operation("Parse key=value and key:value fields from bounded log lines.", text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), kv_to_json),
    "html_to_json": Operation("Extract visible text, headings, and links from caller-provided HTML.", text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), html_to_json),
    "jsonl_to_json": Operation("Parse newline-delimited JSON into an array.", text_properties(), ("text",), jsonl_to_json),
    "json_to_jsonl": Operation("Render a JSON array as newline-delimited JSON.", text_properties(), ("text",), json_to_jsonl),
    "json_flatten": Operation("Flatten nested JSON into dotted and indexed keys.", text_properties(separator=typed("string", minLength=1, maxLength=4)), ("text",), json_flatten),
    "json_unflatten": Operation("Rebuild nested JSON from dotted and indexed keys.", text_properties(separator=typed("string", minLength=1, maxLength=4)), ("text",), json_unflatten),
    "json_diff": Operation("Report structural differences between two JSON documents.", text_properties(otherText={"type": "string"}), ("text", "otherText"), json_diff),
    "json_merge": Operation("Deep-merge two JSON documents.", text_properties(otherText={"type": "string"}, arrays={"enum": ["replace", "concat"]}), ("text", "otherText"), json_merge),
    "json_schema_infer": Operation("Infer a JSON Schema from one document or an array of samples.", text_properties(samplesAreArray=typed("boolean")), ("text",), json_schema_infer),
    "env_to_json": Operation("Parse dotenv-style KEY=VALUE assignments into JSON.", text_properties(strict=typed("boolean")), ("text",), env_to_json),
    "yaml_to_json": Operation("Parse YAML with safe_load into JSON (needs python3-yaml).", text_properties(allDocuments=typed("boolean")), ("text",), yaml_to_json),
    "json_to_yaml": Operation("Render JSON as YAML with safe_dump (needs python3-yaml).", text_properties(sortKeys=typed("boolean"), indent=typed("integer", minimum=2, maximum=8)), ("text",), json_to_yaml),
    "toml_to_json": Operation("Parse TOML into JSON.", text_properties(), ("text",), toml_to_json),
    "json_to_toml": Operation("Render a JSON object as TOML (needs python3-tomli-w).", text_properties(), ("text",), json_to_toml),
    "ini_to_json": Operation("Parse INI sections into JSON.", text_properties(), ("text",), ini_to_json),
    "json_to_ini": Operation("Render a section/key JSON object as INI.", text_properties(), ("text",), json_to_ini),
    "xml_to_json": Operation("Parse XML into an explicit node-tree JSON form.", text_properties(), ("text",), xml_to_json),
    "query_to_json": Operation("Parse a URL query string into JSON.", text_properties(strict=typed("boolean")), ("text",), query_convert),
    "json_to_query": Operation("Encode a JSON object as a URL query string.", text_properties(), ("text",), query_convert),
}

SECURITY_OPERATIONS = {
    "digest": Operation("Calculate a cryptographic digest.", text_properties(algorithm={"enum": ["md5", "sha1", "sha256", "sha512", "blake2b"]}), ("text", "algorithm"), security_operation),
    "hmac": Operation("Calculate an HMAC digest.", text_properties(key={"type": "string"}, algorithm={"enum": ["md5", "sha1", "sha256", "sha512"]}), ("text", "key", "algorithm"), security_operation),
    "checksum": Operation("Calculate CRC32 or a named digest checksum.", text_properties(algorithm={"enum": ["crc32", "md5", "sha1", "sha256", "sha512", "blake2b"]}), ("text", "algorithm"), security_operation),
    "uuid_validate": Operation("Validate and canonicalize a UUID.", text_properties(), ("text",), security_operation),
    "secret_scan": Operation("Report likely credentials with masked previews and locations.", text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS), includeHighEntropy=typed("boolean"), minEntropy=typed("number", minimum=0, maximum=8)), ("text",), secret_scan),
    "redact": Operation("Replace emails, addresses, URLs, card numbers, and secrets with stable placeholders.", text_properties(categories=typed("array", minItems=1, maxItems=5, items=typed("string"))), ("text",), redact),
    "password_strength": Operation("Estimate password entropy and report structural weaknesses.", text_properties(), ("text",), password_strength),
}

GENERATE_OPERATIONS = {
    "uuid": Operation("Generate cryptographically random UUIDv4 values.", {"version": {"const": 4}, "count": typed("integer", minimum=1, maximum=100)}, (), generate),
    "password": Operation("Generate a password with the secrets module.", {"length": typed("integer", minimum=8, maximum=256), "symbols": typed("boolean")}, (), generate),
    "lorem": Operation("Generate bounded lorem-style placeholder text.", {"paragraphs": typed("integer", minimum=1, maximum=20), "sentencesPerParagraph": typed("integer", minimum=1, maximum=20)}, (), generate),
    "passphrase": Operation("Generate a diceware-style passphrase from the system word list (needs wamerican).", {"words": typed("integer", minimum=3, maximum=20), "separator": typed("string", maxLength=4), "capitalize": typed("boolean"), "minWordLength": typed("integer", minimum=3, maximum=8), "maxWordLength": typed("integer", minimum=4, maximum=16)}, (), generate),
}

TABLE_OPERATIONS = {
    "to_markdown": Operation("Render a JSON array of flat objects, or CSV, as a Markdown table.", text_properties(format={"enum": ["json", "csv"]}, delimiter=typed("string", minLength=1, maxLength=1), align={"enum": ["default", "left", "right", "center"]}), ("text",), table_transform),
    "sort": Operation("Sort a JSON table by a named column.", text_properties(column=typed("string", minLength=1), numeric=typed("boolean"), descending=typed("boolean")), ("text", "column"), table_transform),
}

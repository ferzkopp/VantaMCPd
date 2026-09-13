#!/usr/bin/env python3
import base64
import binascii
import configparser
import csv
import hashlib
import hmac as hmac_module
import html
import io
import json
import secrets
import string
import tomllib
import urllib.parse
import uuid as uuid_module
import xml.etree.ElementTree as element_tree
import zlib
from typing import Any

from operation_common import (
    MAX_RESULTS,
    MAX_TABLE_COLUMNS,
    Operation,
    bounded_int,
    choice,
    optional_bool,
    optional_string,
    output_text,
    require_text,
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


def table_transform(arguments: dict[str, Any]) -> dict[str, Any]:
    fields, rows = object_rows(parse_json(require_text(arguments)))
    if arguments["operation"] == "to_markdown":
        lines = ["| " + " | ".join(markdown_cell(field) for field in fields) + " |", "| " + " | ".join("---" for _ in fields) + " |"]
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
    "csv_to_json": Operation("Convert headered CSV to an array of objects.", text_properties(delimiter=typed("string", minLength=1, maxLength=1)), ("text",), csv_to_json),
    "json_to_csv": Operation("Convert an array of flat objects to CSV.", text_properties(delimiter=typed("string", minLength=1, maxLength=1)), ("text",), json_to_csv),
    "toml_to_json": Operation("Parse TOML into JSON.", text_properties(), ("text",), toml_to_json),
    "ini_to_json": Operation("Parse INI sections into JSON.", text_properties(), ("text",), ini_to_json),
    "xml_to_json": Operation("Parse XML into an explicit node-tree JSON form.", text_properties(), ("text",), xml_to_json),
    "query_to_json": Operation("Parse a URL query string into JSON.", text_properties(strict=typed("boolean")), ("text",), query_convert),
    "json_to_query": Operation("Encode a JSON object as a URL query string.", text_properties(), ("text",), query_convert),
}

SECURITY_OPERATIONS = {
    "digest": Operation("Calculate a cryptographic digest.", text_properties(algorithm={"enum": ["md5", "sha1", "sha256", "sha512", "blake2b"]}), ("text", "algorithm"), security_operation),
    "hmac": Operation("Calculate an HMAC digest.", text_properties(key={"type": "string"}, algorithm={"enum": ["md5", "sha1", "sha256", "sha512"]}), ("text", "key", "algorithm"), security_operation),
    "checksum": Operation("Calculate CRC32 or a named digest checksum.", text_properties(algorithm={"enum": ["crc32", "md5", "sha1", "sha256", "sha512", "blake2b"]}), ("text", "algorithm"), security_operation),
    "uuid_validate": Operation("Validate and canonicalize a UUID.", text_properties(), ("text",), security_operation),
}

GENERATE_OPERATIONS = {
    "uuid": Operation("Generate cryptographically random UUIDv4 values.", {"version": {"const": 4}, "count": typed("integer", minimum=1, maximum=100)}, (), generate),
    "password": Operation("Generate a password with the secrets module.", {"length": typed("integer", minimum=8, maximum=256), "symbols": typed("boolean")}, (), generate),
    "lorem": Operation("Generate bounded lorem-style placeholder text.", {"paragraphs": typed("integer", minimum=1, maximum=20), "sentencesPerParagraph": typed("integer", minimum=1, maximum=20)}, (), generate),
}

TABLE_OPERATIONS = {
    "to_markdown": Operation("Render a JSON array of flat objects as a Markdown table.", text_properties(), ("text",), table_transform),
    "sort": Operation("Sort a JSON table by a named column.", text_properties(column=typed("string", minLength=1), numeric=typed("boolean"), descending=typed("boolean")), ("text", "column"), table_transform),
}

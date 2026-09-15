#!/usr/bin/env python3
from typing import Any

MAX_CONTENT_CHARACTERS = 100_000
MAX_QUERIES = 12
MAX_QUERY_RESULTS = 50
MAX_TABLES = 20
MAX_TABLE_ROWS = 500
MAX_TABLE_COLUMNS = 50
MAX_CELL_CHARACTERS = 2_000
ALLOWED_QUERY_FIELDS = {"text", "href", "src", "title", "alt", "value", "datetime", "content", "ariaLabel"}


def strict_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def reject_unknown(arguments: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"unknown argument{'s' if len(unknown) != 1 else ''}: {', '.join(unknown)}")


def bounded_integer(arguments: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def optional_selector(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 500 or "\x00" in value:
        raise ValueError(f"{name} must contain from 1 to 500 characters")
    return value


def validate_retrieve(arguments: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(arguments, {"url", "format", "contentSelector", "waitForSelector", "settleMs", "timeoutMs", "maxCharacters", "linkLimit"})
    output_format = arguments.get("format", "markdown")
    if output_format not in {"markdown", "text"}:
        raise ValueError("format must be markdown or text")
    return {
        "url": arguments.get("url"),
        "format": output_format,
        "contentSelector": optional_selector(arguments, "contentSelector"),
        "waitForSelector": optional_selector(arguments, "waitForSelector"),
        "settleMs": bounded_integer(arguments, "settleMs", 500, 0, 3_000),
        "timeoutMs": bounded_integer(arguments, "timeoutMs", 20_000, 1_000, 30_000),
        "maxCharacters": bounded_integer(arguments, "maxCharacters", 40_000, 1_000, MAX_CONTENT_CHARACTERS),
        "linkLimit": bounded_integer(arguments, "linkLimit", 30, 0, 100),
    }


def validate_query(arguments: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(arguments, {"url", "queries", "waitForSelector", "settleMs", "timeoutMs"})
    raw_queries = arguments.get("queries")
    if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= MAX_QUERIES:
        raise ValueError(f"queries must contain from 1 to {MAX_QUERIES} entries")
    queries = []
    names: set[str] = set()
    for index, raw in enumerate(raw_queries):
        query = strict_object(raw, f"queries[{index}]")
        reject_unknown(query, {"name", "selector", "fields", "limit"})
        name = query.get("name")
        if not isinstance(name, str) or not name or len(name) > 80:
            raise ValueError(f"queries[{index}].name must contain from 1 to 80 characters")
        if name in names:
            raise ValueError(f"duplicate query name: {name}")
        names.add(name)
        selector = optional_selector(query, "selector")
        fields = query.get("fields", ["text"])
        if not isinstance(fields, list) or not fields or len(fields) > len(ALLOWED_QUERY_FIELDS):
            raise ValueError(f"queries[{index}].fields must be a non-empty field list")
        if any(not isinstance(field, str) or field not in ALLOWED_QUERY_FIELDS for field in fields):
            raise ValueError(f"queries[{index}].fields contains an unsupported field")
        queries.append({
            "name": name,
            "selector": selector,
            "fields": list(dict.fromkeys(fields)),
            "limit": bounded_integer(query, "limit", 10, 1, MAX_QUERY_RESULTS),
        })
    return {
        "url": arguments.get("url"),
        "queries": queries,
        "waitForSelector": optional_selector(arguments, "waitForSelector"),
        "settleMs": bounded_integer(arguments, "settleMs", 500, 0, 3_000),
        "timeoutMs": bounded_integer(arguments, "timeoutMs", 20_000, 1_000, 30_000),
    }


def validate_tables(arguments: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(arguments, {"url", "tableSelector", "waitForSelector", "settleMs", "timeoutMs", "maxTables", "maxRows", "maxColumns", "maxCellCharacters"})
    return {
        "url": arguments.get("url"),
        "tableSelector": optional_selector(arguments, "tableSelector") or "table",
        "waitForSelector": optional_selector(arguments, "waitForSelector"),
        "settleMs": bounded_integer(arguments, "settleMs", 500, 0, 3_000),
        "timeoutMs": bounded_integer(arguments, "timeoutMs", 20_000, 1_000, 30_000),
        "maxTables": bounded_integer(arguments, "maxTables", 10, 1, MAX_TABLES),
        "maxRows": bounded_integer(arguments, "maxRows", 100, 1, MAX_TABLE_ROWS),
        "maxColumns": bounded_integer(arguments, "maxColumns", 20, 1, MAX_TABLE_COLUMNS),
        "maxCellCharacters": bounded_integer(arguments, "maxCellCharacters", 500, 10, MAX_CELL_CHARACTERS),
    }


def truncate_text(value: str, maximum: int) -> tuple[str, bool, int]:
    original = len(value)
    if original <= maximum:
        return value, False, original
    return value[:maximum], True, original


def self_test() -> None:
    assert validate_retrieve({"url": "https://example.com"})["maxCharacters"] == 40_000
    assert validate_query({"url": "https://example.com", "queries": [{"name": "title", "selector": "h1"}]})["queries"][0]["fields"] == ["text"]
    assert validate_tables({"url": "https://example.com"})["tableSelector"] == "table"
    assert truncate_text("abcd", 3) == ("abc", True, 4)
    for invalid in (
        lambda: validate_retrieve({"url": "https://example.com", "extra": True}),
        lambda: validate_query({"url": "https://example.com", "queries": []}),
        lambda: validate_query({"url": "https://example.com", "queries": [{"name": "x", "selector": "a", "fields": ["html"]}]}),
        lambda: validate_tables({"url": "https://example.com", "maxRows": 0}),
    ):
        try:
            invalid()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid extraction arguments were accepted")


if __name__ == "__main__":
    self_test()

#!/usr/bin/env python3
import re
from typing import Any

MAX_CONTENT_CHARACTERS = 100_000
MAX_DISCOVERY_CANDIDATES = 20
MAX_DISCOVERY_SAMPLES = 5
MAX_QUERIES = 12
MAX_QUERY_RESULTS = 50
MAX_TABLES = 20
MAX_TABLE_ROWS = 500
MAX_TABLE_COLUMNS = 50
MAX_CELL_CHARACTERS = 2_000
MAX_ROW_OFFSET = 50_000
MAX_TABLE_INDEX = 10_000
ALLOWED_QUERY_FIELDS = {"text", "href", "src", "title", "alt", "value", "datetime", "content", "ariaLabel"}
ALLOWED_COLOR_SCHEMES = {"light", "dark", "no-preference"}
ALLOWED_REDUCED_MOTION = {"reduce", "no-preference"}
LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
TIMEZONE_PATTERN = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")


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


def validate_browser_options(arguments: dict[str, Any]) -> dict[str, Any]:
    raw = arguments.get("browser")
    if raw is None:
        return {}
    browser = strict_object(raw, "browser")
    reject_unknown(browser, {"userAgent", "language", "timezone", "viewport", "colorScheme", "reducedMotion", "javascriptEnabled"})
    result: dict[str, Any] = {}

    user_agent = browser.get("userAgent")
    if user_agent is not None:
        if not isinstance(user_agent, str) or not 1 <= len(user_agent) <= 512 or any(ord(character) < 32 or ord(character) == 127 for character in user_agent):
            raise ValueError("browser.userAgent must contain from 1 to 512 printable characters")
        result["userAgent"] = user_agent

    for name, pattern in (("language", LANGUAGE_PATTERN), ("timezone", TIMEZONE_PATTERN)):
        value = browser.get(name)
        if value is not None:
            if not isinstance(value, str) or not 1 <= len(value) <= 100 or pattern.fullmatch(value) is None:
                raise ValueError(f"browser.{name} is not valid")
            result[name] = value

    viewport = browser.get("viewport")
    if viewport is not None:
        viewport = strict_object(viewport, "browser.viewport")
        reject_unknown(viewport, {"width", "height", "deviceScaleFactor", "mobile"})
        if "width" not in viewport or "height" not in viewport:
            raise ValueError("browser.viewport requires width and height")
        device_scale_factor = viewport.get("deviceScaleFactor", 1)
        if isinstance(device_scale_factor, bool) or not isinstance(device_scale_factor, (int, float)) or not 0.5 <= device_scale_factor <= 4:
            raise ValueError("browser.viewport.deviceScaleFactor must be a number from 0.5 to 4")
        mobile = viewport.get("mobile", False)
        if not isinstance(mobile, bool):
            raise ValueError("browser.viewport.mobile must be a boolean")
        result["viewport"] = {
            "width": bounded_integer(viewport, "width", 1280, 320, 3840),
            "height": bounded_integer(viewport, "height", 720, 200, 2160),
            "deviceScaleFactor": device_scale_factor,
            "mobile": mobile,
        }

    for name, allowed in (("colorScheme", ALLOWED_COLOR_SCHEMES), ("reducedMotion", ALLOWED_REDUCED_MOTION)):
        value = browser.get(name)
        if value is not None:
            if value not in allowed:
                raise ValueError(f"browser.{name} must be one of: {', '.join(sorted(allowed))}")
            result[name] = value

    javascript_enabled = browser.get("javascriptEnabled")
    if javascript_enabled is not None:
        if not isinstance(javascript_enabled, bool):
            raise ValueError("browser.javascriptEnabled must be a boolean")
        result["javascriptEnabled"] = javascript_enabled
    return result


def validate_retrieve(arguments: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(arguments, {"url", "browser", "format", "contentSelector", "waitForSelector", "settleMs", "timeoutMs", "maxCharacters", "linkLimit"})
    output_format = arguments.get("format", "markdown")
    if output_format not in {"markdown", "text"}:
        raise ValueError("format must be markdown or text")
    return {
        "url": arguments.get("url"),
        "browser": validate_browser_options(arguments),
        "format": output_format,
        "contentSelector": optional_selector(arguments, "contentSelector"),
        "waitForSelector": optional_selector(arguments, "waitForSelector"),
        "settleMs": bounded_integer(arguments, "settleMs", 500, 0, 3_000),
        "timeoutMs": bounded_integer(arguments, "timeoutMs", 20_000, 1_000, 30_000),
        "maxCharacters": bounded_integer(arguments, "maxCharacters", 40_000, 1_000, MAX_CONTENT_CHARACTERS),
        "linkLimit": bounded_integer(arguments, "linkLimit", 30, 0, 100),
    }


def validate_discover_links(arguments: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(arguments, {"url", "browser", "waitForSelector", "settleMs", "timeoutMs", "maxCandidates", "sampleLimit"})
    return {
        "url": arguments.get("url"),
        "browser": validate_browser_options(arguments),
        "waitForSelector": optional_selector(arguments, "waitForSelector"),
        "settleMs": bounded_integer(arguments, "settleMs", 500, 0, 3_000),
        "timeoutMs": bounded_integer(arguments, "timeoutMs", 20_000, 1_000, 30_000),
        "maxCandidates": bounded_integer(arguments, "maxCandidates", 10, 1, MAX_DISCOVERY_CANDIDATES),
        "sampleLimit": bounded_integer(arguments, "sampleLimit", 3, 1, MAX_DISCOVERY_SAMPLES),
    }


def validate_query(arguments: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(arguments, {"url", "browser", "queries", "waitForSelector", "settleMs", "timeoutMs"})
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
        "browser": validate_browser_options(arguments),
        "queries": queries,
        "waitForSelector": optional_selector(arguments, "waitForSelector"),
        "settleMs": bounded_integer(arguments, "settleMs", 500, 0, 3_000),
        "timeoutMs": bounded_integer(arguments, "timeoutMs", 20_000, 1_000, 30_000),
    }


def validate_tables(arguments: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(arguments, {"url", "browser", "tableSelector", "waitForSelector", "settleMs", "timeoutMs", "tableIndex", "rowOffset", "rowLimit", "maxTables", "maxColumns", "maxCellCharacters"})
    table_index = arguments.get("tableIndex")
    if table_index is not None:
        table_index = bounded_integer(arguments, "tableIndex", 0, 0, MAX_TABLE_INDEX)
    return {
        "url": arguments.get("url"),
        "browser": validate_browser_options(arguments),
        "tableSelector": optional_selector(arguments, "tableSelector") or "table",
        "waitForSelector": optional_selector(arguments, "waitForSelector"),
        "settleMs": bounded_integer(arguments, "settleMs", 500, 0, 3_000),
        "timeoutMs": bounded_integer(arguments, "timeoutMs", 20_000, 1_000, 30_000),
        "tableIndex": table_index,
        "rowOffset": bounded_integer(arguments, "rowOffset", 0, 0, MAX_ROW_OFFSET),
        "rowLimit": bounded_integer(arguments, "rowLimit", 100, 1, MAX_TABLE_ROWS),
        "maxTables": bounded_integer(arguments, "maxTables", 10, 1, MAX_TABLES),
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
    discovery = validate_discover_links({"url": "https://example.com", "maxCandidates": 5, "sampleLimit": 2})
    assert (discovery["maxCandidates"], discovery["sampleLimit"]) == (5, 2)
    assert validate_query({"url": "https://example.com", "queries": [{"name": "title", "selector": "h1"}]})["queries"][0]["fields"] == ["text"]
    tables = validate_tables({"url": "https://example.com"})
    assert tables["tableSelector"] == "table"
    assert tables["tableIndex"] is None
    assert tables["rowOffset"] == 0
    assert tables["rowLimit"] == 100
    paged = validate_tables({"url": "https://example.com", "tableIndex": 4, "rowOffset": 100, "rowLimit": 25})
    assert (paged["tableIndex"], paged["rowOffset"], paged["rowLimit"]) == (4, 100, 25)
    configured = validate_retrieve({"url": "https://example.com", "browser": {"userAgent": "Example/1.0", "language": "en-US", "timezone": "Europe/Berlin", "viewport": {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True}, "colorScheme": "dark", "reducedMotion": "reduce", "javascriptEnabled": False}})["browser"]
    assert configured["viewport"] == {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True}
    assert configured["javascriptEnabled"] is False
    assert truncate_text("abcd", 3) == ("abc", True, 4)
    for invalid in (
        lambda: validate_retrieve({"url": "https://example.com", "extra": True}),
        lambda: validate_query({"url": "https://example.com", "queries": []}),
        lambda: validate_query({"url": "https://example.com", "queries": [{"name": "x", "selector": "a", "fields": ["html"]}]}),
        lambda: validate_tables({"url": "https://example.com", "maxRows": 0}),
        lambda: validate_tables({"url": "https://example.com", "rowOffset": -1}),
        lambda: validate_retrieve({"url": "https://example.com", "browser": {"language": "not a language"}}),
        lambda: validate_retrieve({"url": "https://example.com", "browser": {"viewport": {"width": 1280}}}),
        lambda: validate_retrieve({"url": "https://example.com", "browser": {"userAgent": "bad\nagent"}}),
        lambda: validate_discover_links({"url": "https://example.com", "maxCandidates": 0}),
        lambda: validate_discover_links({"url": "https://example.com", "sampleLimit": 6}),
    ):
        try:
            invalid()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid extraction arguments were accepted")


if __name__ == "__main__":
    self_test()

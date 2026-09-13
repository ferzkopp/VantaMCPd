#!/usr/bin/env python3
import difflib
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import tomllib
from typing import Any

from operation_common import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_OUTPUT,
    MAX_PATTERN,
    MAX_RESULTS,
    REGEX_TIMEOUT_SECONDS,
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


def regex_replace_worker(connection: Any, text: str, pattern: str, replacement: str, flags: int, count: int) -> None:
    try:
        value, substitutions = re.subn(pattern, replacement, text, count=count, flags=flags)
        connection.send((True, value, substitutions))
    except Exception as error:
        connection.send((False, str(error), 0))
    finally:
        connection.close()


def regex_replace(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    pattern = optional_string(arguments, "pattern", "", MAX_PATTERN)
    replacement = optional_string(arguments, "replacement", "", 16_384)
    count = bounded_int(arguments, "count", 0, 0, MAX_RESULTS)
    flags = re.IGNORECASE if optional_bool(arguments, "ignoreCase", False) else 0
    flags |= re.MULTILINE if optional_bool(arguments, "multiline", False) else 0
    parent, child = multiprocessing.Pipe(False)
    process = multiprocessing.Process(target=regex_replace_worker, args=(child, text, pattern, replacement, flags, count))
    process.start()
    child.close()
    try:
        if not parent.poll(REGEX_TIMEOUT_SECONDS):
            process.kill()
            process.join()
            raise ValueError(f"regex replacement exceeded {REGEX_TIMEOUT_SECONDS:g} seconds")
        ok, value, substitutions = parent.recv()
    finally:
        parent.close()
        if process.is_alive():
            process.join(0.1)
            if process.is_alive():
                process.kill()
        process.join()
    if not ok:
        raise ValueError(f"invalid regular expression or replacement: {value}")
    return {"text": value, "replacements": substitutions}


def make_diff(arguments: dict[str, Any]) -> dict[str, Any]:
    before = require_text(arguments, "before")
    after = require_text(arguments, "after")
    context = bounded_int(arguments, "context", 3, 0, 100)
    value = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=optional_string(arguments, "fromLabel", "before", 200),
        tofile=optional_string(arguments, "toLabel", "after", 200), n=context,
    ))
    return output_text(value)


SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


def parse_semver(value: str) -> tuple[int, int, int, tuple[str, ...], str | None]:
    match = SEMVER_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"invalid semantic version: {value}")
    return int(match[1]), int(match[2]), int(match[3]), tuple((match[4] or "").split(".")) if match[4] else (), match[5]


def compare_identifiers(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for left_value, right_value in zip(left, right):
        if left_value == right_value:
            continue
        if left_value.isdigit() and right_value.isdigit():
            return -1 if int(left_value) < int(right_value) else 1
        if left_value.isdigit() != right_value.isdigit():
            return -1 if left_value.isdigit() else 1
        return -1 if left_value < right_value else 1
    return (len(left) > len(right)) - (len(left) < len(right))


def semver(arguments: dict[str, Any]) -> dict[str, Any]:
    version = optional_string(arguments, "version", "", 200)
    parsed = parse_semver(version)
    action = choice(arguments, "action", {"parse", "compare"}, "parse")
    result = {"valid": True, "version": version, "major": parsed[0], "minor": parsed[1], "patch": parsed[2], "prerelease": list(parsed[3]), "build": parsed[4]}
    if action == "compare":
        other = optional_string(arguments, "otherVersion", "", 200)
        other_parsed = parse_semver(other)
        comparison = (parsed[:3] > other_parsed[:3]) - (parsed[:3] < other_parsed[:3])
        if comparison == 0:
            comparison = compare_identifiers(parsed[3], other_parsed[3])
        result.update({"otherVersion": other, "comparison": comparison, "relation": "equal" if comparison == 0 else "greater" if comparison > 0 else "less"})
    return result


def markdown_to_text(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)", lambda match: match.group(1) or match.group(2), text)
    return output_text(text)


def slug_anchor(value: str) -> str:
    value = re.sub(r"[^\w\s-]", "", value.casefold(), flags=re.UNICODE)
    return re.sub(r"[-\s]+", "-", value).strip("-")


def document_toc(arguments: dict[str, Any]) -> dict[str, Any]:
    minimum = bounded_int(arguments, "minLevel", 1, 1, 6)
    maximum = bounded_int(arguments, "maxLevel", 6, minimum, 6)
    headings = []
    in_fence = False
    for line in require_text(arguments).splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not in_fence and match and minimum <= len(match[1]) <= maximum:
            title = match[2]
            headings.append({"level": len(match[1]), "title": title, "anchor": slug_anchor(title)})
            if len(headings) > MAX_RESULTS:
                raise ValueError(f"table of contents exceeds {MAX_RESULTS} headings")
    rendered = "\n".join("  " * (item["level"] - minimum) + f"- [{item['title']}](#{item['anchor']})" for item in headings)
    return {"text": rendered, "items": headings, "count": len(headings)}


def normalize_fences(arguments: dict[str, Any]) -> dict[str, Any]:
    marker = choice(arguments, "marker", {"```", "~~~"}, "```")
    lines = require_text(arguments).splitlines()
    output = []
    in_fence = False
    for line in lines:
        match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if match:
            suffix = match[2].strip() if not in_fence else ""
            output.append(marker + suffix)
            in_fence = not in_fence
        else:
            output.append(line)
    return {"text": "\n".join(output), "balanced": not in_fence}


def frontmatter_parse(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    if text.startswith("+++\n"):
        end = text.find("\n+++", 4)
        if end < 0:
            raise ValueError("unclosed TOML frontmatter")
        raw = text[4:end]
        try:
            metadata = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f"invalid TOML frontmatter: {error}") from error
        return {"format": "toml", "metadata": metadata, "body": text[end + 4:].lstrip("\r\n")}
    if text.startswith("{\n") or text.startswith("{\r\n"):
        decoder = json.JSONDecoder()
        try:
            metadata, end = decoder.raw_decode(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON frontmatter: {error}") from error
        if not isinstance(metadata, dict):
            raise ValueError("JSON frontmatter must be an object")
        return {"format": "json", "metadata": metadata, "body": text[end:].lstrip("\r\n")}
    if text.startswith("---"):
        raise ValueError("YAML frontmatter is not supported; use JSON or TOML")
    return {"format": None, "metadata": {}, "body": text}


def run_command(command: list[str], text: str) -> tuple[str, str, int]:
    executable = shutil.which(command[0])
    if executable is None:
        raise ValueError(f"required command is unavailable: {command[0]}")
    try:
        completed = subprocess.run(
            [executable, *command[1:]], input=text.encode(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=COMMAND_TIMEOUT_SECONDS, check=False, cwd="/" if os.name == "posix" else None,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"{command[0]} exceeded {COMMAND_TIMEOUT_SECONDS:g} seconds") from error
    stdout = completed.stdout[:MAX_OUTPUT + 1]
    stderr = completed.stderr[:16_385]
    if len(stdout) > MAX_OUTPUT:
        raise ValueError(f"{command[0]} output exceeds {MAX_OUTPUT} bytes")
    if len(stderr) > 16_384:
        raise ValueError(f"{command[0]} error output exceeds 16384 bytes")
    try:
        return stdout.decode("utf-8"), stderr.decode("utf-8"), completed.returncode
    except UnicodeDecodeError as error:
        raise ValueError(f"{command[0]} returned non-UTF-8 output") from error


def rg_search(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    pattern = optional_string(arguments, "pattern", "", MAX_PATTERN)
    if not pattern:
        raise ValueError("pattern must be non-empty")
    maximum = bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS)
    command = ["rg", "--json", "--color", "never", "--line-number", "--max-count", str(maximum)]
    if optional_bool(arguments, "fixedStrings", False):
        command.append("--fixed-strings")
    if optional_bool(arguments, "ignoreCase", False):
        command.append("--ignore-case")
    if optional_bool(arguments, "wordRegexp", False):
        command.append("--word-regexp")
    command.extend(["--regexp", pattern, "-"])
    stdout, stderr, code = run_command(command, text)
    if code not in {0, 1}:
        raise ValueError(f"rg failed: {stderr.strip() or f'exit {code}'}")
    items = []
    for line in stdout.splitlines():
        event = json.loads(line)
        if event.get("type") != "match":
            continue
        data = event["data"]
        items.append({
            "line": data["line_number"], "text": data["lines"]["text"].rstrip("\n"),
            "submatches": [{"text": match["match"]["text"], "start": match["start"], "end": match["end"]} for match in data["submatches"]],
        })
    return {"items": items, "count": len(items), "truncated": len(items) >= maximum}


def jq_filter(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    expression = optional_string(arguments, "filter", ".", MAX_PATTERN)
    if re.search(r"(?:\b(?:import|include|module|env|input_filename)\b|\$ENV\b)", expression):
        raise ValueError("jq filter uses a disabled environment or module feature")
    command = ["jq", "--compact-output" if optional_bool(arguments, "compact", False) else "--monochrome-output", expression]
    stdout, stderr, code = run_command(command, text)
    if code != 0:
        raise ValueError(f"jq failed: {stderr.strip() or f'exit {code}'}")
    return output_text(stdout.rstrip("\n"))


def awk_columns(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    columns = arguments.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= 100 or not all(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1_000 for value in columns):
        raise ValueError("columns must contain 1 to 100 integers from 1 to 1000")
    input_delimiter = optional_string(arguments, "inputDelimiter", " ", 32)
    output_delimiter = optional_string(arguments, "outputDelimiter", "\t", 32)
    program = "{ print " + ", ".join(f"${value}" for value in columns) + " }"
    stdout, stderr, code = run_command(["awk", "-F", input_delimiter, "-v", f"OFS={output_delimiter}", program], text)
    if code != 0:
        raise ValueError(f"awk failed: {stderr.strip() or f'exit {code}'}")
    return output_text(stdout.rstrip("\n"))


def sed_replace(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    pattern = optional_string(arguments, "pattern", "", MAX_PATTERN)
    replacement = optional_string(arguments, "replacement", "", 16_384)
    if not pattern or "\n" in pattern or "\r" in pattern or "\n" in replacement or "\r" in replacement:
        raise ValueError("pattern must be non-empty and pattern/replacement must be single-line")
    delimiter = "\x1f"
    pattern = pattern.replace(delimiter, "\\" + delimiter)
    replacement = replacement.replace("\\", "\\\\").replace(delimiter, "\\" + delimiter)
    suffix = "g" if optional_bool(arguments, "global", True) else ""
    stdout, stderr, code = run_command(["sed", "-E", f"s{delimiter}{pattern}{delimiter}{replacement}{delimiter}{suffix}"], text)
    if code != 0:
        raise ValueError(f"sed failed: {stderr.strip() or f'exit {code}'}")
    return output_text(stdout.rstrip("\n"))


DEVELOPER_OPERATIONS = {
    "regex_replace": Operation("Replace regular-expression matches in a killable worker.", text_properties(pattern=typed("string", maxLength=MAX_PATTERN), replacement=typed("string", maxLength=16_384), count=typed("integer", minimum=0, maximum=MAX_RESULTS), ignoreCase=typed("boolean"), multiline=typed("boolean")), ("text", "pattern", "replacement"), regex_replace),
    "diff": Operation("Create a unified diff between two texts.", {"before": {"type": "string"}, "after": {"type": "string"}, "fromLabel": typed("string", maxLength=200), "toLabel": typed("string", maxLength=200), "context": typed("integer", minimum=0, maximum=100)}, ("before", "after"), make_diff),
    "semver": Operation("Parse or compare strict Semantic Versioning 2.0 versions.", {"version": typed("string", minLength=1, maxLength=200), "action": {"enum": ["parse", "compare"]}, "otherVersion": typed("string", minLength=1, maxLength=200)}, ("version",), semver),
}

DOCUMENT_OPERATIONS = {
    "markdown_to_text": Operation("Remove common Markdown presentation syntax without rendering HTML.", text_properties(), ("text",), markdown_to_text),
    "toc": Operation("Build a Markdown table of contents from ATX headings.", text_properties(minLevel=typed("integer", minimum=1, maximum=6), maxLevel=typed("integer", minimum=1, maximum=6)), ("text",), document_toc),
    "normalize_fences": Operation("Normalize fenced code block markers.", text_properties(marker={"enum": ["```", "~~~"]}), ("text",), normalize_fences),
    "frontmatter_parse": Operation("Parse JSON or TOML frontmatter and return the body.", text_properties(), ("text",), frontmatter_parse),
}

COMMAND_OPERATIONS = {
    "rg_search": Operation("Search stdin with ripgrep using fixed safe flags.", text_properties(pattern=typed("string", minLength=1, maxLength=MAX_PATTERN), fixedStrings=typed("boolean"), ignoreCase=typed("boolean"), wordRegexp=typed("boolean"), maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text", "pattern"), rg_search),
    "jq_filter": Operation("Apply one jq filter to JSON received on stdin.", text_properties(filter=typed("string", minLength=1, maxLength=MAX_PATTERN), compact=typed("boolean")), ("text", "filter"), jq_filter),
    "awk_columns": Operation("Select numbered fields with a generated fixed awk program.", text_properties(columns=typed("array", minItems=1, maxItems=100, items=typed("integer", minimum=1, maximum=1_000)), inputDelimiter=typed("string", maxLength=32), outputDelimiter=typed("string", maxLength=32)), ("text", "columns"), awk_columns),
    "sed_replace": Operation("Run one bounded extended-regex sed substitution.", text_properties(pattern=typed("string", minLength=1, maxLength=MAX_PATTERN), replacement=typed("string", maxLength=16_384), **{"global": typed("boolean")}), ("text", "pattern", "replacement"), sed_replace),
}

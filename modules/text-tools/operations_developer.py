#!/usr/bin/env python3
import difflib
import io
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import tokenize
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
    optional_module,
    optional_string,
    output_text,
    require_text,
    string_list,
    text_properties,
    typed,
)


def regex_replace_worker(connection: Any, text: str, pattern: str, replacement: str, flags: int, count: int) -> None:
    try:
        value, substitutions = re.subn(pattern, replacement, text, count=count, flags=flags)
        connection.send((True, {"text": value, "replacements": substitutions}))
    except Exception as error:
        connection.send((False, str(error)))
    finally:
        connection.close()


def regex_extract_worker(connection: Any, text: str, pattern: str, flags: int, limit: int) -> None:
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
        connection.send((True, {"matches": matches, "count": len(matches), "truncated": len(matches) >= limit}))
    except Exception as error:
        connection.send((False, str(error)))
    finally:
        connection.close()


def run_regex_worker(target: Any, arguments: tuple[Any, ...], label: str) -> dict[str, Any]:
    """Evaluate a caller-supplied pattern in a killable child so backtracking cannot hang the node."""
    # A Pipe rather than a Queue: killing a process that feeds a Queue can leave its background
    # feeder thread and /dev/shm semaphores behind.
    parent, child = multiprocessing.Pipe(False)
    process = multiprocessing.Process(target=target, args=(child, *arguments))
    process.start()
    child.close()
    try:
        if not parent.poll(REGEX_TIMEOUT_SECONDS):
            process.kill()
            process.join()
            raise ValueError(f"{label} exceeded {REGEX_TIMEOUT_SECONDS:g} seconds")
        ok, payload = parent.recv()
    finally:
        parent.close()
        if process.is_alive():
            process.join(0.1)
            if process.is_alive():
                process.kill()
        process.join()
    if not ok:
        raise ValueError(f"invalid regular expression: {payload}")
    return payload


def regex_flags(arguments: dict[str, Any]) -> int:
    flags = re.IGNORECASE if optional_bool(arguments, "ignoreCase", False) else 0
    flags |= re.MULTILINE if optional_bool(arguments, "multiline", False) else 0
    flags |= re.DOTALL if optional_bool(arguments, "dotAll", False) else 0
    return flags


def regex_extract(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    pattern = optional_string(arguments, "pattern", "", MAX_PATTERN)
    if not pattern:
        raise ValueError("pattern must be a non-empty string")
    limit = bounded_int(arguments, "maxMatches", 100, 1, MAX_RESULTS)
    return run_regex_worker(regex_extract_worker, (text, pattern, regex_flags(arguments), limit), "regex evaluation")


def regex_replace(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    pattern = optional_string(arguments, "pattern", "", MAX_PATTERN)
    replacement = optional_string(arguments, "replacement", "", 16_384)
    count = bounded_int(arguments, "count", 0, 0, MAX_RESULTS)
    flags = regex_flags(arguments)
    return run_regex_worker(regex_replace_worker, (text, pattern, replacement, flags, count), "regex replacement")


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


def regex_test_worker(connection: Any, pattern: str, samples: list[str], flags: int) -> None:
    try:
        compiled = re.compile(pattern, flags)
        results = []
        for sample in samples:
            match = compiled.search(sample)
            results.append({
                "sample": sample,
                "matched": match is not None,
                "match": match.group(0) if match else None,
                "start": match.start() if match else None,
                "end": match.end() if match else None,
                "groups": list(match.groups()) if match else [],
                "namedGroups": match.groupdict() if match else {},
            })
        connection.send((True, {
            "valid": True,
            "groupCount": compiled.groups,
            "groupNames": sorted(compiled.groupindex, key=compiled.groupindex.get),
            "items": results,
            "matchedSamples": sum(item["matched"] for item in results),
            "count": len(results),
        }))
    except Exception as error:
        connection.send((False, str(error)))
    finally:
        connection.close()


def regex_test(arguments: dict[str, Any]) -> dict[str, Any]:
    pattern = optional_string(arguments, "pattern", "", MAX_PATTERN)
    if not pattern:
        raise ValueError("pattern must be a non-empty string")
    samples = string_list(arguments, "samples", 1, 100, 4_096)
    try:
        return run_regex_worker(regex_test_worker, (pattern, samples, regex_flags(arguments)), "regex testing")
    except ValueError as error:
        if str(error).startswith("invalid regular expression"):
            return {"valid": False, "error": str(error).split(": ", 1)[-1], "items": [], "count": 0, "matchedSamples": 0}
        raise


def strip_comments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Uses the real Python tokenizer, so strings that look like comments are never touched."""
    choice(arguments, "language", {"python"}, "python")
    text = require_text(arguments)
    drop_docstrings = optional_bool(arguments, "docstrings", False)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as error:
        raise ValueError(f"input is not tokenizable Python: {error}") from error

    removed = 0
    kept: list[tokenize.TokenInfo] = []
    previous_type = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.COMMENT:
            removed += 1
            continue
        is_docstring = drop_docstrings and token.type == tokenize.STRING and previous_type in {tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.DEDENT, tokenize.ENCODING}
        if is_docstring:
            removed += 1
            continue
        kept.append(token)
        if token.type not in {tokenize.NL, tokenize.COMMENT}:
            previous_type = token.type
    value = tokenize.untokenize(((token.type, token.string) for token in kept))
    value = "\n".join(line.rstrip() for line in value.splitlines())
    return {"text": value, "removed": removed}


HEADING_LINE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_LINE = re.compile(r"^\s*(```|~~~)")


def heading_lines(text: str) -> list[tuple[int, int, str]]:
    """Headings outside fenced code, as (line index, level, title)."""
    found = []
    in_fence = False
    for index, line in enumerate(text.splitlines()):
        if FENCE_LINE.match(line):
            in_fence = not in_fence
            continue
        match = HEADING_LINE.match(line)
        if match and not in_fence:
            found.append((index, len(match[1]), match[2]))
    return found


def split_sections(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    level = bounded_int(arguments, "level", 2, 1, 6)
    limit = bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS)
    lines = text.splitlines()
    headings = heading_lines(text)
    boundaries = [item for item in headings if item[1] <= level]

    sections = []
    if not boundaries or boundaries[0][0] > 0:
        preamble = "\n".join(lines[:boundaries[0][0]] if boundaries else lines)
        if preamble.strip():
            sections.append({"level": 0, "title": None, "path": [], "startLine": 1, "text": preamble})

    ancestry: list[tuple[int, str]] = []
    for position, (index, heading_level, title) in enumerate(boundaries):
        end = boundaries[position + 1][0] if position + 1 < len(boundaries) else len(lines)
        while ancestry and ancestry[-1][0] >= heading_level:
            ancestry.pop()
        ancestry.append((heading_level, title))
        sections.append({
            "level": heading_level,
            "title": title,
            "path": [name for _level, name in ancestry],
            "anchor": slug_anchor(title),
            "startLine": index + 1,
            "text": "\n".join(lines[index:end]).rstrip(),
        })
        if len(sections) > limit:
            raise ValueError(f"document splits into more than {limit} sections")
    return {"items": sections, "count": len(sections), "truncated": False}


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", stripped)]


def tables_extract(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    limit = bounded_int(arguments, "maxResults", 20, 1, MAX_RESULTS)
    lines = text.splitlines()
    tables = []
    index = 0
    in_fence = False
    while index < len(lines) - 1:
        if FENCE_LINE.match(lines[index]):
            in_fence = not in_fence
        separator = lines[index + 1].strip()
        if in_fence or "|" not in lines[index] or not re.fullmatch(r"\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?", separator):
            index += 1
            continue
        header = split_markdown_row(lines[index])
        alignments = [("center" if cell.startswith(":") and cell.endswith(":") else "right" if cell.endswith(":") else "left" if cell.startswith(":") else "default") for cell in split_markdown_row(lines[index + 1])]
        rows = []
        cursor = index + 2
        while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
            cells = split_markdown_row(lines[cursor])
            rows.append({name: cells[position] if position < len(cells) else "" for position, name in enumerate(header)})
            cursor += 1
            if len(rows) > MAX_RESULTS:
                raise ValueError(f"table exceeds {MAX_RESULTS} rows")
        tables.append({"startLine": index + 1, "columns": header, "alignments": alignments, "rows": rows, "rowCount": len(rows)})
        if len(tables) >= limit:
            break
        index = cursor
    return {"items": tables, "count": len(tables), "truncated": False}


def links_extract(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    limit = bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS)
    items = []
    for match in re.finditer(r"(!?)\[([^\]]*)\]\(\s*(<[^>]*>|[^\s)]*)(?:\s+[\"']([^\"']*)[\"'])?\s*\)", text):
        destination = match.group(3).strip("<>")
        items.append({
            "kind": "image" if match.group(1) else "link",
            "text": match.group(2),
            "destination": destination,
            "title": match.group(4),
            "absolute": bool(re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", destination)),
            "anchor": destination.startswith("#"),
            "start": match.start(),
        })
        if len(items) >= limit:
            break
    for match in re.finditer(r"^\s{0,3}\[([^\]]+)\]:\s*(\S+)(?:\s+[\"']([^\"']*)[\"'])?\s*$", text, re.MULTILINE):
        if len(items) >= limit:
            break
        items.append({"kind": "reference", "text": match.group(1), "destination": match.group(2), "title": match.group(3), "absolute": bool(re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", match.group(2))), "anchor": match.group(2).startswith("#"), "start": match.start()})
    items.sort(key=lambda item: item["start"])
    return {"items": items, "count": len(items), "truncated": False}


def heading_shift(arguments: dict[str, Any]) -> dict[str, Any]:
    by = arguments.get("by")
    if not isinstance(by, int) or isinstance(by, bool) or not -5 <= by <= 5:
        raise ValueError("by must be an integer from -5 to 5")
    lines = require_text(arguments).splitlines()
    shifted = 0
    in_fence = False
    output = []
    for line in lines:
        if FENCE_LINE.match(line):
            in_fence = not in_fence
            output.append(line)
            continue
        match = HEADING_LINE.match(line)
        if in_fence or not match:
            output.append(line)
            continue
        level = len(match[1]) + by
        if not 1 <= level <= 6:
            raise ValueError(f"shifting would move a heading to level {level}, outside 1-6")
        output.append("#" * level + " " + match[2])
        shifted += 1
    return {"text": "\n".join(output), "headings": shifted}


def markdown_to_html(arguments: dict[str, Any]) -> dict[str, Any]:
    markdown = optional_module("markdown", "python3-markdown", "Markdown rendering")
    text = require_text(arguments)
    extensions = ["tables", "fenced_code", "sane_lists"] if optional_bool(arguments, "extensions", True) else []
    rendered = markdown.markdown(text, extensions=extensions, output_format="html")
    # Raw HTML in the source passes through; this module does not sanitize (see Operations.md).
    raw_html = bool(re.search(r"<\s*(script|style|iframe|object|embed|form)\b", text, re.IGNORECASE))
    result = {"text": rendered, "characters": len(rendered), "sanitized": False}
    if raw_html:
        result["warning"] = "The source contains raw HTML that was passed through unchanged; sanitize before rendering in a browser."
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
        end = re.search(r"^---\s*$", text[3:], re.MULTILINE)
        if end is None:
            raise ValueError("unclosed YAML frontmatter")
        yaml = optional_module("yaml", "python3-yaml", "YAML frontmatter")
        raw = text[3:3 + end.start()]
        try:
            metadata = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise ValueError(f"invalid YAML frontmatter: {error}") from error
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("YAML frontmatter must be a mapping")
        return {"format": "yaml", "metadata": metadata, "body": text[3 + end.end():].lstrip("\r\n")}
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
    before = bounded_int(arguments, "contextBefore", 0, 0, 20)
    after = bounded_int(arguments, "contextAfter", 0, 0, 20)
    command = ["rg", "--json", "--color", "never", "--line-number", "--max-count", str(maximum)]
    if before:
        command.extend(["--before-context", str(before)])
    if after:
        command.extend(["--after-context", str(after)])
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
    context = []
    for line in stdout.splitlines():
        event = json.loads(line)
        kind = event.get("type")
        if kind not in {"match", "context"}:
            continue
        data = event["data"]
        entry = {"line": data["line_number"], "text": data["lines"]["text"].rstrip("\n")}
        if kind == "context":
            context.append({**entry, "kind": "context"})
            continue
        entry["submatches"] = [{"text": match["match"]["text"], "start": match["start"], "end": match["end"]} for match in data["submatches"]]
        items.append({**entry, "kind": "match"})
    result = {"items": items, "count": len(items), "truncated": len(items) >= maximum}
    if before or after:
        result["lines"] = sorted([*items, *context], key=lambda entry: entry["line"])
    return result


def jq_filter(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    expression = optional_string(arguments, "filter", ".", MAX_PATTERN)
    if re.search(r"(?:\b(?:import|include|module|env|input_filename)\b|\$ENV\b)", expression):
        raise ValueError("jq filter uses a disabled environment or module feature")
    command = ["jq"]
    command.append("--compact-output" if optional_bool(arguments, "compact", False) else "--monochrome-output")
    if optional_bool(arguments, "rawOutput", False):
        command.append("--raw-output")
    if optional_bool(arguments, "slurp", False):
        command.append("--slurp")
    command.append(expression)
    stdout, stderr, code = run_command(command, text)
    if code != 0:
        raise ValueError(f"jq failed: {stderr.strip() or f'exit {code}'}")
    return output_text(stdout.rstrip("\n"))


def awk_stats(arguments: dict[str, Any]) -> dict[str, Any]:
    """Numeric summary of one column, using a fixed program built only from a validated index."""
    text = require_text(arguments)
    column = bounded_int(arguments, "column", 1, 1, 1_000)
    input_delimiter = optional_string(arguments, "inputDelimiter", " ", 32)
    skip = bounded_int(arguments, "skipLines", 0, 0, 100)
    program = (
        f"NR > {skip} && $" + str(column) + " ~ /^[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][+-]?[0-9]+)?$/ "
        "{ value = $" + str(column) + " + 0; count++; sum += value; "
        "if (count == 1 || value < min) min = value; if (count == 1 || value > max) max = value } "
        'END { if (count) printf "%d\\t%.10g\\t%.10g\\t%.10g\\t%.10g\\n", count, sum, sum / count, min, max; else print "0" }'
    )
    stdout, stderr, code = run_command(["awk", "-F", input_delimiter, program], text)
    if code != 0:
        raise ValueError(f"awk failed: {stderr.strip() or f'exit {code}'}")
    fields = stdout.strip().split("\t")
    if len(fields) < 5:
        return {"column": column, "count": 0, "note": "no numeric values found in that column"}
    count, total, mean, minimum, maximum = fields
    return {"column": column, "count": int(count), "sum": float(total), "mean": float(mean), "min": float(minimum), "max": float(maximum)}


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
    "regex_extract": Operation("Extract bounded regular-expression matches and capture groups in a killable worker.", text_properties(pattern=typed("string", minLength=1, maxLength=MAX_PATTERN), maxMatches=typed("integer", minimum=1, maximum=MAX_RESULTS), ignoreCase=typed("boolean"), multiline=typed("boolean"), dotAll=typed("boolean")), ("text", "pattern"), regex_extract),
    "regex_replace": Operation("Replace regular-expression matches in a killable worker.", text_properties(pattern=typed("string", maxLength=MAX_PATTERN), replacement=typed("string", maxLength=16_384), count=typed("integer", minimum=0, maximum=MAX_RESULTS), ignoreCase=typed("boolean"), multiline=typed("boolean"), dotAll=typed("boolean")), ("text", "pattern", "replacement"), regex_replace),
    "diff": Operation("Create a unified diff between two texts.", {"before": {"type": "string"}, "after": {"type": "string"}, "fromLabel": typed("string", maxLength=200), "toLabel": typed("string", maxLength=200), "context": typed("integer", minimum=0, maximum=100)}, ("before", "after"), make_diff),
    "semver": Operation("Parse or compare strict Semantic Versioning 2.0 versions.", {"version": typed("string", minLength=1, maxLength=200), "action": {"enum": ["parse", "compare"]}, "otherVersion": typed("string", minLength=1, maxLength=200)}, ("version",), semver),
    "regex_test": Operation("Validate a pattern and report which sample strings it matches.", {"pattern": typed("string", minLength=1, maxLength=MAX_PATTERN), "samples": typed("array", minItems=1, maxItems=100, items=typed("string")), "ignoreCase": typed("boolean"), "multiline": typed("boolean"), "dotAll": typed("boolean")}, ("pattern", "samples"), regex_test),
    "strip_comments": Operation("Remove Python comments, and optionally docstrings, using the real tokenizer.", text_properties(language={"enum": ["python"]}, docstrings=typed("boolean")), ("text",), strip_comments),
}

DOCUMENT_OPERATIONS = {
    "markdown_to_text": Operation("Remove common Markdown presentation syntax without rendering HTML.", text_properties(), ("text",), markdown_to_text),
    "toc": Operation("Build a Markdown table of contents from ATX headings.", text_properties(minLevel=typed("integer", minimum=1, maximum=6), maxLevel=typed("integer", minimum=1, maximum=6)), ("text",), document_toc),
    "normalize_fences": Operation("Normalize fenced code block markers.", text_properties(marker={"enum": ["```", "~~~"]}), ("text",), normalize_fences),
    "frontmatter_parse": Operation("Parse JSON, TOML, or YAML frontmatter and return the body.", text_properties(), ("text",), frontmatter_parse),
    "split_sections": Operation("Split Markdown into sections at a heading level, with heading paths.", text_properties(level=typed("integer", minimum=1, maximum=6), maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), split_sections),
    "tables_extract": Operation("Extract Markdown tables as JSON rows with column alignments.", text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), tables_extract),
    "links_extract": Operation("Extract Markdown links, images, and reference definitions.", text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), links_extract),
    "heading_shift": Operation("Shift every Markdown heading by a number of levels.", text_properties(by=typed("integer", minimum=-5, maximum=5)), ("text", "by"), heading_shift),
    "markdown_to_html": Operation("Render Markdown as HTML without sanitizing (needs python3-markdown).", text_properties(extensions=typed("boolean")), ("text",), markdown_to_html),
}

COMMAND_OPERATIONS = {
    "rg_search": Operation("Search stdin with ripgrep using fixed safe flags.", text_properties(pattern=typed("string", minLength=1, maxLength=MAX_PATTERN), fixedStrings=typed("boolean"), ignoreCase=typed("boolean"), wordRegexp=typed("boolean"), contextBefore=typed("integer", minimum=0, maximum=20), contextAfter=typed("integer", minimum=0, maximum=20), maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text", "pattern"), rg_search),
    "jq_filter": Operation("Apply one jq filter to JSON received on stdin.", text_properties(filter=typed("string", minLength=1, maxLength=MAX_PATTERN), compact=typed("boolean"), rawOutput=typed("boolean"), slurp=typed("boolean")), ("text", "filter"), jq_filter),
    "awk_columns": Operation("Select numbered fields with a generated fixed awk program.", text_properties(columns=typed("array", minItems=1, maxItems=100, items=typed("integer", minimum=1, maximum=1_000)), inputDelimiter=typed("string", maxLength=32), outputDelimiter=typed("string", maxLength=32)), ("text", "columns"), awk_columns),
    "awk_stats": Operation("Summarize one numeric column with count, sum, mean, min, and max.", text_properties(column=typed("integer", minimum=1, maximum=1_000), inputDelimiter=typed("string", maxLength=32), skipLines=typed("integer", minimum=0, maximum=100)), ("text", "column"), awk_stats),
    "sed_replace": Operation("Run one bounded extended-regex sed substitution.", text_properties(pattern=typed("string", minLength=1, maxLength=MAX_PATTERN), replacement=typed("string", maxLength=16_384), **{"global": typed("boolean")}), ("text", "pattern", "replacement"), sed_replace),
}

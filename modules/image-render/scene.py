#!/usr/bin/env python3
"""Scene preparation and POV-Ray output parsing for Image Render. Pure functions, no I/O."""
import re
from typing import Any

from schemas import FORBIDDEN_SUFFIXES

# A complete string literal, so the reference never matches inside comments or longer strings.
ARTIFACT_REFERENCE = re.compile(r'"artifact:([0-9a-f]{32})"')
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_LOCATED = re.compile(r"^File '(?P<file>[^']*)' line (?P<line>\d+):\s*(?P<message>.*)$")
_LOCATED_ALT = re.compile(r"^File:\s*(?P<file>\S+)\s+Line:\s*(?P<line>\d+)\s*$")
_SEVERITY = re.compile(r"^(?P<kind>Parse Error|Possible Parse Error|Parse Warning|Warning|Possible Error|Error)\s*:\s*(?P<text>.*)$")
_FATAL = re.compile(r"^(Fatal error.*|Render failed.*)$")
_TIME = re.compile(r"^(?P<phase>Parse|Bounding|Photon|Radiosity|Trace|Render|Total Scene Processing|Total)\s+Time[s]?:?.*?\((?P<seconds>[0-9.]+) seconds\)")
_VERSION = re.compile(r"\b(?:POV-Ray|Version)\s+(3\.[0-9]+(?:\.[0-9]+)*)")
_PROGRESS = re.compile(r"^(Rendered|Parsing|Pretrace|Rendering|Building|Radiosity).*\b\d+\s*(of|%)")
_MESSAGE_START = re.compile(r"^(File '|File:|Parse Error|Possible Parse Error|Parse Warning|Warning|Possible Error|Error|Fatal)")
MAX_DIAGNOSTICS = 40
CONTEXT_LINES = 2


def references(texts: list[str]) -> list[str]:
    """Artifact IDs referenced as "artifact:<id>" literals, in first-use order."""
    seen: list[str] = []
    for text in texts:
        for match in ARTIFACT_REFERENCE.finditer(text):
            if match.group(1) not in seen:
                seen.append(match.group(1))
    return seen


def staged_name(artifact_id: str, original: str, taken: set[str]) -> str:
    """A unique, POV-Ray-safe file name that keeps the artifact's extension."""
    cleaned = _UNSAFE_NAME.sub("-", original or "artifact").strip(".-_") or "artifact"
    if cleaned.lower().endswith(FORBIDDEN_SUFFIXES):
        cleaned += ".txt"
    stem, dot, extension = cleaned.rpartition(".")
    extension = f".{extension}" if dot and stem and len(extension) <= 16 else ""
    if not extension:
        stem = cleaned
    base = (stem[: 80 - len(extension)].rstrip(".-_") or "artifact") + extension
    candidate = f"{artifact_id[:8]}-{base}"
    suffix = 1
    while candidate.lower() in taken:
        suffix += 1
        candidate = f"{artifact_id[:8]}-{suffix}-{base}"
    taken.add(candidate.lower())
    return candidate


def rewrite(text: str, mapping: dict[str, str]) -> str:
    return ARTIFACT_REFERENCE.sub(lambda match: f'"{mapping[match.group(1)]}"', text)


def decode_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1")


def clean_output(text: str) -> list[str]:
    """POV-Ray console lines without carriage-return progress spam."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        segments = [segment.rstrip() for segment in raw.split("\r") if segment.strip()]
        if not segments:
            continue
        line = segments[-1]
        # POV-Ray wraps long messages at 80 columns and indents the continuation by one space.
        if lines and line.startswith(" ") and not line.startswith("  ") and _MESSAGE_START.match(lines[-1]):
            lines[-1] = f"{lines[-1]} {line.strip()}"
        elif lines and _PROGRESS.match(line) and _PROGRESS.match(lines[-1]):
            lines[-1] = line
        else:
            lines.append(line)
    return lines


def _context(sources: dict[str, str], file: str, line: int) -> list[dict[str, Any]] | None:
    name = file.rsplit("/", 1)[-1]
    text = sources.get(file) or sources.get(name)
    if text is None:
        return None
    rows = text.splitlines()
    first = max(1, line - CONTEXT_LINES)
    last = min(len(rows), line + CONTEXT_LINES)
    return [{"line": number, "text": rows[number - 1][:400], "error": number == line} for number in range(first, last + 1)]


def diagnostics(lines: list[str], sources: dict[str, str]) -> list[dict[str, Any]]:
    """Parse errors and warnings with location and the surrounding lines of inline sources."""
    found: list[dict[str, Any]] = []
    location: tuple[str, int] | None = None
    for line in lines:
        stripped = line.strip()
        located = _LOCATED.match(stripped)
        if located:
            location = (located.group("file"), int(located.group("line")))
            stripped = located.group("message").strip()
            if not stripped:
                continue
        else:
            alternative = _LOCATED_ALT.match(stripped)
            if alternative:
                location = (alternative.group("file"), int(alternative.group("line")))
                continue
        severity = _SEVERITY.match(stripped)
        if severity:
            kind = severity.group("kind")
            entry: dict[str, Any] = {
                "severity": "error" if kind in ("Parse Error", "Error") else "warning",
                "kind": kind,
                "message": severity.group("text")[:600],
            }
        elif _FATAL.match(stripped):
            entry = {"severity": "error", "kind": "Fatal", "message": stripped[:600]}
        else:
            continue
        if location:
            entry["file"] = location[0].rsplit("/", 1)[-1] if location[0].startswith(("/work/", "/inputs/")) else location[0]
            entry["line"] = location[1]
            context = _context(sources, location[0], location[1])
            if context:
                entry["context"] = context
            location = None
        if entry not in found:
            found.append(entry)
        if len(found) >= MAX_DIAGNOSTICS:
            break
    return found


def statistics(lines: list[str]) -> dict[str, float]:
    times: dict[str, float] = {}
    for line in lines:
        match = _TIME.match(line.strip())
        if match:
            key = match.group("phase").split()[0].lower() + "Seconds"
            times[key] = float(match.group("seconds"))
    return times


def version(lines: list[str]) -> str | None:
    for line in lines:
        match = _VERSION.search(line)
        if match:
            return match.group(1)
    return None


def png_dimensions(header: bytes) -> tuple[int, int] | None:
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def self_test() -> None:
    first, second = "a" * 32, "b" * 32
    text = f'#include "artifact:{first}"\n// "artifact:{second}" in a comment still counts\nimage_map {{ png "artifact:{first}" }}\n"artifact:{first}x"'
    assert references([text]) == [first, second]
    rewritten = rewrite(text, {first: "/inputs/aaaaaaaa-part.inc", second: "/inputs/bbbbbbbb-photo.png"})
    assert '"/inputs/aaaaaaaa-part.inc"' in rewritten and f"artifact:{first}x" in rewritten
    taken: set[str] = set()
    assert staged_name(first, "../My Photo.png", taken) == "aaaaaaaa-My-Photo.png"
    assert staged_name(first, "../My Photo.png", taken) == "aaaaaaaa-2-My-Photo.png"
    assert staged_name(first, "x" * 120 + ".png", taken).endswith("x.png")
    assert staged_name(first, "settings.ini", taken) == "aaaaaaaa-settings.ini.txt"
    output = (
        "Persistence of Vision(tm) Ray Tracer Version 3.7.0.10.unofficial\r\n"
        "Parsing 10K tokens\rParsing 20K tokens\n"
        "File '/work/scene.pov' line 3: Parse Error: Expected 'object or directive', undeclared identifier\n"
        " 'spher' found instead\n"
        "Possible Parse Error: assumed_gamma not specified in this POV-Ray 3.7 or later\n"
        " scene.\n"
        "Fatal error in parser: Cannot parse input.\n"
        "Render failed\n"
        "Parse Time:    0 hours  0 minutes  0 seconds (0.012 seconds)\n"
    )
    lines = clean_output(output)
    assert "Parsing 10K tokens" not in lines
    found = diagnostics(lines, {"/work/scene.pov": "camera {}\nlight_source {}\nspher { 0, 1 }\n"})
    assert found[0]["file"] == "scene.pov" and found[0]["line"] == 3 and found[0]["severity"] == "error"
    assert found[0]["message"].endswith("undeclared identifier 'spher' found instead"), found[0]
    assert any(entry["severity"] == "warning" and entry["message"].endswith("later scene.") for entry in found)
    assert found[0]["context"][-1]["error"] is True or any(row["error"] for row in found[0]["context"])
    assert any(entry["kind"] == "Fatal" for entry in found)
    assert statistics(lines) == {"parseSeconds": 0.012}
    assert version(lines) == "3.7.0.10"
    assert png_dimensions(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x00\x80") == (256, 128)


if __name__ == "__main__":
    self_test()

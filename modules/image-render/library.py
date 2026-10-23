#!/usr/bin/env python3
"""Local POV-Ray knowledge library for Image Render.

At installation, --build indexes the Debian POV-Ray 3.7 HTML manual, the packaged example scenes, the
standard include files and the downloaded CC0 assets into library/index.json. At runtime the broker loads
that index read-only to search and read documentation, examples, includes and declared identifiers.
"""
import json
import math
import os
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from typing import Any

INDEX_VERSION = 1
POV_INCLUDE = os.environ.get("VANTA_RENDER_POV_INCLUDE", "/usr/share/povray-3.7/include")
ONLINE_DOCS = "https://www.povray.org/documentation/3.7.0/"
TOKEN = re.compile(r"[a-z0-9_]+")
HEADING_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$")
DECLARATION = re.compile(r"^\s*#\s*(declare|macro)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$")
TEXT_SUFFIXES = (".pov", ".inc", ".ini", ".txt", ".mcr", ".csv", ".map", ".pot")
EXAMPLE_SUFFIXES = (".pov", ".inc", ".ini", ".txt", ".mcr")
MAX_SCAN_BYTES = 256 * 1024
MAX_DECLARATIONS_PER_FILE = 3_000
SNIPPET_BEFORE = 100
SNIPPET_AFTER = 260
BM25_K1 = 1.2
BM25_B = 0.75


class ManualParser(HTMLParser):
    """Split one POV-Ray manual page into numbered sections, skipping navigation panels."""

    HEADINGS = {"h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    BLOCKS = {"p", "div", "li", "tr", "dd", "dt", "table", "ul", "ol", "dl", "blockquote", "hr"}
    IGNORED = {"script", "style", "head", "title"}

    def __init__(self, file_name: str):
        super().__init__(convert_charrefs=True)
        self.file_name = file_name
        self.sections: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.heading: list[str] | None = None
        self.heading_level = 0
        self.divs: list[bool] = []
        self.skipping = 0
        self.ignoring = 0
        self.preformatted = 0

    def _emit(self, text: str) -> None:
        if self.current is not None:
            self.current["parts"].append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            navigation = "NavPanel" in (dict(attrs).get("class") or "")
            self.divs.append(navigation)
            self.skipping += navigation
        if tag in self.IGNORED:
            self.ignoring += 1
        if self.skipping or self.ignoring:
            return
        if tag in self.HEADINGS:
            self.heading = []
            self.heading_level = self.HEADINGS[tag]
        elif tag == "pre":
            self.preformatted += 1
            self._emit("\n")
        elif tag == "br":
            self._emit("\n")
        elif tag in ("td", "th"):
            self._emit("  ")
        elif tag in self.BLOCKS:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.divs:
            self.skipping -= self.divs.pop()
        if tag in self.IGNORED and self.ignoring:
            self.ignoring -= 1
        if self.skipping or self.ignoring:
            return
        if tag in self.HEADINGS and self.heading is not None:
            self._start_section(" ".join("".join(self.heading).split()))
            self.heading = None
        elif tag == "pre" and self.preformatted:
            self.preformatted -= 1
            self._emit("\n")
        elif tag in self.BLOCKS:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self.skipping or self.ignoring:
            return
        if self.heading is not None:
            self.heading.append(data)
        elif self.preformatted:
            self._emit(data)
        else:
            self._emit(re.sub(r"\s+", " ", data))

    def _start_section(self, heading: str) -> None:
        match = HEADING_NUMBER.match(heading)
        if not match:
            if self.current is not None:
                self._emit(f"\n{heading}\n")
            return
        section_id, title = match.group(1), match.group(2).strip()
        prefix = re.sub(r"[^a-z]", "", os.path.splitext(self.file_name)[0].split("_")[0]) or "r"
        self.current = {
            "id": section_id,
            "title": title,
            "path": [],
            "file": self.file_name,
            "url": f"{ONLINE_DOCS}{self.file_name}#{prefix}{section_id.replace('.', '_')}",
            "parts": [],
        }
        self.sections.append(self.current)

    def finish(self) -> list[dict[str, Any]]:
        self.close()
        for section in self.sections:
            text = "".join(section.pop("parts"))
            lines = [line.rstrip() for line in text.split("\n")]
            section["text"] = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return self.sections


def _read_text(path: str, limit: int | None = None) -> str:
    with open(path, "rb") as handle:
        payload = handle.read() if limit is None else handle.read(limit)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("cp1252", errors="replace")


def _manual_order(name: str) -> tuple[int, int, str]:
    order = {"u": 0, "t": 1, "r": 2}
    match = re.match(r"^([a-z])(\d+)_(\d+)", name)
    if not match:
        return (9, 0, name)
    return (order.get(match.group(1), 8), int(match.group(3)), name)


def build_docs(docs_dir: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    if not os.path.isdir(docs_dir):
        return sections
    for name in sorted((entry for entry in os.listdir(docs_dir) if entry.endswith(".html") and entry != "index.html"), key=_manual_order):
        parser = ManualParser(name)
        with open(os.path.join(docs_dir, name), "rb") as handle:
            parser.feed(handle.read().decode("cp1252", errors="replace"))
        sections += [section for section in parser.finish() if section["text"] or section["title"]]
    # Chapters span several HTML files, so breadcrumbs come from the section numbers, not one file's headings.
    titles = {section["id"]: section["title"] for section in sections}
    for section in sections:
        parts = section["id"].split(".")
        ancestors = (".".join(parts[:length]) for length in range(1, len(parts)))
        section["path"] = [titles[ancestor] for ancestor in ancestors if ancestor in titles]
    return sections


def _header_comment(text: str) -> str:
    lines: list[str] = []
    in_block = False
    for raw in text.splitlines()[:60]:
        line = raw.strip()
        if in_block:
            if "*/" in line:
                lines.append(line.split("*/", 1)[0])
                in_block = False
                continue
            lines.append(line.lstrip("*"))
            continue
        if line.startswith("//"):
            lines.append(line[2:])
        elif line.startswith("/*"):
            in_block = "*/" not in line
            lines.append(line[2:].split("*/", 1)[0])
        elif line == "":
            if lines:
                lines.append("")
        else:
            break
    noise = re.compile(
        r"(Persistence Of Vision|sample file|licensed under|creativecommons|Creative Commons|Castro Street|Mountain View|94041|"
        r"copy of this license|send a letter|^\s*USA\.?\s*$|^[\s=*/-]*$|^\s*-+\s*$)",
        re.IGNORECASE,
    )
    kept = [line.strip(" -=*\t") for line in lines if not noise.search(line)]
    summary = " ".join(" ".join(kept).split())
    return summary[:400]


def _declarations(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    previous_comment = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        match = DECLARATION.match(raw)
        if match and not match.group(2).endswith("_Inc_Temp"):
            kind, name, rest = match.group(1), match.group(2), match.group(3)
            comment = ""
            if "//" in rest:
                rest, comment = rest.split("//", 1)
            entry: dict[str, Any] = {"name": name, "kind": kind, "line": number}
            if kind == "macro":
                entry["signature"] = f"{name}{rest.strip()}"[:160]
            else:
                value = rest.strip().lstrip("=").strip()
                entry["value"] = value[:120]
                word = re.match(r"[A-Za-z_][A-Za-z0-9_]*", value)
                if word:
                    entry["type"] = word.group(0)
            note = (comment or previous_comment).strip()
            if note:
                entry["comment"] = note[:160]
            found.append(entry)
            if len(found) >= MAX_DECLARATIONS_PER_FILE:
                break
        previous_comment = stripped[2:].strip() if stripped.startswith("//") else ""
    return found


def build_examples(examples_dir: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not os.path.isdir(examples_dir):
        return entries
    for current, directories, files in os.walk(examples_dir):
        directories.sort()
        for name in sorted(files):
            if not name.lower().endswith(EXAMPLE_SUFFIXES):
                continue
            path = os.path.join(current, name)
            relative = os.path.relpath(path, examples_dir).replace(os.sep, "/")
            entries.append({
                "path": f"examples/{relative}",
                "kind": "example",
                "bytes": os.path.getsize(path),
                "summary": _header_comment(_read_text(path, 16_384)),
            })
    return entries


def build_includes(include_dir: str, prefix: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not os.path.isdir(include_dir):
        return entries
    for name in sorted(os.listdir(include_dir)):
        path = os.path.join(include_dir, name)
        if not name.endswith(".inc") or not os.path.isfile(path):
            continue
        text = _read_text(path)
        entries.append({
            "path": f"{prefix}/{name}",
            "kind": "include",
            "bytes": os.path.getsize(path),
            "summary": _header_comment(text),
            "declarations": _declarations(text),
        })
    return entries


def build_index(library_dir: str, include_dir: str = POV_INCLUDE) -> dict[str, Any]:
    assets_dir = os.path.join(library_dir, "assets")
    assets: list[dict[str, Any]] = []
    packs: dict[str, Any] = {}
    manifest_path = os.path.join(assets_dir, "manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        packs = manifest.get("packs", {})
        assets = manifest.get("files", [])
    includes = build_includes(include_dir, "include") + build_includes(assets_dir, "assets")
    if os.path.isdir(assets_dir):
        for pack in sorted(os.listdir(assets_dir)):
            if os.path.isdir(os.path.join(assets_dir, pack)):
                includes += build_includes(os.path.join(assets_dir, pack), f"assets/{pack}")
    return {
        "version": INDEX_VERSION,
        "docs": build_docs(os.path.join(library_dir, "docs")),
        "examples": build_examples(os.path.join(library_dir, "examples")),
        "includes": includes,
        "assets": assets,
        "packs": packs,
    }


def _tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def _snippet(text: str, terms: list[str]) -> str:
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - SNIPPET_BEFORE) if positions else 0
    excerpt = " ".join(text[start : start + SNIPPET_BEFORE + SNIPPET_AFTER].split())
    return ("..." if start else "") + excerpt + ("..." if start + SNIPPET_BEFORE + SNIPPET_AFTER < len(text) else "")


def _slice(text: str, offset: int, maximum: int) -> dict[str, Any]:
    chunk = text[offset : offset + maximum]
    end = offset + len(chunk)
    return {"text": chunk, "offset": offset, "characters": len(chunk), "totalCharacters": len(text), "nextOffset": end if end < len(text) else None}


class Library:
    def __init__(self, library_dir: str, include_dir: str = POV_INCLUDE):
        self.library_dir = os.path.realpath(library_dir)
        self.include_dir = os.path.realpath(include_dir)
        with open(os.path.join(self.library_dir, "index.json"), "r", encoding="utf-8") as handle:
            index = json.load(handle)
        if index.get("version") != INDEX_VERSION:
            raise ValueError("the library index has an unsupported version")
        self.docs: list[dict[str, Any]] = index["docs"]
        self.examples: list[dict[str, Any]] = index["examples"]
        self.includes: list[dict[str, Any]] = index["includes"]
        self.assets: list[dict[str, Any]] = index["assets"]
        self.packs: dict[str, Any] = index["packs"]
        self.by_section = {section["id"]: section for section in self.docs}
        self._doc_terms = [Counter(_tokens(section["text"])) for section in self.docs]
        self._doc_titles = [set(_tokens(" ".join([*section["path"], section["title"]]))) for section in self.docs]
        self._doc_lengths = [sum(terms.values()) for terms in self._doc_terms]
        self._average_length = (sum(self._doc_lengths) / len(self._doc_lengths)) if self._doc_lengths else 1.0
        frequency: Counter[str] = Counter()
        for terms in self._doc_terms:
            frequency.update(terms.keys())
        self._document_frequency = frequency
        self._content_cache: dict[str, str] = {}

    def summary(self) -> dict[str, Any]:
        return {
            "docs": {"sections": len(self.docs), "source": "Debian povray-doc 3.7 HTML manual (user guide 1.x, tutorial 2.x, reference 3.x)"},
            "examples": len(self.examples),
            "includes": {"files": len(self.includes), "declarations": sum(len(entry["declarations"]) for entry in self.includes)},
            "assets": {"files": len(self.assets), "packs": self.packs},
        }

    def docs_search(self, query: str, limit: int) -> dict[str, Any]:
        terms = list(dict.fromkeys(_tokens(query)))
        if not terms:
            raise ValueError("query must contain letters or digits")
        total = len(self.docs)
        phrase = " ".join(query.lower().split())
        scored = []
        for index, section in enumerate(self.docs):
            score = 0.0
            counts = self._doc_terms[index]
            for term in terms:
                frequency = counts.get(term, 0)
                document_frequency = self._document_frequency.get(term, 0)
                idf = math.log(1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))
                if frequency:
                    norm = BM25_K1 * (1 - BM25_B + BM25_B * self._doc_lengths[index] / self._average_length)
                    score += idf * frequency * (BM25_K1 + 1) / (frequency + norm)
                if term in self._doc_titles[index]:
                    score += 2.5 * idf
            if score and len(terms) > 1 and phrase in section["text"].lower():
                score *= 1.5
            if score:
                scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        results = []
        for score, index in scored[:limit]:
            section = self.docs[index]
            results.append({
                "sectionId": section["id"],
                "title": section["title"],
                "path": section["path"],
                "score": round(score, 3),
                "characters": len(section["text"]),
                "snippet": _snippet(section["text"], terms),
            })
        return {"query": query, "results": results, "next": "Read a result with operation read and its sectionId."}

    def docs_read(self, section_id: str, offset: int, maximum: int) -> dict[str, Any]:
        section = self.by_section.get(section_id)
        if section is None:
            raise ValueError(f"no manual section {section_id}; use operation toc or search")
        children = [entry["id"] for entry in self.docs if entry["id"].startswith(section_id + ".") and entry["id"].count(".") == section_id.count(".") + 1]
        return {
            "sectionId": section_id,
            "title": section["title"],
            "path": section["path"],
            "url": section["url"],
            "subsections": children,
            **_slice(section["text"], offset, maximum),
        }

    def docs_toc(self, section_id: str | None, depth: int) -> dict[str, Any]:
        if section_id is not None and section_id not in self.by_section:
            raise ValueError(f"no manual section {section_id}")
        if section_id is None:
            base = min((entry["id"].count(".") for entry in self.docs), default=0)
            selected = [entry for entry in self.docs if entry["id"].count(".") < base + depth]
        else:
            base = section_id.count(".")
            selected = [entry for entry in self.docs if entry["id"].startswith(section_id + ".") and entry["id"].count(".") <= base + depth]
        return {"sectionId": section_id, "entries": [{"sectionId": entry["id"], "title": entry["title"], "characters": len(entry["text"])} for entry in selected[:400]], "truncated": len(selected) > 400}

    def resolve(self, path: str) -> str:
        area, _, relative = path.partition("/")
        roots = {"examples": os.path.join(self.library_dir, "examples"), "include": self.include_dir, "assets": os.path.join(self.library_dir, "assets")}
        root = os.path.realpath(roots[area])
        target = os.path.realpath(os.path.join(root, *relative.split("/")))
        if os.path.commonpath((root, target)) != root or not os.path.isfile(target):
            raise ValueError(f"library file not found: {path}; use operation search")
        return target

    def _content(self, entry: dict[str, Any]) -> str:
        path = entry["path"]
        if path not in self._content_cache:
            try:
                self._content_cache[path] = _read_text(self.resolve(path), MAX_SCAN_BYTES).lower()
            except (OSError, ValueError):
                self._content_cache[path] = ""
        return self._content_cache[path]

    def search(self, query: str, kind: str | None, limit: int) -> dict[str, Any]:
        terms = list(dict.fromkeys(_tokens(query)))
        if not terms:
            raise ValueError("query must contain letters or digits")
        candidates: list[dict[str, Any]] = []
        if kind in (None, "example"):
            candidates += self.examples
        if kind in (None, "include"):
            candidates += self.includes
        if kind in (None, "asset"):
            candidates += [{**entry, "kind": "asset"} for entry in self.assets]
        scored = []
        for order, entry in enumerate(candidates):
            header = " ".join(str(entry.get(field, "")) for field in ("path", "summary", "title", "identifier", "pack", "description")).lower()
            names = " ".join(declaration["name"].lower() for declaration in entry.get("declarations", []))
            score = 0.0
            matched = 0
            for term in terms:
                hit = False
                if term in header:
                    score += 4
                    hit = True
                if names and term in names:
                    score += 3
                    hit = True
                if entry["kind"] != "asset":
                    count = self._content(entry).count(term)
                    if count:
                        score += 1 + math.log(count)
                        hit = True
                matched += hit
            if matched:
                scored.append((score * matched / len(terms), order, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        results = []
        for score, _, entry in scored[:limit]:
            result = {key: entry[key] for key in ("path", "kind", "bytes") if key in entry}
            for key in ("summary", "title", "identifier", "license", "page"):
                if entry.get(key):
                    result[key] = entry[key]
            if entry.get("declarations"):
                result["declarations"] = len(entry["declarations"])
            result["score"] = round(score, 3)
            results.append(result)
        return {"query": query, "kind": kind, "results": results}

    def read(self, path: str, offset: int, maximum: int) -> dict[str, Any]:
        target = self.resolve(path)
        size = os.path.getsize(target)
        with open(target, "rb") as handle:
            head = handle.read(8_192)
        if b"\x00" in head or not path.lower().endswith(TEXT_SUFFIXES):
            return {"path": path, "bytes": size, "binary": True, "usage": "Reference this file by bare name inside a scene; it is on the render library path."}
        return {"path": path, "bytes": size, **_slice(_read_text(target), offset, maximum)}

    def declarations(self, query: str | None, path: str | None, limit: int) -> dict[str, Any]:
        needle = (query or "").lower().strip()
        terms = _tokens(needle)
        if path and not any(entry["path"] == path for entry in self.includes):
            raise ValueError(f"{path} is not an indexed include file; declarations covers include/*.inc and the asset library's .inc files")
        matches = []
        for entry in self.includes:
            if path and entry["path"] != path:
                continue
            for declaration in entry["declarations"]:
                name = declaration["name"].lower()
                text = " ".join(str(declaration.get(field, "")) for field in ("name", "type", "comment", "value")).lower()
                if needle and needle not in name and not all(term in text for term in terms):
                    continue
                rank = 0 if needle and name == needle else 1 if needle and name.startswith(needle) else 2
                matches.append((rank, entry["path"], declaration))
        matches.sort(key=lambda item: (item[0], item[1], item[2]["line"]))
        results = [{"include": include_path.rsplit("/", 1)[1], "path": include_path, **declaration} for _, include_path, declaration in matches[:limit]]
        return {"query": query, "path": path, "results": results, "total": len(matches), "usage": "#include the file named in include, then use the identifier."}


def self_test() -> None:
    import tempfile

    page = (
        '<html><head><title>Reference</title></head><body><div class="NavPanel"><div><a href="#x">3.4.1 Global Settings</a></div></div>'
        "<h2>3.4 Scene File Elements</h2><p>Intro &amp; overview.</p>"
        "<h3>3.4.1 Camera</h3><p>The camera uses <code>look_at</code>.</p><pre>camera {\n  location &lt;0,1,-3&gt;\n  look_at 0\n}</pre>"
        "<h4>3.4.1.1 Placing the Camera</h4><p>Use location and look_at together.</p>"
        "<h3>3.4.2 Lights</h3><p>An area_light softens shadows.</p></body></html>"
    )
    include = "// Standard textures\n#declare Jade = texture { pigment { rgb <0, 0.6, 0.3> } } // green stone\n// Make a ring\n#macro Ring(R, T)\n  torus { R, T }\n#end\n"
    with tempfile.TemporaryDirectory() as root:
        library_dir = os.path.join(root, "library")
        include_dir = os.path.join(root, "include")
        for directory in ("docs", "examples/basic", "assets/textures"):
            os.makedirs(os.path.join(library_dir, directory))
        os.makedirs(include_dir)
        with open(os.path.join(library_dir, "docs", "r3_4.html"), "wb") as handle:
            handle.write(page.encode("cp1252"))
        with open(os.path.join(library_dir, "examples", "basic", "ring.pov"), "w", encoding="utf-8") as handle:
            handle.write("// Persistence Of Vision raytracer sample file.\n// A jade ring on a checkered plane.\n#include \"textures.inc\"\nobject { Ring(1, 0.2) texture { Jade } }\n")
        with open(os.path.join(include_dir, "textures.inc"), "w", encoding="utf-8") as handle:
            handle.write(include)
        with open(os.path.join(library_dir, "assets", "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"packs": {"textures": {"files": 1}}, "files": [{"path": "assets/textures/wood.jpg", "pack": "textures", "identifier": "Wood_Table", "title": "Wood table", "bytes": 3, "license": "CC0-1.0"}]}, handle)
        with open(os.path.join(library_dir, "assets", "textures", "wood.jpg"), "wb") as handle:
            handle.write(b"\xff\xd8\x00")
        index = build_index(library_dir, include_dir)
        with open(os.path.join(library_dir, "index.json"), "w", encoding="utf-8") as handle:
            json.dump(index, handle)
        library = Library(library_dir, include_dir)
        assert [section["id"] for section in library.docs] == ["3.4", "3.4.1", "3.4.1.1", "3.4.2"]
        assert "Global Settings" not in library.docs[0]["text"], "navigation panels must be skipped"
        assert "look_at 0\n}" in library.by_section["3.4.1"]["text"]
        assert library.by_section["3.4.1.1"]["path"] == ["Scene File Elements", "Camera"]
        assert library.by_section["3.4.1"]["url"].endswith("r3_4.html#r3_4_1")
        assert library.docs_search("area_light shadows", 3)["results"][0]["sectionId"] == "3.4.2"
        assert library.docs_read("3.4.1", 0, 500)["subsections"] == ["3.4.1.1"]
        assert [entry["sectionId"] for entry in library.docs_toc(None, 2)["entries"]] == ["3.4", "3.4.1", "3.4.2"]
        found = library.declarations("jade", None, 10)["results"][0]
        assert found["include"] == "textures.inc" and found["type"] == "texture" and found["comment"] == "green stone"
        assert library.declarations("ring", None, 10)["results"][0]["signature"] == "Ring(R, T)"
        assert library.search("jade ring", "example", 5)["results"][0]["path"] == "examples/basic/ring.pov"
        assert library.search("wood", "asset", 5)["results"][0]["identifier"] == "Wood_Table"
        assert library.read("include/textures.inc", 0, 500)["text"].startswith("// Standard textures")
        assert library.read("assets/textures/wood.jpg", 0, 500)["binary"] is True
        assert "A jade ring" in library.examples[0]["summary"] and "Persistence" not in library.examples[0]["summary"]
        for bad in ("include/../../etc/passwd", "examples/missing.pov"):
            try:
                library.read(bad, 0, 500)
            except ValueError:
                continue
            raise AssertionError(f"read accepted {bad}")


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--build":
        library_dir = sys.argv[2]
        index = build_index(library_dir)
        target = os.path.join(library_dir, "index.json")
        with open(target + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(target + ".tmp", target)
        Library(library_dir)
        print(json.dumps({"sections": len(index["docs"]), "examples": len(index["examples"]), "includes": len(index["includes"]), "assets": len(index["assets"])}))
    else:
        self_test()


if __name__ == "__main__":
    main()

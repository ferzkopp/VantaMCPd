#!/usr/bin/env python3
"""Wikipedia source adapter: the article title index of a Wikimedia project.

The upstream artefact is the `all-titles-in-ns0` dump, roughly 104 MB compressed for English
Wikipedia. It carries one main-namespace article title per line and no article text, which keeps the
ingested corpus small enough for a constrained arm64 node while still answering the questions an agent
actually needs a title index for: whether an article exists, what its exact canonical title is, and
which URL to hand to a retrieval tool.
"""
import gzip
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fetching import content_identity, download_file, emit_progress, iso_timestamp

BASE_URL = "https://dumps.wikimedia.org"
DUMP_MEMBER = "all-titles-in-ns0.gz"
TERMS_URL = "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use"
LICENSE = "CC BY-SA 4.0"
HEADER_LINE = "page_title"
WIKI_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,8})?wiki$")
MAX_TITLE_BYTES = 255
BATCH_RECORDS = 2000
PROGRESS_INTERVAL_LINES = 250_000


def dump_url(wiki: str) -> str:
    return f"{BASE_URL}/{wiki}/latest/{wiki}-latest-{DUMP_MEMBER}"


def site_host(wiki: str) -> str:
    return f"{wiki[:-4]}.wikipedia.org"


def article_url(wiki: str, title: str) -> str:
    return f"https://{site_host(wiki)}/wiki/{urllib.parse.quote(title, safe='_,()!$&*:;=@~')}"


def parse_title_line(line: bytes) -> str | None:
    """Return a main-namespace article title, or None for the header and unusable lines."""
    if not line or len(line) > MAX_TITLE_BYTES:
        return None
    title = line.decode("utf-8", "replace").strip()
    if not title or title == HEADER_LINE or "\ufffd" in title:
        return None
    return title


def title_record(wiki: str, title: str, cutoff: str, source_query: str, fetched_at: str) -> dict[str, Any]:
    readable = title.replace("_", " ")
    return {
        "id": f"wikipedia:{title}",
        "source": "wikipedia",
        "license": LICENSE,
        "title": readable,
        "abstract": "",
        "authors_json": "[]",
        "authors_search": "",
        "categories_json": "[]",
        "categories_search": "||",
        "primary_category": None,
        "published": cutoff,
        "updated": cutoff,
        "doi": None,
        "journal_ref": None,
        "comment": None,
        "abstract_url": article_url(wiki, title),
        "pdf_url": None,
        "source_query": source_query,
        "profile_slice": "title-index",
        "fetched_at": fetched_at,
    }


class WikipediaSource:
    """Main-namespace article titles from a Wikimedia project dump, without article text."""

    key = "wikipedia-title-index"
    id = "wikipedia"
    prefix = "wikipedia"
    name = "Wikipedia main-namespace title index"
    license = LICENSE
    terms_url = TERMS_URL
    catchup_source_url = None
    subject_scheme = None
    supports_topics = False
    supports_catch_up = False

    @property
    def source_url(self) -> str:
        return dump_url("enwiki")

    def validate(self, spec: dict[str, Any]) -> None:
        if spec.get("samplePercent") not in (1, 25, 100):
            raise ValueError("wikipedia samplePercent must be 1, 25, or 100")
        if not isinstance(spec.get("sampleSeed"), str) or not spec["sampleSeed"]:
            raise ValueError("wikipedia sampleSeed must be a non-empty string")
        wiki = spec.get("wiki", "enwiki")
        if not isinstance(wiki, str) or not WIKI_PATTERN.fullmatch(wiki):
            raise ValueError("wikipedia wiki must name a Wikimedia project such as enwiki")
        if spec.get("topics"):
            raise ValueError("wikipedia does not accept topics; the title index has no subject scheme")

    def identity_fields(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": self.key,
            "wiki": spec.get("wiki", "enwiki"),
            "samplePercent": spec["samplePercent"],
            "sampleSeed": spec["sampleSeed"],
        }

    def acquire(self, data_dir: Path, spec: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        wiki = spec.get("wiki", "enwiki")
        local = overrides.get("dump_path")
        if local is not None:
            dump = Path(local)
            stat = dump.stat()
            metadata = {"etag": None, "lastModified": None, "bytes": stat.st_size}
            cutoff = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
            identity = f"local:{stat.st_size}:{int(stat.st_mtime)}"
        else:
            dump = data_dir / "source" / f"{wiki}-latest-{DUMP_MEMBER}"
            metadata = download_file(dump, dump_url(wiki), f"Downloading the {wiki} article title index.", overrides.get("opener", urllib.request.urlopen))
            cutoff = iso_timestamp(metadata.get("lastModified"))
            identity = content_identity(metadata)
        if not cutoff:
            cutoff = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {"identity": identity, "dump": dump, "cutoff": cutoff, "bytes": metadata.get("bytes") or dump.stat().st_size}

    def ingest(self, connection: sqlite3.Connection, spec: dict[str, Any], handle: dict[str, Any], state: dict[str, Any], save: Callable[[], None], fetched_at: str, upsert, sampled) -> tuple[int, str]:
        wiki = spec.get("wiki", "enwiki")
        cutoff = handle["cutoff"]
        source_query = f"titles:{wiki}:ns0;sample={spec['samplePercent']}%"
        skip = int(state.get("lines", 0))
        records = 0 if skip == 0 else int(state.get("records", 0))
        seen = 0
        batch: list[dict[str, Any]] = []
        with gzip.open(handle["dump"], "rb") as source:
            for raw in source:
                seen += 1
                if seen <= skip:
                    continue
                title = parse_title_line(raw.rstrip(b"\r\n"))
                if title is not None and sampled(title, spec["samplePercent"], spec["sampleSeed"]):
                    batch.append(title_record(wiki, title, cutoff, source_query, fetched_at))
                if len(batch) >= BATCH_RECORDS:
                    upsert(connection, batch)
                    records += len(batch)
                    batch.clear()
                    connection.commit()
                    state.update({"lines": seen, "records": records, "cutoff": cutoff})
                    save()
                if seen % PROGRESS_INTERVAL_LINES == 0:
                    emit_progress("ingest", seen, 0, f"Indexed {records} {wiki} article titles.", "titles")
            if batch:
                upsert(connection, batch)
                records += len(batch)
                connection.commit()
        state.update({"lines": seen, "records": records, "cutoff": cutoff})
        save()
        return records, cutoff

    def catch_up(self, *arguments: Any, **keywords: Any) -> int:
        raise ValueError("wikipedia title dumps have no incremental feed; reinstall to adopt a newer dump")

    def accepts(self, value: str) -> bool:
        if value.startswith("wikipedia:"):
            return bool(value[len("wikipedia:"):].strip())
        return "/wiki/" in value and ".wikipedia.org" in value

    def normalize(self, value: str) -> str:
        if value.startswith("wikipedia:"):
            title = value[len("wikipedia:"):]
        else:
            title = urllib.parse.unquote(value.split("/wiki/", 1)[1].split("#", 1)[0].split("?", 1)[0])
        title = title.strip().replace(" ", "_")
        if not title or len(title.encode("utf-8")) > MAX_TITLE_BYTES:
            raise ValueError("id must be a Wikipedia article title")
        return f"wikipedia:{title}"

    def describe(self, subject: str) -> tuple[str | None, str | None]:
        return None, None

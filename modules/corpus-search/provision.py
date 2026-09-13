#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from corpus import SCHEMA_VERSION, connect, initialize, put_metadata, rebuild_search, upsert_papers, verify

API_URL = "https://export.arxiv.org/api/query"
TERMS_URL = "https://info.arxiv.org/help/api/tou.html"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_API_RESULTS = 30_000
MIN_REQUEST_INTERVAL_SECONDS = 3.0
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def emit_progress(phase: str, current: int, total: int, message: str) -> None:
    print("VANTA_PROGRESS " + json.dumps({"phase": phase, "current": current, "total": total, "unit": "records", "message": message}), flush=True)


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def versionless_id(url: str) -> str:
    identifier = url.rsplit("/abs/", 1)[-1]
    return re.sub(r"v\d+$", "", identifier)


def parse_feed(payload: bytes, source_query: str, slice_id: str, fetched_at: str) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        identifier = versionless_id(clean_text(entry.findtext(f"{ATOM}id")))
        if not re.fullmatch(r"(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})", identifier, re.IGNORECASE):
            continue
        authors = [clean_text(author.findtext(f"{ATOM}name")) for author in entry.findall(f"{ATOM}author")]
        authors = [author for author in authors if author]
        categories = [element.attrib.get("term", "") for element in entry.findall(f"{ATOM}category")]
        categories = [category for category in categories if category]
        links = entry.findall(f"{ATOM}link")
        abstract_url = next((link.attrib.get("href") for link in links if link.attrib.get("rel") == "alternate"), f"https://arxiv.org/abs/{identifier}")
        pdf_url = next((link.attrib.get("href") for link in links if link.attrib.get("title") == "pdf"), None)
        primary = entry.find(f"{ARXIV}primary_category")
        paper = {
            "id": identifier,
            "title": clean_text(entry.findtext(f"{ATOM}title")),
            "abstract": clean_text(entry.findtext(f"{ATOM}summary")),
            "authors_json": json.dumps(authors, ensure_ascii=False),
            "authors_search": " ".join(authors),
            "categories_json": json.dumps(categories),
            "categories_search": "|" + "|".join(categories) + "|",
            "primary_category": primary.attrib.get("term") if primary is not None else (categories[0] if categories else None),
            "published": clean_text(entry.findtext(f"{ATOM}published")),
            "updated": clean_text(entry.findtext(f"{ATOM}updated")),
            "doi": clean_text(entry.findtext(f"{ARXIV}doi")) or None,
            "journal_ref": clean_text(entry.findtext(f"{ARXIV}journal_ref")) or None,
            "comment": clean_text(entry.findtext(f"{ARXIV}comment")) or None,
            "abstract_url": abstract_url,
            "pdf_url": pdf_url,
            "source_query": source_query,
            "profile_slice": slice_id,
            "fetched_at": fetched_at,
        }
        papers.append(paper)
    return papers


class ArxivClient:
    def __init__(self, opener: Callable[..., Any] = urllib.request.urlopen, sleeper: Callable[[float], None] = time.sleep):
        self.opener = opener
        self.sleeper = sleeper
        self.last_request = 0.0

    def fetch(self, query: str, start: int, page_size: int) -> bytes:
        parameters = urllib.parse.urlencode({
            "search_query": query,
            "start": start,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        })
        request = urllib.request.Request(
            f"{API_URL}?{parameters}",
            headers={"User-Agent": os.environ.get("VANTA_ARXIV_USER_AGENT", "VantaMCPd/0.1 corpus-search local metadata index")},
        )
        for attempt in range(4):
            wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self.last_request)
            if wait > 0:
                self.sleeper(wait)
            self.last_request = time.monotonic()
            try:
                with self.opener(request, timeout=60) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("arXiv API response exceeds 8 MiB")
                return payload
            except Exception:
                if attempt == 3:
                    raise
                self.sleeper(MIN_REQUEST_INTERVAL_SECONDS * (attempt + 1))
        raise AssertionError("unreachable")


def load_profile(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    profile = json.loads(raw)
    if profile.get("schemaVersion") != 1 or profile.get("source") != "arxiv-api":
        raise ValueError("unsupported corpus profile")
    page_size = profile.get("pageSize")
    maximum = profile.get("maxRecords")
    slices = profile.get("slices")
    if not isinstance(page_size, int) or not 1 <= page_size <= 500:
        raise ValueError("profile pageSize must be from 1 to 500")
    if not isinstance(maximum, int) or not 1 <= maximum <= 100_000:
        raise ValueError("profile maxRecords must be from 1 to 100000")
    if not isinstance(slices, list) or not slices or sum(item.get("maxRecords", 0) for item in slices) != maximum:
        raise ValueError("profile slices must exactly fill maxRecords")
    for item in slices:
        if not isinstance(item.get("id"), str) or not isinstance(item.get("maxRecords"), int):
            raise ValueError("invalid profile slice")
        categories = item.get("categories")
        if not isinstance(categories, list) or not categories or not all(re.fullmatch(r"[A-Za-z0-9.-]+", value or "") for value in categories):
            raise ValueError("invalid profile categories")
    return profile, hashlib.sha256(raw).hexdigest()


def active_matches(database: Path, profile_hash: str) -> bool:
    if not database.exists():
        return False
    try:
        with connect(database, readonly=True) as connection:
            values = dict(connection.execute("SELECT key, value FROM metadata"))
            return values.get("schema_version") == SCHEMA_VERSION and values.get("profile_hash") == profile_hash
    except (sqlite3.Error, OSError):
        return False


def provision(data_dir: Path, profile_path: Path, client: ArxivClient | None = None, cutoff: str | None = None) -> dict[str, Any]:
    profile, profile_hash = load_profile(profile_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    active = data_dir / "corpus.db"
    working = data_dir / "corpus.next.db"
    checkpoint_path = data_dir / "checkpoint.json"
    if active_matches(active, profile_hash):
        with connect(active, readonly=True) as connection:
            records = int(connection.execute("SELECT count(*) FROM papers").fetchone()[0])
        emit_progress("complete", records, profile["maxRecords"], "Existing corpus matches the requested profile.")
        return {"records": records, "reused": True}

    checkpoint = None
    if checkpoint_path.exists():
        try:
            candidate = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if candidate.get("profileHash") == profile_hash and (cutoff is None or candidate.get("cutoff") == cutoff):
                checkpoint = candidate
                cutoff = candidate.get("cutoff")
        except (OSError, ValueError):
            checkpoint = None
    cutoff = cutoff or datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    if checkpoint is None:
        working.unlink(missing_ok=True)
        checkpoint = {
            "schemaVersion": 1,
            "profileHash": profile_hash,
            "cutoff": cutoff,
            "slices": {item["id"]: {"offset": 0, "records": 0} for item in profile["slices"]},
        }
        atomic_json(checkpoint_path, checkpoint)

    client = client or ArxivClient()
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with connect(working) as connection:
        initialize(connection)
        for item in profile["slices"]:
            progress = checkpoint["slices"][item["id"]]
            query_categories = " OR ".join(f"cat:{category}" for category in item["categories"])
            query = f"({query_categories}) AND submittedDate:[199101010000 TO {cutoff}]"
            while progress["records"] < item["maxRecords"] and progress["offset"] < MAX_API_RESULTS:
                payload = client.fetch(query, progress["offset"], profile["pageSize"])
                papers = parse_feed(payload, query, item["id"], fetched_at)
                if not papers:
                    break
                remaining = item["maxRecords"] - progress["records"]
                for paper in papers:
                    if remaining <= 0:
                        break
                    exists = connection.execute("SELECT 1 FROM papers WHERE id = ?", (paper["id"],)).fetchone()
                    if exists is not None:
                        continue
                    upsert_papers(connection, [paper])
                    progress["records"] += 1
                    remaining -= 1
                progress["offset"] += profile["pageSize"]
                connection.commit()
                atomic_json(checkpoint_path, checkpoint)
                total = int(connection.execute("SELECT count(*) FROM papers").fetchone()[0])
                emit_progress("download", total, profile["maxRecords"], f"Indexed arXiv page for {item['id']}.")
                if len(papers) < profile["pageSize"]:
                    break

        rebuild_search(connection)
        put_metadata(connection, {
            "schema_version": SCHEMA_VERSION,
            "profile_id": profile["id"],
            "profile_hash": profile_hash,
            "source": "arXiv descriptive metadata",
            "source_url": API_URL,
            "source_terms_url": TERMS_URL,
            "cutoff": cutoff,
            "refreshed_at": fetched_at,
        })
        connection.execute("ANALYZE")
        connection.commit()
        count = verify(connection, profile["maxRecords"])
        if count == 0:
            raise ValueError("arXiv profile produced an empty corpus")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
    connection.close()

    previous = data_dir / "corpus.previous.db"
    previous.unlink(missing_ok=True)
    if active.exists():
        os.replace(active, previous)
    os.replace(working, active)
    for suffix in ("-wal", "-shm"):
        Path(str(working) + suffix).unlink(missing_ok=True)
    os.chmod(active, 0o644)
    checkpoint_path.unlink(missing_ok=True)
    emit_progress("complete", count, profile["maxRecords"], "Corpus database activated.")
    return {"records": count, "reused": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--cutoff", help="Fixed UTC cutoff in YYYYMMDDHHMM format; primarily for deterministic recovery/tests.")
    arguments = parser.parse_args()
    result = provision(arguments.data_dir, arguments.profile, cutoff=arguments.cutoff)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
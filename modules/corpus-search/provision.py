#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from corpus import SCHEMA_VERSION, connect, count_categories, initialize, put_metadata, rebuild_search, upsert_papers, verify

API_URL = "https://export.arxiv.org/api/query"
OAI_URL = "https://oaipmh.arxiv.org/oai"
SNAPSHOT_URL = "https://www.kaggle.com/api/v1/datasets/download/Cornell-University/arxiv"
SNAPSHOT_MEMBER = "arxiv-metadata-oai-snapshot.json"
TERMS_URL = "https://info.arxiv.org/help/api/tou.html"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_REQUEST_INTERVAL_SECONDS = 3.0
MAX_REQUEST_ATTEMPTS = 6
MAX_RETRY_DELAY_SECONDS = 300.0
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
OAI_ARXIV = "{http://arxiv.org/OAI/arXiv/}"
PHYSICS_ARCHIVES = {
    "astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th", "math-ph",
    "nlin", "nucl-ex", "nucl-th", "physics", "quant-ph",
}


def emit_progress(phase: str, current: int, total: int, message: str, unit: str = "records") -> None:
    print("VANTA_PROGRESS " + json.dumps({"phase": phase, "current": current, "total": total, "unit": unit, "message": message}), flush=True)


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


def parse_oai_feed(payload: bytes, source_query: str, slice_id: str, fetched_at: str) -> tuple[list[dict[str, Any]], str | None]:
    root = ET.fromstring(payload)
    papers = []
    for record in root.findall(f".//{OAI}record"):
        header = record.find(f"{OAI}header")
        metadata = record.find(f"{OAI}metadata/{OAI_ARXIV}arXiv")
        if header is None or metadata is None or header.attrib.get("status") == "deleted":
            continue
        identifier = clean_text(metadata.findtext(f"{OAI_ARXIV}id"))
        if not re.fullmatch(r"(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})", identifier, re.IGNORECASE):
            continue
        authors = []
        for author in metadata.findall(f"{OAI_ARXIV}authors/{OAI_ARXIV}author"):
            name = " ".join(filter(None, [
                clean_text(author.findtext(f"{OAI_ARXIV}forenames")),
                clean_text(author.findtext(f"{OAI_ARXIV}keyname")),
                clean_text(author.findtext(f"{OAI_ARXIV}suffix")),
            ]))
            if name:
                authors.append(name)
        categories = clean_text(metadata.findtext(f"{OAI_ARXIV}categories")).split()
        created = clean_text(metadata.findtext(f"{OAI_ARXIV}created"))
        updated = clean_text(metadata.findtext(f"{OAI_ARXIV}updated")) or created
        papers.append({
            "id": identifier,
            "title": clean_text(metadata.findtext(f"{OAI_ARXIV}title")),
            "abstract": clean_text(metadata.findtext(f"{OAI_ARXIV}abstract")),
            "authors_json": json.dumps(authors, ensure_ascii=False),
            "authors_search": " ".join(authors),
            "categories_json": json.dumps(categories),
            "categories_search": "|" + "|".join(categories) + "|",
            "primary_category": categories[0] if categories else None,
            "published": f"{created}T00:00:00Z" if created else "",
            "updated": f"{updated}T00:00:00Z" if updated else "",
            "doi": clean_text(metadata.findtext(f"{OAI_ARXIV}doi")) or None,
            "journal_ref": clean_text(metadata.findtext(f"{OAI_ARXIV}journal-ref")) or None,
            "comment": clean_text(metadata.findtext(f"{OAI_ARXIV}comments")) or None,
            "abstract_url": f"https://arxiv.org/abs/{identifier}",
            "pdf_url": f"https://arxiv.org/pdf/{identifier}",
            "source_query": source_query,
            "profile_slice": slice_id,
            "fetched_at": fetched_at,
        })
    token = clean_text(root.findtext(f".//{OAI}resumptionToken")) or None
    return papers, token


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
        for attempt in range(MAX_REQUEST_ATTEMPTS):
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
            except Exception as error:
                if isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code != 429:
                    raise
                if attempt == MAX_REQUEST_ATTEMPTS - 1:
                    raise
                retry_after = None
                if isinstance(error, urllib.error.HTTPError):
                    value = error.headers.get("Retry-After")
                    try:
                        retry_after = float(value) if value is not None else None
                    except ValueError:
                        retry_after = None
                if isinstance(error, urllib.error.HTTPError) and (error.code == 429 or error.code >= 500):
                    delay = retry_after if retry_after is not None else 30.0 * (2 ** attempt)
                else:
                    delay = MIN_REQUEST_INTERVAL_SECONDS * (attempt + 1)
                self.sleeper(min(max(delay, MIN_REQUEST_INTERVAL_SECONDS), MAX_RETRY_DELAY_SECONDS))
        raise AssertionError("unreachable")


def oai_set_spec(category: str) -> str:
    archive, separator, subject = category.partition(".")
    group = "physics" if archive in PHYSICS_ARCHIVES else archive
    return f"{group}:{archive}:{subject}" if separator else f"{group}:{archive}"


class OaiClient(ArxivClient):
    def fetch(self, category: str, from_date: str, until_date: str, token: str | None = None) -> bytes:
        parameters = {"verb": "ListRecords", "resumptionToken": token} if token else {
            "verb": "ListRecords",
            "metadataPrefix": "arXiv",
            "set": oai_set_spec(category),
            "from": from_date,
            "until": until_date,
        }
        request = urllib.request.Request(
            f"{OAI_URL}?{urllib.parse.urlencode(parameters)}",
            headers={"User-Agent": os.environ.get("VANTA_ARXIV_USER_AGENT", "VantaMCPd/0.1 corpus-search local metadata index")},
        )
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self.last_request)
            if wait > 0:
                self.sleeper(wait)
            self.last_request = time.monotonic()
            try:
                with self.opener(request, timeout=90) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("arXiv OAI response exceeds 8 MiB")
                return payload
            except Exception as error:
                if isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code not in (429, 503):
                    raise
                if attempt == MAX_REQUEST_ATTEMPTS - 1:
                    raise
                value = error.headers.get("Retry-After") if isinstance(error, urllib.error.HTTPError) else None
                try:
                    retry_after = float(value) if value is not None else None
                except ValueError:
                    retry_after = None
                delay = retry_after if retry_after is not None else 30.0 * (2 ** attempt)
                self.sleeper(min(max(delay, MIN_REQUEST_INTERVAL_SECONDS), MAX_RETRY_DELAY_SECONDS))
        raise AssertionError("unreachable")


def load_profile(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    profile = json.loads(raw)
    if profile.get("schemaVersion") != 2 or profile.get("source") != "arxiv-bulk-snapshot":
        raise ValueError("unsupported corpus profile")
    sample_percent = profile.get("samplePercent")
    if sample_percent not in (1, 25, 100):
        raise ValueError("profile samplePercent must be 1, 25, or 100")
    if not isinstance(profile.get("sampleSeed"), str) or not profile["sampleSeed"]:
        raise ValueError("profile sampleSeed must be a non-empty string")
    topics = profile.get("topics")
    if not isinstance(topics, list) or not topics or not all(re.fullmatch(r"[A-Za-z0-9.-]+", value or "") for value in topics):
        raise ValueError("profile topics must contain valid arXiv categories")
    return profile, hashlib.sha256(raw).hexdigest()


def resolve_profile(profile_dir: Path, profile_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", profile_id):
        raise ValueError("profileId must be a lowercase kebab-case identifier")
    matches = []
    for candidate in profile_dir.glob("*.json"):
        try:
            if json.loads(candidate.read_text(encoding="utf-8")).get("id") == profile_id:
                matches.append(candidate)
        except (OSError, ValueError):
            continue
    if len(matches) != 1:
        raise ValueError(f"unknown or ambiguous corpus profile: {profile_id}")
    return matches[0]


def configure_profile(
    profile: dict[str, Any],
    base_profile_hash: str,
    categories: list[str] | None = None,
) -> tuple[dict[str, Any], str, str]:
    configured = dict(profile)
    if categories is not None:
        if not isinstance(categories, list) or not 1 <= len(categories) <= 50:
            raise ValueError("categories must contain from 1 to 50 arXiv categories")
        if not all(isinstance(category, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,40}", category) for category in categories):
            raise ValueError("categories contains an invalid arXiv category")
        configured["topics"] = list(dict.fromkeys(categories))

    content = {
        "baseProfileHash": base_profile_hash,
        "source": configured["source"],
        "samplePercent": configured["samplePercent"],
        "sampleSeed": configured["sampleSeed"],
        "topics": configured["topics"],
    }
    content_hash = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if categories is None:
        profile_hash = base_profile_hash
    else:
        profile_hash = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return configured, profile_hash, content_hash


def sampled(identifier: str, sample_percent: int, seed: str) -> bool:
    if sample_percent == 100:
        return True
    value = int.from_bytes(hashlib.sha256(f"{seed}\0{identifier}".encode()).digest()[:8], "big")
    return value < (1 << 64) * sample_percent // 100


def download_snapshot(data_dir: Path, opener: Callable[..., Any] = urllib.request.urlopen) -> Path:
    source_dir = data_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    destination = source_dir / "arxiv-metadata-oai-snapshot.zip"
    partial = destination.with_suffix(".zip.part")
    metadata_path = destination.with_suffix(".zip.http.json")
    metadata = {}
    if destination.exists() and metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
    headers = {"User-Agent": os.environ.get("VANTA_ARXIV_USER_AGENT", "VantaMCPd/0.1 corpus-search local metadata index")}
    if destination.exists():
        if metadata.get("etag"):
            headers["If-None-Match"] = metadata["etag"]
        if metadata.get("lastModified"):
            headers["If-Modified-Since"] = metadata["lastModified"]
    elif partial.exists() and partial.stat().st_size:
        headers["Range"] = f"bytes={partial.stat().st_size}-"
    request = urllib.request.Request(os.environ.get("VANTA_ARXIV_SNAPSHOT_URL", SNAPSHOT_URL), headers=headers)
    try:
        response = opener(request, timeout=300)
    except urllib.error.HTTPError as error:
        if error.code == 304 and destination.exists():
            return destination
        raise
    with response:
        status = getattr(response, "status", response.getcode())
        append = status == 206 and partial.exists()
        if not append:
            partial.unlink(missing_ok=True)
        downloaded = partial.stat().st_size if append else 0
        content_length = int(response.headers.get("Content-Length", "0") or 0)
        total = downloaded + content_length
        with partial.open("ab" if append else "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if downloaded % (64 * 1024 * 1024) < len(chunk):
                    emit_progress("download", downloaded, total, "Downloading the arXiv metadata snapshot ZIP.", "bytes")
        response_metadata = {
            "etag": response.headers.get("ETag"),
            "lastModified": response.headers.get("Last-Modified"),
            "url": response.geturl(),
        }
    os.replace(partial, destination)
    atomic_json(metadata_path, response_metadata)
    return destination


def extract_snapshot(data_dir: Path, archive: Path) -> tuple[Path, str]:
    destination = data_dir / "source" / SNAPSHOT_MEMBER
    destination.parent.mkdir(parents=True, exist_ok=True)
    state_path = destination.with_suffix(".json.extract.json")
    with zipfile.ZipFile(archive) as bundle:
        matches = [item for item in bundle.infolist() if Path(item.filename).name == SNAPSHOT_MEMBER and not item.is_dir()]
        if len(matches) != 1:
            raise ValueError(f"snapshot ZIP must contain exactly one {SNAPSHOT_MEMBER}")
        member = matches[0]
        identity = f"{member.CRC:08x}:{member.file_size}:{member.compress_size}"
        if destination.exists() and destination.stat().st_size == member.file_size and state_path.exists():
            try:
                if json.loads(state_path.read_text(encoding="utf-8")).get("identity") == identity:
                    return destination, identity
            except (OSError, ValueError):
                pass
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        with bundle.open(member) as source, temporary.open("wb") as output:
            copied = 0
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                copied += len(chunk)
                if copied % (64 * 1024 * 1024) < len(chunk):
                    emit_progress("extract", copied, member.file_size, "Extracting the arXiv metadata snapshot JSON.", "bytes")
        os.replace(temporary, destination)
        atomic_json(state_path, {"identity": identity})
    return destination, identity


def snapshot_date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    cleaned = value.strip()
    try:
        parsed = parsedate_to_datetime(cleaned) if "," in cleaned else datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return ""


def parse_snapshot_record(record: dict[str, Any], topics: set[str], profile: dict[str, Any], fetched_at: str) -> dict[str, Any] | None:
    identifier = versionless_id(clean_text(str(record.get("id", ""))))
    if not re.fullmatch(r"(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})", identifier, re.IGNORECASE):
        return None
    categories = clean_text(record.get("categories") if isinstance(record.get("categories"), str) else "").split()
    if not topics.intersection(categories) or not sampled(identifier, profile["samplePercent"], profile["sampleSeed"]):
        return None
    authors = []
    for author in record.get("authors_parsed", []):
        if isinstance(author, list):
            parts = [author[index] for index in (1, 0, 2) if index < len(author) and isinstance(author[index], str) and author[index].strip()]
            if parts:
                authors.append(clean_text(" ".join(parts)))
    if not authors and isinstance(record.get("authors"), str):
        authors = [clean_text(record["authors"])]
    versions = record.get("versions") if isinstance(record.get("versions"), list) else []
    version_dates = [snapshot_date(item.get("created")) for item in versions if isinstance(item, dict)]
    version_dates = [value for value in version_dates if value]
    published = version_dates[0] if version_dates else snapshot_date(record.get("update_date"))
    updated = snapshot_date(record.get("update_date")) or (version_dates[-1] if version_dates else published)
    return {
        "id": identifier,
        "title": clean_text(record.get("title") if isinstance(record.get("title"), str) else ""),
        "abstract": clean_text(record.get("abstract") if isinstance(record.get("abstract"), str) else ""),
        "authors_json": json.dumps(authors, ensure_ascii=False),
        "authors_search": " ".join(authors),
        "categories_json": json.dumps(categories),
        "categories_search": "|" + "|".join(categories) + "|",
        "primary_category": categories[0] if categories else None,
        "published": published,
        "updated": updated,
        "doi": clean_text(record.get("doi") if isinstance(record.get("doi"), str) else "") or None,
        "journal_ref": clean_text(record.get("journal-ref") if isinstance(record.get("journal-ref"), str) else "") or None,
        "comment": clean_text(record.get("comments") if isinstance(record.get("comments"), str) else "") or None,
        "abstract_url": f"https://arxiv.org/abs/{identifier}",
        "pdf_url": f"https://arxiv.org/pdf/{identifier}",
        "source_query": f"bulk:sample={profile['samplePercent']}%;topics={','.join(profile['topics'])}",
        "profile_slice": "bulk-snapshot",
        "fetched_at": fetched_at,
    }


def ingest_snapshot(connection: sqlite3.Connection, snapshot: Path, profile: dict[str, Any], checkpoint: dict[str, Any], checkpoint_path: Path, fetched_at: str) -> tuple[int, str]:
    topics = set(profile["topics"])
    offset = int(checkpoint.get("snapshotOffset", 0))
    records = int(checkpoint.get("snapshotRecords", 0))
    latest = str(checkpoint.get("snapshotCutoff", ""))
    batch = []
    total_bytes = snapshot.stat().st_size
    with snapshot.open("rb") as source:
        source.seek(offset)
        while line := source.readline():
            try:
                raw = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(f"invalid JSON record at snapshot byte {offset}") from error
            update_date = snapshot_date(raw.get("update_date"))
            if update_date > latest:
                latest = update_date
            paper = parse_snapshot_record(raw, topics, profile, fetched_at)
            if paper is not None:
                batch.append(paper)
            offset = source.tell()
            if len(batch) >= 1000:
                upsert_papers(connection, batch)
                records += len(batch)
                batch.clear()
                connection.commit()
                checkpoint.update({"snapshotOffset": offset, "snapshotRecords": records, "snapshotCutoff": latest})
                atomic_json(checkpoint_path, checkpoint)
                emit_progress("ingest", offset, total_bytes, f"Sampled {records} matching snapshot records.", "bytes")
        if batch:
            upsert_papers(connection, batch)
            records += len(batch)
            connection.commit()
    checkpoint.update({"phase": "catchup", "snapshotOffset": offset, "snapshotRecords": records, "snapshotCutoff": latest})
    atomic_json(checkpoint_path, checkpoint)
    return records, latest


def catch_up_oai(connection: sqlite3.Connection, profile: dict[str, Any], checkpoint: dict[str, Any], checkpoint_path: Path, client: OaiClient, from_date: str, until_date: str, fetched_at: str) -> None:
    progress = checkpoint.setdefault("catchup", {"topicIndex": 0, "resumptionToken": None})
    topics = set(profile["topics"])
    while progress["topicIndex"] < len(profile["topics"]):
        topic = profile["topics"][progress["topicIndex"]]
        token = progress.get("resumptionToken")
        source_query = f"oai:set={topic};from={from_date};until={until_date}"
        payload = client.fetch(topic, from_date, until_date, token)
        papers, next_token = parse_oai_feed(payload, source_query, "oai-catchup", fetched_at)
        selected = [paper for paper in papers if topics.intersection(json.loads(paper["categories_json"])) and sampled(paper["id"], profile["samplePercent"], profile["sampleSeed"])]
        upsert_papers(connection, selected)
        if next_token:
            progress["resumptionToken"] = next_token
        else:
            progress["topicIndex"] += 1
            progress["resumptionToken"] = None
        connection.commit()
        atomic_json(checkpoint_path, checkpoint)
        count = int(connection.execute("SELECT count(*) FROM papers").fetchone()[0])
        emit_progress("catchup", progress["topicIndex"], len(profile["topics"]), f"Added newer arXiv metadata; corpus now has {count} records.", "topics")


def inspect_database(database: Path) -> tuple[dict[str, str], int, dict[str, int]] | None:
    if not database.exists():
        return None
    try:
        with closing(connect(database, readonly=True)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            records = int(connection.execute("SELECT count(*) FROM papers").fetchone()[0])
            slice_records = {
                str(row[0]): int(row[1])
                for row in connection.execute("SELECT profile_slice, count(*) FROM papers GROUP BY profile_slice")
            }
        if metadata.get("schema_version") != SCHEMA_VERSION:
            return None
        return metadata, records, slice_records
    except (sqlite3.Error, OSError, ValueError):
        return None


def active_matches(database: Path, profile_hash: str) -> bool:
    if not database.exists():
        return False
    try:
        with connect(database, readonly=True) as connection:
            values = dict(connection.execute("SELECT key, value FROM metadata"))
            return values.get("schema_version") == SCHEMA_VERSION and values.get("profile_hash") == profile_hash
    except (sqlite3.Error, OSError):
        return False


def provision(
    data_dir: Path,
    profile_path: Path,
    client: OaiClient | None = None,
    cutoff: str | None = None,
    categories: list[str] | None = None,
    snapshot_path: Path | None = None,
) -> dict[str, Any]:
    base_profile, base_profile_hash = load_profile(profile_path)
    profile, profile_hash, content_hash = configure_profile(base_profile, base_profile_hash, categories)
    data_dir.mkdir(parents=True, exist_ok=True)
    archive = snapshot_path or download_snapshot(data_dir)
    snapshot, snapshot_identity = extract_snapshot(data_dir, archive)
    active = data_dir / "corpus.db"
    working = data_dir / "corpus.next.db"
    checkpoint_path = data_dir / "checkpoint.json"

    checkpoint = None
    if checkpoint_path.exists():
        try:
            candidate = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if working.exists() and candidate.get("profileHash") == profile_hash and candidate.get("snapshotIdentity") == snapshot_identity:
                checkpoint = candidate
        except (OSError, ValueError):
            checkpoint = None
    active_state = inspect_database(active)
    active_metadata = active_state[0] if active_state else {}
    reusable = active_metadata.get("profile_hash") == profile_hash and active_metadata.get("snapshot_identity") == snapshot_identity
    if checkpoint is None:
        working.unlink(missing_ok=True)
        if reusable:
            shutil.copy2(active, working)
        checkpoint = {
            "schemaVersion": 2,
            "profileHash": profile_hash,
            "snapshotIdentity": snapshot_identity,
            "phase": "catchup" if reusable else "snapshot",
        }
        atomic_json(checkpoint_path, checkpoint)

    client = client or OaiClient()
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with connect(working) as connection:
        initialize(connection)
        if checkpoint["phase"] == "snapshot":
            _, snapshot_cutoff = ingest_snapshot(connection, snapshot, profile, checkpoint, checkpoint_path, fetched_at)
        else:
            snapshot_cutoff = active_metadata.get("snapshot_cutoff", "")
        if not snapshot_cutoff:
            raise ValueError("arXiv snapshot does not contain a usable update date")
        from_date = active_metadata.get("catchup_cutoff", snapshot_cutoff)[:10] if reusable else snapshot_cutoff[:10]
        until_date = datetime.strptime(cutoff[:8], "%Y%m%d").date().isoformat() if cutoff else datetime.now(timezone.utc).date().isoformat()
        catch_up_oai(connection, profile, checkpoint, checkpoint_path, client, from_date, until_date, fetched_at)

        rebuild_search(connection)
        put_metadata(connection, {
            "schema_version": SCHEMA_VERSION,
            "profile_id": profile["id"],
            "profile_hash": profile_hash,
            "base_profile_hash": base_profile_hash,
            "content_hash": content_hash,
            "sample_percent": str(profile["samplePercent"]),
            "content_mode": "topics" if categories is not None else "profile",
            "configured_categories": json.dumps(profile["topics"], separators=(",", ":")),
            "category_counts": json.dumps(count_categories(connection), separators=(",", ":"), sort_keys=True),
            "source": "arXiv bulk metadata snapshot with OAI-PMH catch-up",
            "source_url": os.environ.get("VANTA_ARXIV_SNAPSHOT_URL", SNAPSHOT_URL),
            "catchup_source_url": OAI_URL,
            "source_terms_url": TERMS_URL,
            "snapshot_identity": snapshot_identity,
            "snapshot_cutoff": snapshot_cutoff,
            "catchup_cutoff": until_date,
            "cutoff": f"{until_date}T23:59:59Z",
            "refreshed_at": fetched_at,
        })
        connection.execute("ANALYZE")
        connection.commit()
        count = verify(connection)
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
    emit_progress("complete", count, count, "Corpus database activated.")
    return {"records": count, "reused": reusable}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    profile_source = parser.add_mutually_exclusive_group(required=True)
    profile_source.add_argument("--profile", type=Path)
    profile_source.add_argument("--profile-dir", type=Path)
    parser.add_argument("--profile-id", default=os.environ.get("VANTA_MODULE_OPTION_PROFILE_ID", "small-arxiv-cs"))
    parser.add_argument("--cutoff", help="Fixed UTC cutoff in YYYYMMDDHHMM format; primarily for deterministic recovery/tests.")
    parser.add_argument("--category", action="append", dest="categories")
    arguments = parser.parse_args()
    categories = arguments.categories
    if categories is None and os.environ.get("VANTA_MODULE_OPTION_CATEGORIES"):
        categories = json.loads(os.environ["VANTA_MODULE_OPTION_CATEGORIES"])
    profile_path = arguments.profile or resolve_profile(arguments.profile_dir, arguments.profile_id)
    result = provision(
        arguments.data_dir,
        profile_path,
        cutoff=arguments.cutoff,
        categories=categories,
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""arXiv source adapter: bulk metadata snapshot ingestion with OAI-PMH catch-up."""
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable

from fetching import (
    ThrottledClient,
    atomic_json,
    clean_text,
    download_file,
    emit_progress,
    iso_timestamp,
    user_agent,
)
from taxonomy import describe as describe_category

API_URL = "https://export.arxiv.org/api/query"
OAI_URL = "https://oaipmh.arxiv.org/oai"
SNAPSHOT_URL = "https://www.kaggle.com/api/v1/datasets/download/Cornell-University/arxiv"
SNAPSHOT_MEMBER = "arxiv-metadata-oai-snapshot.json"
TERMS_URL = "https://info.arxiv.org/help/api/tou.html"
LICENSE = "arXiv metadata under the arXiv API terms of use; per-article licences vary"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
OAI_ARXIV = "{http://arxiv.org/OAI/arXiv/}"
ARXIV_ID = re.compile(r"(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
BARE_ID = re.compile(r"(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})", re.IGNORECASE)
ABSTRACT_PREFIXES = ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "https://www.arxiv.org/abs/")
BATCH_RECORDS = 1000
PHYSICS_ARCHIVES = {
    "astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th", "math-ph",
    "nlin", "nucl-ex", "nucl-th", "physics", "quant-ph",
}


def snapshot_url() -> str:
    return os.environ.get("VANTA_ARXIV_SNAPSHOT_URL", SNAPSHOT_URL)


def versionless_id(url: str) -> str:
    identifier = url.rsplit("/abs/", 1)[-1]
    return re.sub(r"v\d+$", "", identifier)


def oai_set_spec(category: str) -> str:
    archive, separator, subject = category.partition(".")
    group = "physics" if archive in PHYSICS_ARCHIVES else archive
    return f"{group}:{archive}:{subject}" if separator else f"{group}:{archive}"


class ArxivClient(ThrottledClient):
    def fetch(self, query: str, start: int, page_size: int) -> bytes:
        parameters = urllib.parse.urlencode({
            "search_query": query,
            "start": start,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        })
        request = urllib.request.Request(f"{API_URL}?{parameters}", headers={"User-Agent": user_agent()})
        return self.request(request, timeout=60, label="arXiv API response")


class OaiClient(ThrottledClient):
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
            headers={"User-Agent": user_agent()},
        )
        return self.request(request, timeout=90, label="arXiv OAI response")


def parse_feed(payload: bytes, source_query: str, slice_id: str, fetched_at: str) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        identifier = versionless_id(clean_text(entry.findtext(f"{ATOM}id")))
        if not BARE_ID.fullmatch(identifier):
            continue
        authors = [clean_text(author.findtext(f"{ATOM}name")) for author in entry.findall(f"{ATOM}author")]
        authors = [author for author in authors if author]
        categories = [element.attrib.get("term", "") for element in entry.findall(f"{ATOM}category")]
        categories = [category for category in categories if category]
        links = entry.findall(f"{ATOM}link")
        abstract_url = next((link.attrib.get("href") for link in links if link.attrib.get("rel") == "alternate"), f"https://arxiv.org/abs/{identifier}")
        pdf_url = next((link.attrib.get("href") for link in links if link.attrib.get("title") == "pdf"), None)
        primary = entry.find(f"{ARXIV}primary_category")
        papers.append({
            "id": identifier,
            "source": "arxiv",
            "license": LICENSE,
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
        })
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
        if not BARE_ID.fullmatch(identifier):
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
            "source": "arxiv",
            "license": LICENSE,
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


def parse_snapshot_record(record: dict[str, Any], topics: set[str], spec: dict[str, Any], fetched_at: str, sampled: Callable[..., bool]) -> dict[str, Any] | None:
    identifier = versionless_id(clean_text(str(record.get("id", ""))))
    if not BARE_ID.fullmatch(identifier):
        return None
    categories = clean_text(record.get("categories") if isinstance(record.get("categories"), str) else "").split()
    if not topics.intersection(categories) or not sampled(identifier, spec["samplePercent"], spec["sampleSeed"]):
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
    version_dates = [iso_timestamp(item.get("created")) for item in versions if isinstance(item, dict)]
    version_dates = [value for value in version_dates if value]
    published = version_dates[0] if version_dates else iso_timestamp(record.get("update_date"))
    updated = iso_timestamp(record.get("update_date")) or (version_dates[-1] if version_dates else published)
    return {
        "id": identifier,
        "source": "arxiv",
        "license": LICENSE,
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
        "source_query": f"bulk:sample={spec['samplePercent']}%;topics={','.join(spec['topics'])}",
        "profile_slice": "bulk-snapshot",
        "fetched_at": fetched_at,
    }


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


class ArxivSource:
    """Sampled arXiv descriptive metadata: a bulk snapshot baseline plus incremental OAI-PMH harvest."""

    key = "arxiv-bulk-snapshot"
    id = "arxiv"
    prefix = ""
    name = "arXiv bulk metadata snapshot with OAI-PMH catch-up"
    license = LICENSE
    terms_url = TERMS_URL
    catchup_source_url = OAI_URL
    subject_scheme = "arxiv"
    supports_topics = True
    supports_catch_up = True

    @property
    def source_url(self) -> str:
        return snapshot_url()

    def validate(self, spec: dict[str, Any]) -> None:
        if spec.get("samplePercent") not in (1, 25, 100):
            raise ValueError("arXiv samplePercent must be 1, 25, or 100")
        if not isinstance(spec.get("sampleSeed"), str) or not spec["sampleSeed"]:
            raise ValueError("arXiv sampleSeed must be a non-empty string")
        topics = spec.get("topics")
        if not isinstance(topics, list) or not topics or not all(re.fullmatch(r"[A-Za-z0-9.-]+", value or "") for value in topics):
            raise ValueError("arXiv topics must contain valid arXiv categories")

    def identity_fields(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": self.key,
            "samplePercent": spec["samplePercent"],
            "sampleSeed": spec["sampleSeed"],
            "topics": spec["topics"],
        }

    def acquire(self, data_dir: Path, spec: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        archive = overrides.get("snapshot_path")
        if archive is None:
            destination = data_dir / "source" / "arxiv-metadata-oai-snapshot.zip"
            download_file(destination, snapshot_url(), "Downloading the arXiv metadata snapshot ZIP.", overrides.get("opener", urllib.request.urlopen))
            archive = destination
        snapshot, identity = extract_snapshot(data_dir, Path(archive))
        return {"identity": identity, "snapshot": snapshot}

    def ingest(self, connection: sqlite3.Connection, spec: dict[str, Any], handle: dict[str, Any], state: dict[str, Any], save: Callable[[], None], fetched_at: str, upsert, sampled) -> tuple[int, str]:
        snapshot: Path = handle["snapshot"]
        topics = set(spec["topics"])
        offset = int(state.get("offset", 0))
        records = int(state.get("records", 0))
        latest = str(state.get("cutoff", ""))
        batch: list[dict[str, Any]] = []
        total_bytes = snapshot.stat().st_size
        with snapshot.open("rb") as source:
            source.seek(offset)
            while line := source.readline():
                try:
                    raw = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError(f"invalid JSON record at snapshot byte {offset}") from error
                update_date = iso_timestamp(raw.get("update_date"))
                if update_date > latest:
                    latest = update_date
                paper = parse_snapshot_record(raw, topics, spec, fetched_at, sampled)
                if paper is not None:
                    batch.append(paper)
                offset = source.tell()
                if len(batch) >= BATCH_RECORDS:
                    upsert(connection, batch)
                    records += len(batch)
                    batch.clear()
                    connection.commit()
                    state.update({"offset": offset, "records": records, "cutoff": latest})
                    save()
                    emit_progress("ingest", offset, total_bytes, f"Sampled {records} matching arXiv records.", "bytes")
            if batch:
                upsert(connection, batch)
                records += len(batch)
                connection.commit()
        state.update({"offset": offset, "records": records, "cutoff": latest})
        save()
        if not latest:
            raise ValueError("arXiv snapshot does not contain a usable update date")
        return records, latest

    def catch_up(
        self,
        connection: sqlite3.Connection,
        spec: dict[str, Any],
        state: dict[str, Any],
        save: Callable[[], None],
        from_date: str,
        until_date: str,
        fetched_at: str,
        overrides: dict[str, Any],
        upsert,
        sampled,
        indexer=None,
    ) -> int:
        client = overrides.get("oai_client") or OaiClient()
        progress = state.setdefault("catchup", {"topicIndex": 0, "resumptionToken": None})
        topics = set(spec["topics"])
        added = 0
        while progress["topicIndex"] < len(spec["topics"]):
            topic = spec["topics"][progress["topicIndex"]]
            token = progress.get("resumptionToken")
            source_query = f"oai:set={topic};from={from_date};until={until_date}"
            payload = client.fetch(topic, from_date, until_date, token)
            papers, next_token = parse_oai_feed(payload, source_query, "oai-catchup", fetched_at)
            selected = [paper for paper in papers if topics.intersection(json.loads(paper["categories_json"])) and sampled(paper["id"], spec["samplePercent"], spec["sampleSeed"])]
            upsert(connection, selected)
            if indexer is not None and selected:
                indexer(connection, [paper["id"] for paper in selected])
            added += len(selected)
            if next_token:
                progress["resumptionToken"] = next_token
            else:
                progress["topicIndex"] += 1
                progress["resumptionToken"] = None
            connection.commit()
            save()
            count = int(connection.execute("SELECT count(*) FROM papers").fetchone()[0])
            emit_progress("catchup", progress["topicIndex"], len(spec["topics"]), f"Added newer arXiv metadata; corpus now has {count} records.", "topics")
        state.pop("catchup", None)
        save()
        return added

    def accepts(self, value: str) -> bool:
        candidate = value
        for prefix in ABSTRACT_PREFIXES:
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
        return bool(ARXIV_ID.fullmatch(candidate))

    def normalize(self, value: str) -> str:
        candidate = value
        for prefix in ABSTRACT_PREFIXES:
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
        if not ARXIV_ID.fullmatch(candidate):
            raise ValueError("id must be an arXiv identifier")
        return re.sub(r"v\d+$", "", candidate, flags=re.IGNORECASE)

    def describe(self, subject: str) -> tuple[str | None, str | None]:
        return describe_category(subject)

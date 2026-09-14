#!/usr/bin/env python3
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1"
ARXIV_ID = re.compile(r"^(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$", re.IGNORECASE)
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def connect(database: Path, readonly: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database}?mode=ro" if readonly else database, uri=readonly)
    connection.row_factory = sqlite3.Row
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    else:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS papers (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          abstract TEXT NOT NULL,
          authors_json TEXT NOT NULL,
          authors_search TEXT NOT NULL,
          categories_json TEXT NOT NULL,
          categories_search TEXT NOT NULL,
          primary_category TEXT,
          published TEXT NOT NULL,
          updated TEXT NOT NULL,
          doi TEXT,
          journal_ref TEXT,
          comment TEXT,
          abstract_url TEXT NOT NULL,
          pdf_url TEXT,
          source_query TEXT NOT NULL,
          profile_slice TEXT NOT NULL,
          fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS papers_published_idx ON papers(published);
        CREATE INDEX IF NOT EXISTS papers_primary_category_idx ON papers(primary_category);
                CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
                    title, abstract, authors, categories, tokenize='unicode61 remove_diacritics 2'
                );
        """
    )


def put_metadata(connection: sqlite3.Connection, values: dict[str, str]) -> None:
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        values.items(),
    )


def upsert_papers(connection: sqlite3.Connection, papers: Iterable[dict[str, Any]]) -> int:
    before = connection.total_changes
    connection.executemany(
        """
        INSERT INTO papers(
          id, title, abstract, authors_json, authors_search, categories_json, categories_search,
          primary_category, published, updated, doi, journal_ref, comment, abstract_url, pdf_url,
          source_query, profile_slice, fetched_at
        ) VALUES(
          :id, :title, :abstract, :authors_json, :authors_search, :categories_json, :categories_search,
          :primary_category, :published, :updated, :doi, :journal_ref, :comment, :abstract_url, :pdf_url,
          :source_query, :profile_slice, :fetched_at
        ) ON CONFLICT(id) DO UPDATE SET
          title=excluded.title, abstract=excluded.abstract, authors_json=excluded.authors_json,
          authors_search=excluded.authors_search, categories_json=excluded.categories_json,
          categories_search=excluded.categories_search, primary_category=excluded.primary_category,
          published=excluded.published, updated=excluded.updated, doi=excluded.doi,
          journal_ref=excluded.journal_ref, comment=excluded.comment,
          abstract_url=excluded.abstract_url, pdf_url=excluded.pdf_url,
          source_query=excluded.source_query, profile_slice=excluded.profile_slice, fetched_at=excluded.fetched_at
        """,
        papers,
    )
    return connection.total_changes - before


def rebuild_search(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM papers_fts")
    connection.execute(
        "INSERT INTO papers_fts(rowid, title, abstract, authors, categories) "
        "SELECT rowid, title, abstract, authors_search, categories_search FROM papers"
    )


def verify(connection: sqlite3.Connection, maximum: int | None = None) -> int:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"SQLite integrity check failed: {integrity}")
    count = int(connection.execute("SELECT count(*) FROM papers").fetchone()[0])
    if maximum is not None and count > maximum:
        raise ValueError(f"corpus has {count} records, above profile cap {maximum}")
    connection.execute("SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'science'").fetchone()
    return count


def info(database: Path) -> dict[str, Any]:
    with connect(database, readonly=True) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        count = int(connection.execute("SELECT count(*) FROM papers").fetchone()[0])
        categories = [dict(row) for row in connection.execute(
            "SELECT primary_category AS category, count(*) AS records FROM papers "
            "WHERE primary_category IS NOT NULL GROUP BY primary_category ORDER BY records DESC, category LIMIT 100"
        )]
    sample_percent = metadata.get("sample_percent")
    configured_categories = metadata.get("configured_categories")
    return {
        "schemaVersion": metadata.get("schema_version"),
        "profileId": metadata.get("profile_id"),
        "profileHash": metadata.get("profile_hash"),
        "samplePercent": int(sample_percent) if sample_percent is not None else None,
        "contentMode": metadata.get("content_mode", "profile"),
        "topics": json.loads(configured_categories) if configured_categories is not None else None,
        "source": metadata.get("source"),
        "sourceUrl": metadata.get("source_url"),
        "catchUpSourceUrl": metadata.get("catchup_source_url"),
        "sourceTermsUrl": metadata.get("source_terms_url"),
        "snapshotCutoff": metadata.get("snapshot_cutoff"),
        "cutoff": metadata.get("cutoff"),
        "refreshedAt": metadata.get("refreshed_at"),
        "records": count,
        "bytes": database.stat().st_size,
        "categories": categories,
    }


def normalize_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("id must be an arXiv identifier")
    identifier = value.strip().removeprefix("https://arxiv.org/abs/").removeprefix("http://arxiv.org/abs/")
    if not ARXIV_ID.fullmatch(identifier):
        raise ValueError("id must be an arXiv identifier")
    return re.sub(r"v\d+$", "", identifier, flags=re.IGNORECASE)


def paper_dict(row: sqlite3.Row, include_abstract: bool = True) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "title": row["title"],
        "authors": json.loads(row["authors_json"]),
        "categories": json.loads(row["categories_json"]),
        "primaryCategory": row["primary_category"],
        "published": row["published"],
        "updated": row["updated"],
        "doi": row["doi"],
        "journalReference": row["journal_ref"],
        "comment": row["comment"],
        "abstractUrl": row["abstract_url"],
        "pdfUrl": row["pdf_url"],
        "sourceQuery": row["source_query"],
        "profileSlice": row["profile_slice"],
        "fetchedAt": row["fetched_at"],
    }
    if include_abstract:
        result["abstract"] = row["abstract"]
    return result


def get_paper(database: Path, identifier: Any) -> dict[str, Any]:
    normalized = normalize_id(identifier)
    with connect(database, readonly=True) as connection:
        row = connection.execute("SELECT * FROM papers WHERE id = ?", (normalized,)).fetchone()
    if row is None:
        raise ValueError(f"arXiv record not found: {normalized}")
    return paper_dict(row)


def search(database: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("query must be a non-empty string of at most 500 characters")
    tokens = re.findall(r"[^\W_][\w.-]*", query, flags=re.UNICODE)
    if not tokens or len(tokens) > 32:
        raise ValueError("query must contain from 1 to 32 searchable terms")
    match = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
    limit = arguments.get("limit", 10)
    offset = arguments.get("offset", 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10_000:
        raise ValueError("offset must be an integer from 0 to 10000")
    category = arguments.get("category")
    if category is not None and (not isinstance(category, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,40}", category)):
        raise ValueError("category is invalid")
    published_from = arguments.get("publishedFrom")
    published_to = arguments.get("publishedTo")
    if published_from is not None and (not isinstance(published_from, str) or not DATE.fullmatch(published_from)):
        raise ValueError("publishedFrom must use YYYY-MM-DD")
    if published_to is not None and (not isinstance(published_to, str) or not DATE.fullmatch(published_to)):
        raise ValueError("publishedTo must use YYYY-MM-DD")

    where = ["papers_fts MATCH ?"]
    parameters: list[Any] = [match]
    if category:
        where.append("instr(p.categories_search, ?) > 0")
        parameters.append(f"|{category}|")
    if published_from:
        where.append("substr(p.published, 1, 10) >= ?")
        parameters.append(published_from)
    if published_to:
        where.append("substr(p.published, 1, 10) <= ?")
        parameters.append(published_to)
    parameters.extend([limit, offset])
    sql = (
        "SELECT p.*, bm25(papers_fts, 8.0, 2.0, 1.0, 1.0) AS rank, "
        "snippet(papers_fts, 1, '[', ']', '...', 24) AS snippet "
        "FROM papers_fts JOIN papers p ON p.rowid = papers_fts.rowid WHERE " + " AND ".join(where) +
        " ORDER BY rank, p.published DESC LIMIT ? OFFSET ?"
    )
    with connect(database, readonly=True) as connection:
        rows = list(connection.execute(sql, parameters))
    results = []
    for row in rows:
        value = paper_dict(row, include_abstract=False)
        value.update({"snippet": row["snippet"], "score": -float(row["rank"])})
        results.append(value)
    return {"query": query, "results": results, "limit": limit, "offset": offset, "hasMore": len(results) == limit}


def self_test() -> None:
    with sqlite3.connect(":memory:") as connection:
        initialize(connection)
        assert connection.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0] == 1
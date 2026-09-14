#!/usr/bin/env python3
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from query import fold, parse
from taxonomy import describe

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
                CREATE VIRTUAL TABLE IF NOT EXISTS papers_vocab USING fts5vocab(papers_fts, 'row');
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


def count_categories(connection: sqlite3.Connection) -> dict[str, int]:
    """Count every category membership, including cross-lists.

    Run at provisioning time only: it scans the whole corpus, which the read-only tools must not do.
    """
    counts: dict[str, int] = {}
    for (packed,) in connection.execute("SELECT categories_search FROM papers"):
        for category in packed.split("|"):
            if category:
                counts[category] = counts.get(category, 0) + 1
    return counts


def list_categories(database: Path, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    contains = arguments.get("contains")
    if contains is not None and (not isinstance(contains, str) or not 1 <= len(contains) <= 80):
        raise ValueError("contains must be a string of 1 to 80 characters")
    ingested_only = arguments.get("ingestedOnly", False)
    if not isinstance(ingested_only, bool):
        raise ValueError("ingestedOnly must be a boolean")

    with connect(database, readonly=True) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        primary = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT primary_category, count(*) FROM papers WHERE primary_category IS NOT NULL GROUP BY primary_category"
            )
        }

    topics = set(json.loads(metadata.get("configured_categories") or "[]"))
    packed_counts = metadata.get("category_counts")
    counts: dict[str, int] = json.loads(packed_counts) if packed_counts else {}
    # Databases provisioned before category counts were recorded still expose primary-category totals.
    complete = packed_counts is not None
    names = set(counts) | set(primary) | topics

    needle = contains.casefold() if contains else None
    entries = []
    for category in sorted(names):
        group, name = describe(category)
        if ingested_only and category not in topics:
            continue
        if needle and needle not in category.casefold() and needle not in (name or "").casefold():
            continue
        entries.append({
            "category": category,
            "name": name,
            "group": group,
            "ingestionTopic": category in topics,
            "records": counts.get(category) if complete else None,
            "primaryRecords": primary.get(category, 0),
        })
    entries.sort(key=lambda entry: (-(entry["records"] or entry["primaryRecords"]), entry["category"]))
    return {
        "categories": entries,
        "total": len(entries),
        "countsIncludeCrossLists": complete,
        "ingestionTopics": sorted(topics),
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


MAX_EDITS = 2
MAX_CANDIDATES = 20_000


def bounded_edit_distance(left: str, right: str, maximum: int) -> int | None:
    """Levenshtein distance, abandoned as soon as it is known to exceed `maximum`."""
    if abs(len(left) - len(right)) > maximum:
        return None
    previous = list(range(len(right) + 1))
    for i, left_character in enumerate(left, start=1):
        current = [i]
        best = i
        for j, right_character in enumerate(right, start=1):
            cost = 0 if left_character == right_character else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > maximum:
            return None
        previous = current
    return previous[-1] if previous[-1] <= maximum else None


def known_term(connection: sqlite3.Connection, term: str) -> bool:
    return connection.execute("SELECT 1 FROM papers_vocab WHERE term = ? LIMIT 1", (term,)).fetchone() is not None


def nearest_term(connection: sqlite3.Connection, term: str) -> str | None:
    """Find the most common indexed term within a small edit distance of `term`."""
    # Typos rarely fall in the first characters, so a short prefix keeps the candidate scan seekable.
    prefix = term[:3] if len(term) >= 5 else term[:1]
    if not prefix:
        return None
    allowed = 1 if len(term) <= 4 else MAX_EDITS
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    best: tuple[int, int, str] | None = None
    rows = connection.execute(
        "SELECT term, doc FROM papers_vocab WHERE term >= ? AND term < ? LIMIT ?",
        (prefix, upper, MAX_CANDIDATES),
    )
    for candidate, documents in rows:
        distance = bounded_edit_distance(term, candidate, allowed)
        if distance is None or distance == 0:
            continue
        ranked = (distance, -int(documents), candidate)
        if best is None or ranked < best:
            best = ranked
    return best[2] if best else None


def correct_terms(connection: sqlite3.Connection, parsed: Any) -> list[dict[str, str]]:
    """Replace unknown required words with their nearest indexed spelling, in place."""
    corrections: list[dict[str, str]] = []
    for clause in parsed.correctable():
        original = fold(clause.text)
        if not original or known_term(connection, original):
            continue
        replacement = nearest_term(connection, original)
        if replacement is None:
            continue
        corrections.append({"from": clause.text, "to": replacement})
        clause.text = replacement
    return corrections


def search(database: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    parsed = parse(query)
    limit = arguments.get("limit", 10)
    offset = arguments.get("offset", 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10_000:
        raise ValueError("offset must be an integer from 0 to 10000")
    fuzzy = arguments.get("fuzzy", True)
    if not isinstance(fuzzy, bool):
        raise ValueError("fuzzy must be a boolean")
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
    filters: list[Any] = []
    if category:
        where.append("instr(p.categories_search, ?) > 0")
        filters.append(f"|{category}|")
    if published_from:
        where.append("substr(p.published, 1, 10) >= ?")
        filters.append(published_from)
    if published_to:
        where.append("substr(p.published, 1, 10) <= ?")
        filters.append(published_to)
    sql = (
        "SELECT p.*, bm25(papers_fts, 8.0, 2.0, 1.0, 1.0) AS rank, "
        "snippet(papers_fts, 1, '[', ']', '...', 24) AS snippet "
        "FROM papers_fts JOIN papers p ON p.rowid = papers_fts.rowid WHERE " + " AND ".join(where) +
        " ORDER BY rank, p.published DESC LIMIT ? OFFSET ?"
    )

    corrections: list[dict[str, str]] = []
    with connect(database, readonly=True) as connection:
        try:
            rows = list(connection.execute(sql, [parsed.render(), *filters, limit, offset]))
        except sqlite3.OperationalError as error:
            raise ValueError(f"query could not be evaluated: {error}") from error
        # Spelling is only worth paying for when the query as written found nothing.
        if not rows and fuzzy and not offset:
            try:
                corrections = correct_terms(connection, parsed)
            except sqlite3.OperationalError:
                corrections = []
            if corrections:
                rows = list(connection.execute(sql, [parsed.render(), *filters, limit, offset]))

    results = []
    for row in rows:
        value = paper_dict(row, include_abstract=False)
        value.update({"snippet": row["snippet"], "score": -float(row["rank"])})
        results.append(value)
    response = {"query": query, "results": results, "limit": limit, "offset": offset, "hasMore": len(results) == limit}
    if corrections:
        response["corrections"] = corrections
    if not results and not offset:
        # An empty result is the one moment a caller reliably needs the syntax, so carry it in the response.
        response["hint"] = (
            "No records matched. Terms are combined with AND and matched literally, without stemming. "
            'Try fewer words, "exact phrase", alternative OR alternative, or a prefix* term. '
            "Use corpus_categories to confirm a category identifier and corpus_info for the corpus coverage and cutoff."
        )
    return response


def self_test() -> None:
    with sqlite3.connect(":memory:") as connection:
        initialize(connection)
        assert connection.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0] == 1
    assert describe("cs.RO") == ("Computer Science", "Robotics")
    assert describe("not-a-category") == (None, None)
    assert parse("robot learning").render() == '"robot" AND "learning"'
    assert parse('"deep learning" -survey').render() == '("deep learning") NOT ("survey")'
    assert parse("robot OR drone").render() == '("robot" OR "drone")'
    assert parse("robot or drone").render() == parse("robot OR drone").render()
    assert parse("robot and drone").render() == parse("robot drone").render()
    assert parse("robot not survey").render() == parse("robot -survey").render()
    assert parse('robot "or"').render() == '"robot" AND "or"'
    assert parse("title:transformer robo*").render() == '{title} : "transformer" AND "robo"*'
    assert bounded_edit_distance("robotics", "robitics", 2) == 1
    assert bounded_edit_distance("robotics", "chemistry", 2) is None
    assert fold("Schr\u00f6dinger") == "schrodinger"
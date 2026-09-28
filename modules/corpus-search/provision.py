#!/usr/bin/env python3
"""Provision and refresh the corpus database from one or more registered sources.

This module owns the pipeline: profile resolution and hashing, content identity, checkpointed
recovery, sampling, verification, and the atomic swap of the activated database. Everything specific
to an upstream dataset lives behind a source adapter in `sources/`, so adding a dataset is a matter of
registering an adapter and naming it in a profile.

One profile configures exactly one source. An installation names the set of profiles it wants, and the
corpus is their union, so sources stay independent of each other and are selected additively.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sources
from corpus import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    connect,
    census,
    drop_source,
    index_papers,
    index_source,
    initialize,
    migrate,
    put_metadata,
    put_source,
    source_rows,
    total_records,
    upsert_papers,
    verify,
)
from fetching import atomic_json, copy_file, emit_progress
from sources.arxiv import ArxivClient, OaiClient, oai_set_spec, parse_feed, parse_oai_feed

CHECKPOINT_VERSION = 3
DEFAULT_PROFILE_IDS = ["small-arxiv-cs"]
MAX_PROFILES = 4
CATEGORY = re.compile(r"[A-Za-z0-9.-]{1,40}")
PROFILE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

__all__ = [
    "ArxivClient",
    "OaiClient",
    "configure_profile",
    "configure_profiles",
    "load_profile",
    "load_profiles",
    "oai_set_spec",
    "parse_feed",
    "parse_oai_feed",
    "provision",
    "refresh",
    "resolve_profile",
    "resolve_profiles",
    "sampled",
]


def load_profile(path: Path) -> tuple[dict[str, Any], str]:
    """Read and validate one source's profile, returning it with the hash of its exact bytes.

    The bytes are the profile's identity, which is why validation never rewrites or normalizes them.
    That hash is also stored on the returned profile as `digest`, so a composed installation can still
    identify each source independently of the set it was named in.
    """
    raw = path.read_bytes()
    profile = json.loads(raw)
    if profile.get("schemaVersion") != 2:
        raise ValueError("unsupported corpus profile")
    sources.by_key(profile.get("source", "")).validate(profile)
    digest = hashlib.sha256(raw).hexdigest()
    profile["digest"] = digest
    return profile, digest


def load_profiles(paths: Sequence[Path]) -> tuple[list[dict[str, Any]], str]:
    """Load the profiles an installation composes, ordered so their identity does not depend on order."""
    if not 1 <= len(paths) <= MAX_PROFILES:
        raise ValueError(f"an installation composes from 1 to {MAX_PROFILES} profiles")
    loaded = sorted((load_profile(path) for path in paths), key=lambda item: item[0]["id"])
    profiles = [profile for profile, _ in loaded]
    if len({profile["id"] for profile in profiles}) != len(profiles):
        raise ValueError("the same profile is named more than once")
    if len({profile["source"] for profile in profiles}) != len(profiles):
        raise ValueError("two of the named profiles configure the same source")
    if len(profiles) == 1:
        return profiles, loaded[0][1]
    combined = [{"id": profile["id"], "hash": digest} for profile, digest in loaded]
    return profiles, hashlib.sha256(json.dumps(combined, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_profile(profile_dir: Path, profile_id: str) -> Path:
    if not PROFILE_ID.fullmatch(profile_id):
        raise ValueError("a profile ID must be a lowercase kebab-case identifier")
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


def resolve_profiles(profile_dir: Path, profile_ids: Sequence[str]) -> list[Path]:
    return [resolve_profile(profile_dir, profile_id) for profile_id in profile_ids]


def configure_profile(
    profile: dict[str, Any],
    base_profile_hash: str,
    categories: list[str] | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Apply an optional topic override to one profile and derive its profile and content identities.

    Without an override the profile hash stays the hash of the packaged file, so a corpus already
    provisioned from that file is still recognized as reusable after the module is upgraded.
    """
    configured, = apply_categories([profile], categories)
    content = {"baseProfileHash": base_profile_hash, **sources.by_key(configured["source"]).identity_fields(configured)}
    content_hash = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return configured, base_profile_hash if categories is None else content_hash, content_hash


def configure_profiles(
    profiles: Sequence[dict[str, Any]],
    base_profile_hash: str,
    categories: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str, str]:
    """Derive the identities of the composed installation.

    A single profile keeps the identity it had before composition existed, which is what lets an
    already provisioned corpus be reused rather than rebuilt.
    """
    if len(profiles) == 1:
        configured, profile_hash, content_hash = configure_profile(profiles[0], base_profile_hash, categories)
        return [configured], profile_hash, content_hash
    configured = apply_categories(profiles, categories)
    content = {
        "baseProfileHash": base_profile_hash,
        "sources": [sources.by_key(profile["source"]).identity_fields(profile) for profile in configured],
    }
    content_hash = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return configured, base_profile_hash if categories is None else content_hash, content_hash


def apply_categories(profiles: Sequence[dict[str, Any]], categories: list[str] | None) -> list[dict[str, Any]]:
    """Replace the topic list of every profile whose source has a subject scheme."""
    configured = [dict(profile) for profile in profiles]
    if categories is None:
        return configured
    if not isinstance(categories, list) or not 1 <= len(categories) <= 50:
        raise ValueError("categories must contain from 1 to 50 arXiv categories")
    if not all(isinstance(category, str) and CATEGORY.fullmatch(category) for category in categories):
        raise ValueError("categories contains an invalid arXiv category")
    topics = list(dict.fromkeys(categories))
    targets = [profile for profile in configured if sources.by_key(profile["source"]).supports_topics]
    if not targets:
        raise ValueError("none of the named profiles configures a source that accepts a topic list")
    for profile in targets:
        profile["topics"] = topics
    return configured


def source_config_hash(profile: dict[str, Any]) -> str:
    """Identify one source's configuration: its profile bytes, the fields that select records, and
    the version of the adapter that parses them.

    Reuse otherwise compares only the upstream artefact and the profile, so a correction to how an
    adapter builds records would never reach a corpus already holding them. An adapter bumps
    `recordVersion` when its output changes; the key is omitted at version 1 so that every corpus
    provisioned before this existed is still recognized as holding its sources unchanged.
    """
    adapter = sources.by_key(profile["source"])
    version = getattr(adapter, "record_version", 1)
    content = {
        "baseProfileHash": profile["digest"],
        **adapter.identity_fields(profile),
        **({"recordVersion": version} if version > 1 else {}),
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sampled(identifier: str, sample_percent: int, seed: str) -> bool:
    if sample_percent == 100:
        return True
    value = int.from_bytes(hashlib.sha256(f"{seed}\0{identifier}".encode()).digest()[:8], "big")
    return value < (1 << 64) * sample_percent // 100


def inspect_database(database: Path) -> dict[str, str] | None:
    """Read the metadata of a retained database, or None when it cannot serve as a reuse baseline.

    Every schema version this module can migrate counts as reusable. A corpus written by an earlier
    version is upgraded in place on the working copy rather than being reingested.
    """
    if not database.exists():
        return None
    try:
        with closing(connect(database, readonly=True)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            if metadata.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
                return None
        return metadata
    except (sqlite3.Error, OSError, ValueError):
        return None


def active_sources(database: Path) -> dict[str, Any]:
    try:
        with closing(connect(database, readonly=True)) as connection:
            return {row["source"]: row for row in source_rows(connection)}
    except (sqlite3.Error, OSError):
        return {}


def until_date_for(cutoff: str | None) -> str:
    return datetime.strptime(cutoff[:8], "%Y%m%d").date().isoformat() if cutoff else datetime.now(timezone.utc).date().isoformat()


def source_overrides(overrides: dict[str, Any] | None, client: Any = None, snapshot_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Collect per-source test and recovery injections, keyed by source identifier."""
    resolved = {key: dict(value) for key, value in (overrides or {}).items()}
    arxiv = resolved.setdefault("arxiv", {})
    if client is not None:
        arxiv["oai_client"] = client
    if snapshot_path is not None:
        arxiv["snapshot_path"] = snapshot_path
    return resolved


def previous_state(rows: dict[str, Any], metadata: dict[str, str], source: str) -> dict[str, Any]:
    """Recover a source's recorded cutoffs, falling back to the pre-multi-source metadata layout."""
    if source in rows:
        return dict(rows[source])
    if source == "arxiv":
        return {
            "snapshot_cutoff": metadata.get("snapshot_cutoff", ""),
            "catchup_cutoff": metadata.get("catchup_cutoff", ""),
            "identity": metadata.get("snapshot_identity"),
            "config_hash": metadata.get("content_hash"),
            "profile_hash": metadata.get("profile_hash"),
        }
    return {}


def source_is_current(recorded: dict[str, Any], profile: dict[str, Any], identity: str) -> bool:
    """Decide whether a retained corpus already holds this source exactly as configured.

    Sources are independent, so this is asked per source rather than for the corpus as a whole:
    adding or dropping one source must not cost a reingestion of the others.
    """
    if not recorded or recorded.get("identity") != identity:
        return False
    if recorded.get("config_hash"):
        return recorded["config_hash"] == source_config_hash(profile)
    # A corpus written before content hashing recorded only the profile hash of its single source.
    return bool(recorded.get("profile_hash")) and recorded["profile_hash"] == profile["digest"]


def provision(
    data_dir: Path,
    profile_paths: Path | Sequence[Path],
    client: OaiClient | None = None,
    cutoff: str | None = None,
    categories: list[str] | None = None,
    snapshot_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = [profile_paths] if isinstance(profile_paths, Path) else list(profile_paths)
    base_profiles, base_profile_hash = load_profiles(paths)
    profiles, profile_hash, content_hash = configure_profiles(base_profiles, base_profile_hash, categories)
    data_dir.mkdir(parents=True, exist_ok=True)
    overrides = source_overrides(overrides, client, snapshot_path)

    specs = [(sources.by_key(profile["source"]), profile) for profile in profiles]
    handles: dict[str, dict[str, Any]] = {}
    identities: dict[str, str] = {}
    for adapter, spec in specs:
        handles[adapter.id] = adapter.acquire(data_dir, spec, overrides.get(adapter.id, {}))
        identities[adapter.id] = handles[adapter.id]["identity"]

    active = data_dir / "corpus.db"
    working = data_dir / "corpus.next.db"
    checkpoint_path = data_dir / "checkpoint.json"

    checkpoint = None
    if checkpoint_path.exists():
        try:
            candidate = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if (
                working.exists()
                and candidate.get("schemaVersion") == CHECKPOINT_VERSION
                and candidate.get("profileHash") == profile_hash
                and candidate.get("identities") == identities
            ):
                checkpoint = candidate
        except (OSError, ValueError):
            checkpoint = None

    active_metadata = inspect_database(active) or {}
    recorded = active_sources(active) if active_metadata else {}
    current = {
        adapter.id: source_is_current(previous_state(recorded, active_metadata, adapter.id), spec, identities[adapter.id])
        for adapter, spec in specs
    }
    # Seeding is worth its copy as soon as one source survives unchanged; the rest are reingested.
    reusable = bool(active_metadata) and any(current.values())
    if checkpoint is None:
        working.unlink(missing_ok=True)
        if reusable:
            copy_file(active, working, "seed", "Copying the retained corpus.")
        checkpoint = {
            "schemaVersion": CHECKPOINT_VERSION,
            "profileHash": profile_hash,
            "identities": identities,
            "reused": reusable,
            "current": current,
            "sources": {},
        }
        atomic_json(checkpoint_path, checkpoint)
    reusable = bool(checkpoint.get("reused", reusable))
    current = checkpoint.get("current", current)

    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    until_date = until_date_for(cutoff)
    summaries: list[dict[str, Any]] = []
    with connect(working) as connection:
        initialize(connection)
        migrate(connection)

        def save() -> None:
            atomic_json(checkpoint_path, checkpoint)

        if not checkpoint.get("pruned"):
            # Runs once per checkpoint, because a resumed ingest would otherwise lose its own records.
            held = {row["source"] for row in source_rows(connection)}
            if not held and connection.execute("SELECT 1 FROM papers LIMIT 1").fetchone():
                held = {"arxiv"}
            for source in sorted(held):
                if not current.get(source):
                    drop_source(connection, source)
            connection.commit()
            checkpoint["pruned"] = True
            save()

        # A reingested source arrives without index entries and is indexed below; a retained one keeps
        # the entries the seeded copy brought, so only its catch-up records need adding.
        reingested = [adapter.id for adapter, _ in specs if not current.get(adapter.id)]
        for adapter, spec in specs:
            state = checkpoint["sources"].setdefault(adapter.id, {})
            earlier = previous_state(recorded, active_metadata, adapter.id)
            if current.get(adapter.id):
                snapshot_cutoff = str(earlier.get("snapshot_cutoff") or "")
                if not snapshot_cutoff:
                    raise ValueError(f"the retained corpus records no cutoff for source {adapter.id}")
                from_date = str(earlier.get("catchup_cutoff") or snapshot_cutoff)[:10]
            else:
                _, snapshot_cutoff = adapter.ingest(connection, spec, handles[adapter.id], state, save, fetched_at, upsert_papers, sampled)
                from_date = snapshot_cutoff[:10]
            if adapter.supports_catch_up:
                adapter.catch_up(
                    connection, spec, state, save, from_date, until_date, fetched_at,
                    overrides.get(adapter.id, {}), upsert_papers, sampled,
                    None if adapter.id in reingested else index_papers,
                )
            summaries.append({
                "adapter": adapter,
                "spec": spec,
                "snapshotCutoff": snapshot_cutoff,
                "catchupCutoff": until_date if adapter.supports_catch_up else None,
            })

        for source in reingested:
            emit_progress("index", 0, 0, f"Building the full-text index for {source}.")
            index_source(connection, source)
        emit_progress("summarize", 0, 0, "Counting records and categories.")
        tally = census(connection)
        for summary in summaries:
            adapter, spec = summary["adapter"], summary["spec"]
            put_source(connection, {
                "source": adapter.id,
                "name": adapter.name,
                "profile_id": spec["id"],
                "identity": identities[adapter.id],
                "config_hash": source_config_hash(spec),
                "source_url": adapter.source_url,
                "catchup_source_url": adapter.catchup_source_url,
                "terms_url": adapter.terms_url,
                "license": adapter.license,
                "topics_json": json.dumps(spec.get("topics", []), separators=(",", ":")),
                "sample_percent": spec.get("samplePercent"),
                "snapshot_cutoff": summary["snapshotCutoff"],
                "catchup_cutoff": summary["catchupCutoff"],
                "records": tally["records"].get(adapter.id, 0),
                "refreshed_at": fetched_at,
            })

        # The top-level metadata keeps describing the corpus through one primary source, which keeps
        # corpus_info answerable for callers written before per-source provenance existed.
        primary = next((item for item in summaries if item["adapter"].id == "arxiv"), summaries[0])
        adapter, spec = primary["adapter"], primary["spec"]
        put_metadata(connection, {
            "schema_version": SCHEMA_VERSION,
            "profile_id": spec["id"],
            "profile_ids": json.dumps([item["id"] for item in profiles], separators=(",", ":")),
            "profile_hash": profile_hash,
            "base_profile_hash": base_profile_hash,
            "content_hash": content_hash,
            "sample_percent": str(spec["samplePercent"]),
            "content_mode": "topics" if categories is not None else "profile",
            "configured_categories": json.dumps(spec.get("topics", []), separators=(",", ":")),
            "category_counts": json.dumps(tally["categories"], separators=(",", ":"), sort_keys=True),
            "primary_category_counts": json.dumps(
                tally["primaryCategories"], separators=(",", ":"), sort_keys=True
            ),
            "source": adapter.name,
            "source_url": adapter.source_url,
            "catchup_source_url": adapter.catchup_source_url or "",
            "source_terms_url": adapter.terms_url,
            "snapshot_identity": identities[adapter.id],
            "source_identities": json.dumps(identities, sort_keys=True, separators=(",", ":")),
            "profile_sources": json.dumps([item["adapter"].id for item in summaries], separators=(",", ":")),
            "snapshot_cutoff": primary["snapshotCutoff"],
            "catchup_cutoff": until_date,
            "cutoff": f"{until_date}T23:59:59Z",
            "refreshed_at": fetched_at,
        })
        # Sampled statistics keep the planner informed without the full index scan ANALYZE defaults to.
        emit_progress("optimize", 0, 0, "Updating query planner statistics.")
        connection.execute("PRAGMA analysis_limit=1000")
        connection.execute("ANALYZE")
        connection.commit()
        total = sum(tally["records"].values())
        emit_progress("verify", total, total, "Verifying the corpus.")
        count = verify(connection, count=total)
        if count == 0:
            raise ValueError("this corpus profile produced an empty corpus")
        emit_progress("activate", count, count, "Activating the corpus database.")
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


def refresh(
    data_dir: Path,
    profile_dir: Path,
    source: str | None = None,
    cutoff: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bring the activated corpus up to date in place, without reingesting its baseline.

    Only a source that publishes an incremental feed can be refreshed; the others report why they
    were skipped. Updates are written to the live database and indexed record by record, so readers
    keep serving the corpus throughout and no second copy of it is needed on the storage volume.
    """
    database = data_dir / "corpus.db"
    if not database.exists():
        raise ValueError("no activated corpus database is present")
    if source is not None and sources.find(source) is None:
        raise ValueError(f"unknown source '{source}'; known sources are {', '.join(sources.identifiers())}")
    overrides = source_overrides(overrides)
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    until_date = until_date_for(cutoff)

    refreshed: list[dict[str, Any]] = []
    caught_up: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    added = 0
    # A rollback journal keeps the activated file self-contained, so read-only callers are unaffected.
    with connect(database, journal="DELETE") as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError("the activated corpus uses an unsupported schema version")
        migrate(connection)
        profile_ids = json.loads(metadata.get("profile_ids") or "[]") or ([metadata["profile_id"]] if metadata.get("profile_id") else [])
        if not profile_ids:
            raise ValueError("the activated corpus does not record the profiles it was built from")
        categories = json.loads(metadata.get("configured_categories") or "[]") if metadata.get("content_mode") == "topics" else None
        base_profiles, base_profile_hash = load_profiles(resolve_profiles(profile_dir, profile_ids))
        profiles, profile_hash, _ = configure_profiles(base_profiles, base_profile_hash, categories)
        if profile_hash != metadata.get("profile_hash"):
            raise ValueError("the installed profiles no longer match the activated corpus; reinstall to change them")

        recorded = {row["source"]: row for row in source_rows(connection)}
        for spec in profiles:
            adapter = sources.by_key(spec["source"])
            if source is not None and adapter.id != source:
                continue
            if not adapter.supports_catch_up:
                skipped.append({"source": adapter.id, "reason": "this source publishes no incremental feed; reinstall to adopt a newer snapshot"})
                continue
            earlier = previous_state(recorded, metadata, adapter.id)
            baseline = str(earlier.get("catchup_cutoff") or earlier.get("snapshot_cutoff") or "")
            if not baseline:
                skipped.append({"source": adapter.id, "reason": "no recorded cutoff to resume from; reinstall to rebuild this source"})
                continue
            from_date = baseline[:10]
            # The recorded total is authoritative, which saves counting the source before and after.
            before = earlier.get("records")
            if before is None:
                before = int(connection.execute("SELECT count(*) FROM papers WHERE source = ?", (adapter.id,)).fetchone()[0])
            adapter.catch_up(
                connection, spec, {}, lambda: None, from_date, until_date, fetched_at,
                overrides.get(adapter.id, {}), upsert_papers, sampled, index_papers,
            )
            caught_up.append({"adapter": adapter, "spec": spec, "earlier": earlier, "before": int(before), "from": from_date})

        if caught_up:
            emit_progress("summarize", 0, 0, "Counting records and categories.")
            tally = census(connection)
            for entry in caught_up:
                adapter, spec, earlier = entry["adapter"], entry["spec"], entry["earlier"]
                records = tally["records"].get(adapter.id, 0)
                added += records - entry["before"]
                put_source(connection, {
                    "source": adapter.id,
                    "name": adapter.name,
                    "profile_id": spec["id"],
                    "identity": earlier.get("identity"),
                    "config_hash": earlier.get("config_hash") or source_config_hash(spec),
                    "source_url": adapter.source_url,
                    "catchup_source_url": adapter.catchup_source_url,
                    "terms_url": adapter.terms_url,
                    "license": adapter.license,
                    "topics_json": json.dumps(spec.get("topics", []), separators=(",", ":")),
                    "sample_percent": spec.get("samplePercent"),
                    "snapshot_cutoff": earlier.get("snapshot_cutoff"),
                    "catchup_cutoff": until_date,
                    "records": records,
                    "refreshed_at": fetched_at,
                })
                refreshed.append({
                    "source": adapter.id, "from": entry["from"], "until": until_date,
                    "added": records - entry["before"], "records": records,
                })
            put_metadata(connection, {
                "category_counts": json.dumps(tally["categories"], separators=(",", ":"), sort_keys=True),
                "primary_category_counts": json.dumps(
                    tally["primaryCategories"], separators=(",", ":"), sort_keys=True
                ),
                "catchup_cutoff": until_date,
                "cutoff": f"{until_date}T23:59:59Z",
                "refreshed_at": fetched_at,
            })
        connection.commit()
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite check failed after refresh: {integrity}")
        total = sum(tally["records"].values()) if caught_up else total_records(connection, source_rows(connection))
    connection.close()
    emit_progress("complete", total, total, "Corpus refresh complete.")
    return {
        "refreshed": refreshed,
        "skipped": skipped,
        "added": added,
        "records": total,
        "cutoff": f"{until_date}T23:59:59Z",
        "refreshedAt": fetched_at,
    }


def requested_profile_ids() -> list[str]:
    packed = os.environ.get("VANTA_MODULE_OPTION_PROFILE_IDS")
    return json.loads(packed) if packed else list(DEFAULT_PROFILE_IDS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    profile_source = parser.add_mutually_exclusive_group(required=True)
    profile_source.add_argument("--profile", action="append", type=Path, dest="profiles")
    profile_source.add_argument("--profile-dir", type=Path)
    parser.add_argument("--profile-id", action="append", dest="profile_ids", help="Repeat to compose several sources into one corpus.")
    parser.add_argument("--cutoff", help="Fixed UTC cutoff in YYYYMMDDHHMM format; primarily for deterministic recovery/tests.")
    parser.add_argument("--category", action="append", dest="categories")
    parser.add_argument("--refresh", action="store_true", help="Catch the activated corpus up in place instead of provisioning it.")
    parser.add_argument("--source", help="Restrict a refresh to one source identifier.")
    arguments = parser.parse_args()
    if arguments.refresh:
        profile_dir = arguments.profile_dir or arguments.profiles[0].parent
        print(json.dumps(refresh(arguments.data_dir, profile_dir, arguments.source, arguments.cutoff)), flush=True)
        return
    categories = arguments.categories
    if categories is None and os.environ.get("VANTA_MODULE_OPTION_CATEGORIES"):
        categories = json.loads(os.environ["VANTA_MODULE_OPTION_CATEGORIES"])
    profile_paths = arguments.profiles or resolve_profiles(arguments.profile_dir, arguments.profile_ids or requested_profile_ids())
    result = provision(
        arguments.data_dir,
        profile_paths,
        cutoff=arguments.cutoff,
        categories=categories,
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

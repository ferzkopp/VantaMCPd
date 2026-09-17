#!/usr/bin/env python3
"""Shared filesystem protocol for trusted artifact-consuming module brokers."""
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows development tests; deployed modules run on Linux.
    fcntl = None

PROTOCOL_VERSION = 1
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PRODUCER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
LOCAL_LOCK = threading.Lock()


def _iso(timestamp: float) -> str:
    """Format a Unix timestamp as a UTC ISO 8601 string."""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: str) -> dict[str, Any]:
    """Read a JSON object from a regular, non-symlink file."""
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("artifact metadata is missing or unsafe")
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("artifact metadata is invalid")
    return value


def _atomic_json(path: str, value: dict[str, Any]) -> None:
    """Durably replace a JSON metadata file without exposing a partial write."""
    descriptor, temporary = tempfile.mkstemp(prefix=".metadata-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o660)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _integer(value: Any, field: str, minimum: int, maximum: int | None = None) -> int:
    """Validate and return a non-boolean integer within inclusive bounds."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        suffix = f" to {maximum}" if maximum is not None else f" or greater"
        raise ValueError(f"{field} must be an integer from {minimum}{suffix}")
    return value


def _producer(value: Any) -> str:
    """Validate the producer identifier stored in quota and artifact metadata."""
    if not isinstance(value, str) or PRODUCER_PATTERN.fullmatch(value) is None:
        raise ValueError("artifact producer is invalid")
    return value


def _reservation_path(root: str, reservation_id: Any) -> str:
    """Resolve a validated reservation identifier beneath the store root."""
    if not isinstance(reservation_id, str) or ID_PATTERN.fullmatch(reservation_id) is None:
        raise ValueError("artifact reservationId is invalid")
    return os.path.join(root, ".reservations", reservation_id)


def marker(root: str) -> dict[str, Any]:
    """Return the compatible store marker and policy for ``root``.

    Raises ``ValueError`` when the marker is absent, unsafe, or uses another
    protocol version. Filesystem and JSON decoding errors propagate.
    """
    value = _read_json(os.path.join(root, ".store.json"))
    if value.get("protocolVersion") != PROTOCOL_VERSION or not isinstance(value.get("policy"), dict):
        raise ValueError("artifact store protocol or policy is unavailable")
    return value


def available(root: str | None) -> bool:
    """Return whether ``root`` exposes a readable, compatible store marker."""
    try:
        if not root:
            return False
        marker(root)
        return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


@contextlib.contextmanager
def locked(root: str) -> Iterator[None]:
    """Hold the store-wide advisory lock for a mutation or quota decision."""
    if fcntl is None:
        with LOCAL_LOCK:
            yield
        return
    with open(os.path.join(root, ".store.lock"), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def object_dir(root: str, artifact_id: str) -> str:
    """Return the sharded object directory for a validated artifact ID."""
    if not isinstance(artifact_id, str) or ID_PATTERN.fullmatch(artifact_id) is None:
        raise ValueError("artifactId is invalid")
    return os.path.join(root, "objects", artifact_id[:2], artifact_id)


def _artifact_record(root: str, artifact_id: str, max_bytes: int) -> tuple[str, dict[str, Any]]:
    """Validate an artifact's metadata, expiry, content type, and byte size."""
    max_bytes = _integer(max_bytes, "maxBytes", 0)
    directory = object_dir(root, artifact_id)
    metadata = _read_json(os.path.join(directory, "metadata.json"))
    source = os.path.join(directory, "content")
    if metadata.get("id") != artifact_id or os.path.islink(source) or not os.path.isfile(source):
        raise ValueError(f"artifact {artifact_id} is corrupt")
    if float(metadata.get("expiresEpoch", 0)) <= time.time():
        raise ValueError(f"artifact {artifact_id} has expired")
    size = int(metadata.get("bytes", -1))
    if size < 0 or size > max_bytes or os.path.getsize(source) != size:
        raise ValueError(f"artifact {artifact_id} exceeds {max_bytes} bytes or has the wrong size")
    return source, metadata


def verified_source(root: str, artifact_id: str, max_bytes: int) -> tuple[str, dict[str, Any]]:
    """Return an artifact's shared path and metadata after SHA-256 verification.

    Trusted brokers may read the returned path directly. Sandboxed workloads
    should instead receive a private copy made with :func:`copy_verified`.
    """
    marker(root)
    source, metadata = _artifact_record(root, artifact_id, max_bytes)
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != metadata.get("sha256"):
        raise ValueError(f"artifact {artifact_id} failed integrity verification")
    return source, metadata


def copy_verified(root: str, artifact_id: str, target: str, max_bytes: int) -> dict[str, Any]:
    """Copy and verify one artifact into a new read-only local file.

    ``target`` must not already exist. Any file created by a failed copy or
    integrity check is removed before the exception is re-raised.
    """
    marker(root)
    source, metadata = _artifact_record(root, artifact_id, max_bytes)
    digest = hashlib.sha256()
    created = False
    try:
        with open(source, "rb") as reader, open(target, "xb") as writer:
            created = True
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(chunk)
                writer.write(chunk)
        if digest.hexdigest() != metadata.get("sha256"):
            raise ValueError(f"artifact {artifact_id} failed integrity verification")
        os.chmod(target, 0o400)
        return metadata
    except Exception:
        if created:
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
        raise


def usage(root: str) -> tuple[int, dict[str, int]]:
    """Return reserved bytes store-wide and grouped by producer.

    Committed objects, active uploads, and compute reservations are included.
    Callers making a quota decision must hold :func:`locked` while using this
    snapshot.
    """
    total = 0
    producers: dict[str, int] = {}
    for base, metadata_name in (("objects", "metadata.json"), (".uploads", "upload.json")):
        parent = os.path.join(root, base)
        for current, directories, files in os.walk(parent, followlinks=False):
            directories[:] = [name for name in directories if not os.path.islink(os.path.join(current, name))]
            if metadata_name not in files:
                continue
            try:
                metadata = _read_json(os.path.join(current, metadata_name))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            size = int(metadata.get("bytes", metadata.get("declaredBytes", 0)))
            producer = str(metadata.get("producer", "unknown"))
            total += size
            producers[producer] = producers.get(producer, 0) + size
    for name in os.listdir(os.path.join(root, ".reservations")):
        try:
            metadata = _read_json(os.path.join(root, ".reservations", name))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        size = int(metadata.get("declaredBytes", 0))
        producer = str(metadata.get("producer", "unknown"))
        total += size
        producers[producer] = producers.get(producer, 0) + size
    return total, producers


def reserve(root: str, producer: str, declared_bytes: int) -> str:
    """Reserve quota and free space for a later atomic publication batch.

    Returns a private reservation ID for :func:`publish_reserved`. Call
    :func:`release` in a ``finally`` block when publication does not consume it.
    """
    producer = _producer(producer)
    declared_bytes = _integer(declared_bytes, "declaredBytes", 0)
    policy = marker(root)["policy"]
    with locked(root):
        total, producers = usage(root)
        if total + declared_bytes > int(policy["totalQuotaBytes"]):
            raise ValueError("artifact store total quota would be exceeded")
        if producers.get(producer, 0) + declared_bytes > int(policy["producerQuotaBytes"]):
            raise ValueError(f"artifact producer quota would be exceeded for {producer}")
        if shutil.disk_usage(root).free - declared_bytes < int(policy["freeReserveBytes"]):
            raise ValueError("artifact store free-space reserve would be crossed")
        reservation_id = uuid.uuid4().hex
        _atomic_json(os.path.join(root, ".reservations", reservation_id), {
            "id": reservation_id,
            "producer": producer,
            "declaredBytes": declared_bytes,
            "updatedEpoch": time.time(),
        })
    return reservation_id


def release(root: str | None, reservation_id: str | None) -> None:
    """Delete an unused reservation; missing reservations are harmless."""
    if not root or not reservation_id:
        return
    reservation_path = _reservation_path(root, reservation_id)
    try:
        with locked(root):
            os.unlink(reservation_path)
    except FileNotFoundError:
        pass


def _validate_publication(policy: dict[str, Any], source: Any, name: Any, mime_type: Any) -> int:
    """Validate one publication item and return its current source size."""
    if not isinstance(source, str) or os.path.islink(source) or not os.path.isfile(source):
        raise ValueError("artifact output is missing or unsafe")
    if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("artifact output name is invalid")
    if not isinstance(mime_type, str) or not 1 <= len(mime_type) <= 200 or any(character.isspace() for character in mime_type):
        raise ValueError("artifact output mimeType is invalid")
    size = os.path.getsize(source)
    if size > int(policy["maxArtifactBytes"]):
        raise ValueError("artifact output exceeds its reserved budget")
    return size


def _publish_locked(root: str, producer: str, items: list[dict[str, str]], retention_days: int, budget: Any, key: str) -> list[dict[str, Any]]:
    """Publish a validated batch while the caller holds the store lock."""
    policy = marker(root)["policy"]
    retention_days = _integer(retention_days, "retentionDays", 1, int(policy["maxRetentionDays"]))
    budget = _integer(budget, "budgetBytes", 0)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("artifact publication items must be a list of objects")
    sizes = [_validate_publication(policy, item.get("source"), item.get("name"), item.get("mimeType")) for item in items]
    if sum(sizes) > budget:
        raise ValueError("artifact output exceeds its reserved budget")
    published: list[dict[str, Any]] = []
    published_directories: list[str] = []
    staging_directories: list[str] = []
    try:
        for index, (item, size) in enumerate(zip(items, sizes)):
            artifact_id = uuid.uuid4().hex
            staging = os.path.join(root, ".uploads", f"publish-{key}-{index}")
            os.mkdir(staging, mode=0o2770)
            staging_directories.append(staging)
            os.chmod(staging, 0o2770)
            digest = hashlib.sha256()
            copied_bytes = 0
            content = os.path.join(staging, "content")
            with open(item["source"], "rb") as reader, open(content, "xb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    digest.update(chunk)
                    writer.write(chunk)
                    copied_bytes += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if copied_bytes != size:
                raise ValueError("artifact output changed during publication")
            os.chmod(content, 0o660)
            now = time.time()
            metadata = {
                "protocolVersion": PROTOCOL_VERSION,
                "id": artifact_id,
                "name": item["name"],
                "mimeType": item["mimeType"][:200],
                "producer": producer,
                "bytes": size,
                "sha256": digest.hexdigest(),
                "createdAt": _iso(now),
                "expiresAt": _iso(now + retention_days * 86400),
                "createdEpoch": now,
                "expiresEpoch": now + retention_days * 86400,
            }
            _atomic_json(os.path.join(staging, "metadata.json"), metadata)
            target = object_dir(root, artifact_id)
            parent = os.path.dirname(target)
            os.makedirs(parent, mode=0o2770, exist_ok=True)
            os.chmod(parent, 0o2770)
            os.replace(staging, target)
            staging_directories.remove(staging)
            published_directories.append(target)
            published.append(metadata)
        return published
    except Exception:
        for directory in staging_directories + published_directories:
            shutil.rmtree(directory, ignore_errors=True)
        raise


def publish_reserved(root: str, reservation_id: str, producer: str, items: list[dict[str, str]], retention_days: int) -> list[dict[str, Any]]:
    """Publish a batch against a matching reservation and consume it on success.

    Each item requires ``source``, ``name``, and ``mimeType`` strings. Published
    objects are removed if a later item fails; the reservation remains so the
    caller can retry or release it.
    """
    producer = _producer(producer)
    reservation_path = _reservation_path(root, reservation_id)
    with locked(root):
        reservation = _read_json(reservation_path)
        if reservation.get("id") != reservation_id or reservation.get("producer") != producer:
            raise ValueError("artifact reservation producer does not match")
        published = _publish_locked(root, producer, items, retention_days, reservation.get("declaredBytes"), reservation_id)
        os.unlink(reservation_path)
        return published


def publish_unreserved(root: str, producer: str, items: list[dict[str, str]], budget_bytes: int, retention_days: int) -> list[dict[str, Any]]:
    """Quota-check and publish a batch without creating a durable reservation.

    This is intended for short trusted operations that already produced their
    outputs. Long-running work should reserve capacity before it starts.
    """
    producer = _producer(producer)
    budget_bytes = _integer(budget_bytes, "budgetBytes", 0)
    policy = marker(root)["policy"]
    with locked(root):
        total, producers = usage(root)
        if total + budget_bytes > int(policy["totalQuotaBytes"]):
            raise ValueError("artifact store total quota would be exceeded")
        if producers.get(producer, 0) + budget_bytes > int(policy["producerQuotaBytes"]):
            raise ValueError(f"artifact producer quota would be exceeded for {producer}")
        if shutil.disk_usage(root).free - budget_bytes < int(policy["freeReserveBytes"]):
            raise ValueError("artifact store free-space reserve would be crossed")
        return _publish_locked(root, producer, items, retention_days, budget_bytes, uuid.uuid4().hex)


def self_test() -> None:
    """Run dependency-free validation checks used by module installers."""
    assert ID_PATTERN.fullmatch("a" * 32)
    assert not ID_PATTERN.fullmatch("../etc/passwd")
    assert PRODUCER_PATTERN.fullmatch("image-processing")
    assert not PRODUCER_PATTERN.fullmatch("../producer")
    assert NAME_PATTERN.fullmatch("result.webp")
    assert not NAME_PATTERN.fullmatch("../result.webp")


if __name__ == "__main__":
    self_test()

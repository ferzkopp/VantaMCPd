#!/usr/bin/env python3
"""Small artifact filesystem client for trusted Text Tools operations."""
import contextlib
import hashlib
import json
import os
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
LOCAL_LOCK = threading.Lock()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: str) -> dict[str, Any]:
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("artifact metadata is missing or unsafe")
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("artifact metadata is invalid")
    return value


def _atomic_json(path: str, value: dict[str, Any]) -> None:
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


def root() -> str:
    value = os.environ.get("VANTA_ARTIFACT_ROOT")
    if not value:
        raise ValueError("shared artifact storage is not configured for text-tools")
    marker = _read_json(os.path.join(value, ".store.json"))
    if marker.get("protocolVersion") != PROTOCOL_VERSION or not isinstance(marker.get("policy"), dict):
        raise ValueError("artifact store protocol or policy is unavailable")
    return value


def _marker(store_root: str) -> dict[str, Any]:
    return _read_json(os.path.join(store_root, ".store.json"))


@contextlib.contextmanager
def _locked(store_root: str) -> Iterator[None]:
    if fcntl is None:
        with LOCAL_LOCK:
            yield
        return
    with open(os.path.join(store_root, ".store.lock"), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def verified_source(store_root: str, artifact_id: str, max_bytes: int) -> tuple[str, dict[str, Any]]:
    if not isinstance(artifact_id, str) or len(artifact_id) != 32 or any(character not in "0123456789abcdef" for character in artifact_id):
        raise ValueError("artifactId is invalid")
    directory = os.path.join(store_root, "objects", artifact_id[:2], artifact_id)
    metadata = _read_json(os.path.join(directory, "metadata.json"))
    content = os.path.join(directory, "content")
    if metadata.get("id") != artifact_id or os.path.islink(content) or not os.path.isfile(content):
        raise ValueError("artifact is corrupt")
    size = int(metadata.get("bytes", -1))
    if size < 0 or size > max_bytes or os.path.getsize(content) != size:
        raise ValueError(f"artifact exceeds the {max_bytes}-byte CSV limit or has the wrong size")
    if float(metadata.get("expiresEpoch", 0)) <= time.time():
        raise ValueError("artifact has expired")
    digest = hashlib.sha256()
    with open(content, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != metadata.get("sha256"):
        raise ValueError("artifact failed integrity verification")
    return content, metadata


def _usage(store_root: str) -> tuple[int, dict[str, int]]:
    total = 0
    producers: dict[str, int] = {}
    for base, metadata_name in (("objects", "metadata.json"), (".uploads", "upload.json")):
        for current, directories, files in os.walk(os.path.join(store_root, base), followlinks=False):
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
    for name in os.listdir(os.path.join(store_root, ".reservations")):
        try:
            metadata = _read_json(os.path.join(store_root, ".reservations", name))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        size = int(metadata.get("declaredBytes", 0))
        producer = str(metadata.get("producer", "unknown"))
        total += size
        producers[producer] = producers.get(producer, 0) + size
    return total, producers


def publish_file(store_root: str, source: str, name: str, mime_type: str, budget_bytes: int, retention_days: int) -> dict[str, Any]:
    policy = _marker(store_root)["policy"]
    size = os.path.getsize(source)
    if size > budget_bytes or size > int(policy["maxArtifactBytes"]):
        raise ValueError("artifact output exceeds its reserved budget")
    if not 1 <= retention_days <= int(policy["maxRetentionDays"]):
        raise ValueError("artifact retention exceeds the configured maximum")
    producer = "text-tools"
    with _locked(store_root):
        total, producers = _usage(store_root)
        if total + budget_bytes > int(policy["totalQuotaBytes"]):
            raise ValueError("artifact store total quota would be exceeded")
        if producers.get(producer, 0) + budget_bytes > int(policy["producerQuotaBytes"]):
            raise ValueError("artifact producer quota would be exceeded for text-tools")
        if shutil.disk_usage(store_root).free - budget_bytes < int(policy["freeReserveBytes"]):
            raise ValueError("artifact store free-space reserve would be crossed")
        artifact_id = uuid.uuid4().hex
        staging = os.path.join(store_root, ".uploads", f"publish-{artifact_id}")
        os.mkdir(staging, mode=0o2770)
        os.chmod(staging, 0o2770)
        digest = hashlib.sha256()
        try:
            content = os.path.join(staging, "content")
            with open(source, "rb") as reader, open(content, "xb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    digest.update(chunk)
                    writer.write(chunk)
            os.chmod(content, 0o660)
            now = time.time()
            metadata = {
                "protocolVersion": PROTOCOL_VERSION,
                "id": artifact_id,
                "name": name[:128],
                "mimeType": mime_type,
                "producer": producer,
                "bytes": size,
                "sha256": digest.hexdigest(),
                "createdAt": _iso(now),
                "expiresAt": _iso(now + retention_days * 86400),
                "createdEpoch": now,
                "expiresEpoch": now + retention_days * 86400,
            }
            _atomic_json(os.path.join(staging, "metadata.json"), metadata)
            target = os.path.join(store_root, "objects", artifact_id[:2], artifact_id)
            parent = os.path.dirname(target)
            os.makedirs(parent, mode=0o2770, exist_ok=True)
            os.chmod(parent, 0o2770)
            os.replace(staging, target)
            return metadata
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
#!/usr/bin/env python3
"""Filesystem protocol used by the trusted Python Compute broker, never submitted code."""
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
NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
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


def _marker(root: str) -> dict[str, Any]:
    marker = _read_json(os.path.join(root, ".store.json"))
    if marker.get("protocolVersion") != PROTOCOL_VERSION or not isinstance(marker.get("policy"), dict):
        raise ValueError("artifact store protocol or policy is unavailable")
    return marker


@contextlib.contextmanager
def _locked(root: str) -> Iterator[None]:
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


def available(root: str | None) -> bool:
    try:
        if not root:
            return False
        _marker(root)
        return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _object_dir(root: str, artifact_id: str) -> str:
    if not isinstance(artifact_id, str) or not ID_PATTERN.fullmatch(artifact_id):
        raise ValueError("artifactId is invalid")
    return os.path.join(root, "objects", artifact_id[:2], artifact_id)


def stage_inputs(root: str, inputs: list[dict[str, str]], destination: str, max_bytes: int) -> dict[str, str]:
    _marker(root)
    os.makedirs(destination, mode=0o700)
    paths: dict[str, str] = {}
    total = 0
    for entry in inputs:
        artifact_id, name = entry["artifactId"], entry["name"]
        directory = _object_dir(root, artifact_id)
        metadata = _read_json(os.path.join(directory, "metadata.json"))
        source = os.path.join(directory, "content")
        if metadata.get("id") != artifact_id or os.path.islink(source) or not os.path.isfile(source):
            raise ValueError(f"artifact {artifact_id} is corrupt")
        if float(metadata.get("expiresEpoch", 0)) <= time.time():
            raise ValueError(f"artifact {artifact_id} has expired")
        size = int(metadata.get("bytes", -1))
        total += size
        if size < 0 or total > max_bytes:
            raise ValueError(f"artifact inputs exceed {max_bytes} bytes")
        digest = hashlib.sha256()
        target = os.path.join(destination, name)
        with open(source, "rb") as reader, open(target, "xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(chunk)
                writer.write(chunk)
        if os.path.getsize(target) != size or digest.hexdigest() != metadata.get("sha256"):
            raise ValueError(f"artifact {artifact_id} failed integrity verification")
        os.chmod(target, 0o400)
        paths[name] = f"/inputs/{name}"
    return paths


def _usage(root: str) -> tuple[int, dict[str, int]]:
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
    reservations = os.path.join(root, ".reservations")
    for name in os.listdir(reservations):
        try:
            metadata = _read_json(os.path.join(reservations, name))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        size = int(metadata.get("declaredBytes", 0))
        producer = str(metadata.get("producer", "unknown"))
        total += size
        producers[producer] = producers.get(producer, 0) + size
    return total, producers


def reserve(root: str, producer: str, declared_bytes: int) -> str:
    marker = _marker(root)
    policy = marker["policy"]
    with _locked(root):
        total, producers = _usage(root)
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


def release(root: str, reservation_id: str | None) -> None:
    if not reservation_id:
        return
    try:
        with _locked(root):
            os.unlink(os.path.join(root, ".reservations", reservation_id))
    except FileNotFoundError:
        pass


def publish(root: str, reservation_id: str, producer: str, artifacts: list[dict[str, Any]], workspace: str, retention_days: int) -> list[dict[str, Any]]:
    policy = _marker(root)["policy"]
    if not 1 <= retention_days <= int(policy["maxRetentionDays"]):
        raise ValueError("artifact retention exceeds the configured maximum")
    published: list[dict[str, Any]] = []
    published_directories: list[str] = []
    with _locked(root):
        reservation_path = os.path.join(root, ".reservations", reservation_id)
        reservation = _read_json(reservation_path)
        budget = int(reservation["declaredBytes"])
        total = 0
        try:
            for index, artifact in enumerate(artifacts):
                sandbox_path = str(artifact.get("path", ""))
                if not sandbox_path.startswith("/work/"):
                    raise ValueError("artifact output path escaped the workspace")
                source = os.path.join(workspace, *sandbox_path[6:].split("/"))
                if os.path.islink(source) or not os.path.isfile(source):
                    raise ValueError("artifact output is missing or unsafe")
                size = os.path.getsize(source)
                total += size
                if size > int(policy["maxArtifactBytes"]) or total > budget:
                    raise ValueError("artifact output exceeds its reserved budget")
                artifact_id = uuid.uuid4().hex
                staging = os.path.join(root, ".uploads", f"publish-{reservation_id}-{index}")
                os.mkdir(staging, mode=0o2770)
                os.chmod(staging, 0o2770)
                digest = hashlib.sha256()
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
                    "name": str(artifact.get("name", "artifact.bin"))[:128],
                    "mimeType": str(artifact.get("mimeType", "application/octet-stream"))[:200],
                    "producer": producer,
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                    "createdAt": _iso(now),
                    "expiresAt": _iso(now + retention_days * 86400),
                    "createdEpoch": now,
                    "expiresEpoch": now + retention_days * 86400,
                }
                _atomic_json(os.path.join(staging, "metadata.json"), metadata)
                target = _object_dir(root, artifact_id)
                parent = os.path.dirname(target)
                os.makedirs(parent, mode=0o2770, exist_ok=True)
                os.chmod(parent, 0o2770)
                os.replace(staging, target)
                published_directories.append(target)
                published.append(metadata)
            os.unlink(reservation_path)
        except Exception:
            for directory in published_directories:
                shutil.rmtree(directory, ignore_errors=True)
            raise
    return published
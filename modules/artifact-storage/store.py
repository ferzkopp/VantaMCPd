#!/usr/bin/env python3
"""Immutable shared artifact store backed by one filesystem."""
import base64
import binascii
import contextlib
import hashlib
import json
import mimetypes
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
MAX_CHUNK_BYTES = 512 * 1024
MAX_READ_BYTES = 512 * 1024
UPLOAD_TTL_SECONDS = 3600
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PRODUCER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_PATTERN = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,128}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LOCAL_LOCK = threading.Lock()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: str, value: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".metadata-", dir=directory)
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


def _read_json(path: str) -> dict[str, Any]:
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("artifact metadata is missing or unsafe")
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("artifact metadata is invalid")
    return value


def _bounded_string(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


class ArtifactStore:
    def __init__(
        self,
        root: str,
        total_quota_bytes: int,
        producer_quota_bytes: int,
        max_artifact_bytes: int,
        default_retention_days: int,
        max_retention_days: int,
        free_reserve_bytes: int,
    ) -> None:
        self.root = os.path.realpath(root)
        self.total_quota_bytes = total_quota_bytes
        self.producer_quota_bytes = producer_quota_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.default_retention_days = default_retention_days
        self.max_retention_days = max_retention_days
        self.free_reserve_bytes = free_reserve_bytes
        self.objects_dir = os.path.join(self.root, "objects")
        self.uploads_dir = os.path.join(self.root, ".uploads")
        self.reservations_dir = os.path.join(self.root, ".reservations")
        self.marker_path = os.path.join(self.root, ".store.json")
        self.lock_path = os.path.join(self.root, ".store.lock")

    def initialize(self) -> None:
        os.makedirs(self.root, mode=0o2770, exist_ok=True)
        os.makedirs(self.objects_dir, mode=0o2770, exist_ok=True)
        os.makedirs(self.uploads_dir, mode=0o2770, exist_ok=True)
        os.makedirs(self.reservations_dir, mode=0o2770, exist_ok=True)
        if os.path.islink(self.root) or os.path.islink(self.objects_dir) or os.path.islink(self.uploads_dir) or os.path.islink(self.reservations_dir):
            raise ValueError("artifact store paths must not be symbolic links")
        for directory in (self.root, self.objects_dir, self.uploads_dir, self.reservations_dir):
            os.chmod(directory, 0o2770)
        marker = {
            "protocolVersion": PROTOCOL_VERSION,
            "immutable": True,
            "createdAt": _iso(time.time()),
            "policy": {
                "totalQuotaBytes": self.total_quota_bytes,
                "producerQuotaBytes": self.producer_quota_bytes,
                "maxArtifactBytes": self.max_artifact_bytes,
                "defaultRetentionDays": self.default_retention_days,
                "maxRetentionDays": self.max_retention_days,
                "freeReserveBytes": self.free_reserve_bytes,
            },
        }
        if os.path.exists(self.marker_path):
            current = _read_json(self.marker_path)
            if current.get("protocolVersion") != PROTOCOL_VERSION:
                raise ValueError("artifact store protocol version is unsupported")
            marker["createdAt"] = current.get("createdAt", marker["createdAt"])
        _atomic_json(self.marker_path, marker)
        with open(self.lock_path, "a", encoding="utf-8"):
            pass
        os.chmod(self.lock_path, 0o660)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:
            with LOCAL_LOCK:
                yield
            return
        with open(self.lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _upload_dir(self, upload_id: str) -> str:
        return os.path.join(self.uploads_dir, _bounded_string(upload_id, "uploadId", ID_PATTERN))

    def _object_dir(self, artifact_id: str) -> str:
        safe_id = _bounded_string(artifact_id, "artifactId", ID_PATTERN)
        return os.path.join(self.objects_dir, safe_id[:2], safe_id)

    def _artifact(self, artifact_id: str) -> tuple[dict[str, Any], str]:
        directory = self._object_dir(artifact_id)
        if os.path.islink(directory) or not os.path.isdir(directory):
            raise ValueError("artifact was not found")
        metadata = _read_json(os.path.join(directory, "metadata.json"))
        content = os.path.join(directory, "content")
        if metadata.get("id") != artifact_id or os.path.islink(content) or not os.path.isfile(content):
            raise ValueError("artifact is corrupt")
        if float(metadata.get("expiresEpoch", 0)) <= time.time():
            raise ValueError("artifact has expired")
        return metadata, content

    def _records(self) -> Iterator[tuple[dict[str, Any], str]]:
        if not os.path.isdir(self.objects_dir):
            return
        for prefix in sorted(os.listdir(self.objects_dir)):
            prefix_path = os.path.join(self.objects_dir, prefix)
            if os.path.islink(prefix_path) or not os.path.isdir(prefix_path):
                continue
            for artifact_id in sorted(os.listdir(prefix_path)):
                directory = os.path.join(prefix_path, artifact_id)
                try:
                    metadata = _read_json(os.path.join(directory, "metadata.json"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if metadata.get("id") == artifact_id:
                    yield metadata, directory

    def _usage(self) -> tuple[int, dict[str, int]]:
        total = 0
        producers: dict[str, int] = {}
        for metadata, _directory in self._records():
            size = int(metadata.get("bytes", 0))
            producer = str(metadata.get("producer", "unknown"))
            total += size
            producers[producer] = producers.get(producer, 0) + size
        for upload_id in os.listdir(self.uploads_dir):
            try:
                metadata = _read_json(os.path.join(self._upload_dir(upload_id), "upload.json"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            size = int(metadata.get("declaredBytes", 0))
            producer = str(metadata.get("producer", "unknown"))
            total += size
            producers[producer] = producers.get(producer, 0) + size
        for reservation_id in os.listdir(self.reservations_dir):
            try:
                metadata = _read_json(os.path.join(self.reservations_dir, reservation_id))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            size = int(metadata.get("declaredBytes", 0))
            producer = str(metadata.get("producer", "unknown"))
            total += size
            producers[producer] = producers.get(producer, 0) + size
        return total, producers

    def begin(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = _bounded_string(arguments.get("name"), "name", NAME_PATTERN)
        producer = _bounded_string(arguments.get("producer", "agent"), "producer", PRODUCER_PATTERN)
        size = _integer(arguments.get("bytes"), "bytes", 0, self.max_artifact_bytes)
        mime_type = arguments.get("mimeType") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        if not isinstance(mime_type, str) or not 1 <= len(mime_type) <= 200 or any(character.isspace() for character in mime_type):
            raise ValueError("mimeType is invalid")
        retention_days = _integer(arguments.get("retentionDays", self.default_retention_days), "retentionDays", 1, self.max_retention_days)
        expected_sha256 = arguments.get("sha256")
        if expected_sha256 is not None:
            expected_sha256 = _bounded_string(expected_sha256, "sha256", SHA256_PATTERN)
        now = time.time()
        with self._locked():
            self._gc_locked(now)
            total, producers = self._usage()
            if total + size > self.total_quota_bytes:
                raise ValueError("artifact store total quota would be exceeded")
            if producers.get(producer, 0) + size > self.producer_quota_bytes:
                raise ValueError(f"artifact producer quota would be exceeded for {producer}")
            free = shutil.disk_usage(self.root).free
            if free - size < self.free_reserve_bytes:
                raise ValueError("artifact store free-space reserve would be crossed")
            upload_id = uuid.uuid4().hex
            directory = self._upload_dir(upload_id)
            os.mkdir(directory, mode=0o2770)
            with open(os.path.join(directory, "content"), "xb"):
                pass
            metadata = {
                "protocolVersion": PROTOCOL_VERSION,
                "uploadId": upload_id,
                "name": name,
                "mimeType": mime_type,
                "producer": producer,
                "declaredBytes": size,
                "receivedBytes": 0,
                "expectedSha256": expected_sha256,
                "retentionDays": retention_days,
                "createdEpoch": now,
                "updatedEpoch": now,
            }
            _atomic_json(os.path.join(directory, "upload.json"), metadata)
        return {"uploadId": upload_id, "nextOffset": 0, "declaredBytes": size, "chunkLimitBytes": MAX_CHUNK_BYTES}

    def append(self, arguments: dict[str, Any]) -> dict[str, Any]:
        directory = self._upload_dir(arguments.get("uploadId"))
        offset = _integer(arguments.get("offset"), "offset", 0, self.max_artifact_bytes)
        encoded = arguments.get("data")
        if not isinstance(encoded, str) or len(encoded) > (MAX_CHUNK_BYTES * 4 // 3) + 8:
            raise ValueError("data is too large")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("data must be strict base64") from error
        if len(payload) > MAX_CHUNK_BYTES:
            raise ValueError(f"decoded chunk exceeds {MAX_CHUNK_BYTES} bytes")
        chunk_sha256 = arguments.get("sha256")
        if chunk_sha256 is not None and hashlib.sha256(payload).hexdigest() != _bounded_string(chunk_sha256, "sha256", SHA256_PATTERN):
            raise ValueError("chunk SHA-256 does not match")
        with self._locked():
            metadata_path = os.path.join(directory, "upload.json")
            metadata = _read_json(metadata_path)
            if metadata.get("receivedBytes") != offset:
                raise ValueError(f"offset must be {metadata.get('receivedBytes')}")
            if offset + len(payload) > metadata.get("declaredBytes", -1):
                raise ValueError("chunk exceeds the declared artifact size")
            content = os.path.join(directory, "content")
            if os.path.islink(content) or not os.path.isfile(content):
                raise ValueError("upload content is unsafe")
            with open(content, "ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            metadata["receivedBytes"] = offset + len(payload)
            metadata["updatedEpoch"] = time.time()
            _atomic_json(metadata_path, metadata)
        return {"uploadId": metadata["uploadId"], "receivedBytes": metadata["receivedBytes"], "nextOffset": metadata["receivedBytes"]}

    def commit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        directory = self._upload_dir(arguments.get("uploadId"))
        with self._locked():
            metadata = _read_json(os.path.join(directory, "upload.json"))
            content = os.path.join(directory, "content")
            if metadata.get("receivedBytes") != metadata.get("declaredBytes") or os.path.getsize(content) != metadata.get("declaredBytes"):
                raise ValueError("upload is incomplete")
            digest = hashlib.sha256()
            with open(content, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
            if metadata.get("expectedSha256") and metadata["expectedSha256"] != sha256:
                raise ValueError("artifact SHA-256 does not match")
            artifact_id = uuid.uuid4().hex
            now = time.time()
            committed = {
                "protocolVersion": PROTOCOL_VERSION,
                "id": artifact_id,
                "name": metadata["name"],
                "mimeType": metadata["mimeType"],
                "producer": metadata["producer"],
                "bytes": metadata["declaredBytes"],
                "sha256": sha256,
                "createdAt": _iso(now),
                "expiresAt": _iso(now + metadata["retentionDays"] * 86400),
                "createdEpoch": now,
                "expiresEpoch": now + metadata["retentionDays"] * 86400,
            }
            os.unlink(os.path.join(directory, "upload.json"))
            _atomic_json(os.path.join(directory, "metadata.json"), committed)
            parent = os.path.dirname(self._object_dir(artifact_id))
            os.makedirs(parent, mode=0o2770, exist_ok=True)
            os.replace(directory, self._object_dir(artifact_id))
        return committed

    def abort(self, arguments: dict[str, Any]) -> dict[str, Any]:
        directory = self._upload_dir(arguments.get("uploadId"))
        with self._locked():
            removed = os.path.isdir(directory) and not os.path.islink(directory)
            if removed:
                shutil.rmtree(directory)
        return {"uploadId": arguments.get("uploadId"), "removed": removed}

    def info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        metadata, _content = self._artifact(arguments.get("artifactId"))
        return dict(metadata)

    def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        artifact_id = arguments.get("artifactId")
        metadata, content = self._artifact(artifact_id)
        offset = _integer(arguments.get("offset", 0), "offset", 0, metadata["bytes"])
        length = _integer(arguments.get("length", MAX_READ_BYTES), "length", 1, MAX_READ_BYTES)
        with open(content, "rb") as handle:
            handle.seek(offset)
            payload = handle.read(min(length, metadata["bytes"] - offset))
        return {
            "artifactId": artifact_id,
            "offset": offset,
            "bytes": len(payload),
            "nextOffset": offset + len(payload),
            "eof": offset + len(payload) >= metadata["bytes"],
            "encoding": "base64",
            "data": base64.b64encode(payload).decode("ascii"),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = _integer(arguments.get("limit", 50), "limit", 1, 200)
        cursor = arguments.get("cursor", "")
        if not isinstance(cursor, str) or (cursor and not ID_PATTERN.fullmatch(cursor)):
            raise ValueError("cursor is invalid")
        producer = arguments.get("producer")
        if producer is not None:
            producer = _bounded_string(producer, "producer", PRODUCER_PATTERN)
        now = time.time()
        records = [metadata for metadata, _directory in self._records() if metadata["id"] > cursor and metadata.get("expiresEpoch", 0) > now and (producer is None or metadata.get("producer") == producer)]
        records.sort(key=lambda item: item["id"])
        page = records[:limit]
        total, producers = self._usage()
        return {
            "artifacts": page,
            "nextCursor": page[-1]["id"] if len(records) > limit else None,
            "usage": {"reservedBytes": total, "totalQuotaBytes": self.total_quota_bytes, "producerBytes": producers},
        }

    def delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("confirm") is not True:
            raise ValueError("confirm must be true to delete an artifact")
        artifact_id = _bounded_string(arguments.get("artifactId"), "artifactId", ID_PATTERN)
        directory = self._object_dir(artifact_id)
        with self._locked():
            removed = os.path.isdir(directory) and not os.path.islink(directory)
            if removed:
                metadata = _read_json(os.path.join(directory, "metadata.json"))
                if metadata.get("id") != artifact_id:
                    raise ValueError("artifact marker does not match")
                shutil.rmtree(directory)
        return {"artifactId": artifact_id, "removed": removed}

    def update(self, arguments: dict[str, Any]) -> dict[str, Any]:
        artifact_id = _bounded_string(arguments.get("artifactId"), "artifactId", ID_PATTERN)
        retention_days = _integer(arguments.get("retentionDays"), "retentionDays", 1, self.max_retention_days)
        with self._locked():
            metadata, _content = self._artifact(artifact_id)
            requested_expiry = time.time() + retention_days * 86400
            maximum_expiry = metadata["createdEpoch"] + self.max_retention_days * 86400
            metadata["expiresEpoch"] = min(requested_expiry, maximum_expiry)
            metadata["expiresAt"] = _iso(metadata["expiresEpoch"])
            _atomic_json(os.path.join(self._object_dir(artifact_id), "metadata.json"), metadata)
        return metadata

    def gc(self) -> dict[str, int]:
        with self._locked():
            return self._gc_locked(time.time())

    def _gc_locked(self, now: float) -> dict[str, int]:
        artifacts = 0
        uploads = 0
        for metadata, directory in list(self._records()):
            if float(metadata.get("expiresEpoch", 0)) <= now and not os.path.islink(directory):
                shutil.rmtree(directory)
                artifacts += 1
        for upload_id in list(os.listdir(self.uploads_dir)):
            directory = self._upload_dir(upload_id)
            try:
                metadata = _read_json(os.path.join(directory, "upload.json"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if float(metadata.get("updatedEpoch", 0)) + UPLOAD_TTL_SECONDS <= now and not os.path.islink(directory):
                shutil.rmtree(directory)
                uploads += 1
        for reservation_id in list(os.listdir(self.reservations_dir)):
            path = os.path.join(self.reservations_dir, reservation_id)
            try:
                metadata = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if float(metadata.get("updatedEpoch", 0)) + UPLOAD_TTL_SECONDS <= now and not os.path.islink(path):
                os.unlink(path)
                uploads += 1
        return {"artifactsRemoved": artifacts, "uploadsRemoved": uploads}


def from_environment() -> ArtifactStore:
    mib = 1024 * 1024
    return ArtifactStore(
        os.environ["VANTA_ARTIFACT_ROOT"],
        int(os.environ.get("VANTA_ARTIFACT_TOTAL_QUOTA_MB", "10240")) * mib,
        int(os.environ.get("VANTA_ARTIFACT_PRODUCER_QUOTA_MB", "2048")) * mib,
        int(os.environ.get("VANTA_ARTIFACT_MAX_ARTIFACT_MB", "512")) * mib,
        int(os.environ.get("VANTA_ARTIFACT_RETENTION_DAYS", "7")),
        int(os.environ.get("VANTA_ARTIFACT_MAX_RETENTION_DAYS", "90")),
        int(os.environ.get("VANTA_ARTIFACT_FREE_RESERVE_MB", "512")) * mib,
    )


def self_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = ArtifactStore(directory, 4096, 4096, 2048, 7, 90, 0)
        store.initialize()
        payload = b"name,value\nalpha,1\n"
        upload = store.begin({"name": "sample.csv", "bytes": len(payload), "mimeType": "text/csv", "sha256": hashlib.sha256(payload).hexdigest()})
        first = payload[:8]
        store.append({"uploadId": upload["uploadId"], "offset": 0, "data": base64.b64encode(first).decode()})
        store.append({"uploadId": upload["uploadId"], "offset": len(first), "data": base64.b64encode(payload[8:]).decode()})
        artifact = store.commit({"uploadId": upload["uploadId"]})
        assert artifact["bytes"] == len(payload)
        result = store.read({"artifactId": artifact["id"], "offset": 5, "length": 4})
        assert base64.b64decode(result["data"]) == payload[5:9]
        assert store.info({"artifactId": artifact["id"]})["sha256"] == hashlib.sha256(payload).hexdigest()
        assert store.list({})["artifacts"][0]["id"] == artifact["id"]
        try:
            store.delete({"artifactId": artifact["id"]})
            raise AssertionError("delete accepted without confirmation")
        except ValueError as error:
            assert "confirm" in str(error)
        assert store.delete({"artifactId": artifact["id"], "confirm": True})["removed"] is True


if __name__ == "__main__":
    self_test()
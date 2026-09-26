#!/usr/bin/env python3
"""HTTP retrieval, throttling, and progress helpers shared by every corpus source adapter."""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_REQUEST_INTERVAL_SECONDS = 3.0
MAX_REQUEST_ATTEMPTS = 6
MAX_RETRY_DELAY_SECONDS = 300.0
DEFAULT_USER_AGENT = "VantaMCPd/0.1 corpus-search local metadata index"
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
PROGRESS_INTERVAL_BYTES = 64 * 1024 * 1024


def user_agent() -> str:
    return os.environ.get("VANTA_CORPUS_USER_AGENT") or os.environ.get("VANTA_ARXIV_USER_AGENT", DEFAULT_USER_AGENT)


_progress_stream: Any = sys.stdout


def route_progress(stream: Any) -> None:
    """Redirect progress markers, which a process serving MCP must keep off its stdout."""
    global _progress_stream
    _progress_stream = stream


def emit_progress(phase: str, current: int, total: int, message: str, unit: str = "records") -> None:
    print(
        "VANTA_PROGRESS " + json.dumps({"phase": phase, "current": current, "total": total, "unit": unit, "message": message}),
        file=_progress_stream,
        flush=True,
    )


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def iso_timestamp(value: Any) -> str:
    """Normalize an RFC 2822 or ISO 8601 date to a UTC ISO 8601 string, or '' when unusable."""
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


class ThrottledClient:
    """Serializes requests to one host and retries throttling and server errors with bounded backoff."""

    def __init__(self, opener: Callable[..., Any] = urllib.request.urlopen, sleeper: Callable[[float], None] = time.sleep):
        self.opener = opener
        self.sleeper = sleeper
        self.last_request = 0.0

    def request(self, request: urllib.request.Request, timeout: int = 60, maximum_bytes: int = MAX_RESPONSE_BYTES, label: str = "response") -> bytes:
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self.last_request)
            if wait > 0:
                self.sleeper(wait)
            self.last_request = time.monotonic()
            try:
                with self.opener(request, timeout=timeout) as response:
                    payload = response.read(maximum_bytes + 1)
                if len(payload) > maximum_bytes:
                    raise ValueError(f"{label} exceeds {maximum_bytes // (1024 * 1024)} MiB")
                return payload
            except Exception as error:
                if isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code != 429:
                    raise
                if attempt == MAX_REQUEST_ATTEMPTS - 1:
                    raise
                self.sleeper(backoff_delay(error, attempt))
        raise AssertionError("unreachable")


def backoff_delay(error: Exception, attempt: int) -> float:
    """Honour Retry-After when the server sends one, otherwise back off exponentially within bounds."""
    retry_after = None
    if isinstance(error, urllib.error.HTTPError):
        value = error.headers.get("Retry-After") if error.headers else None
        try:
            retry_after = float(value) if value is not None else None
        except ValueError:
            retry_after = None
    if isinstance(error, urllib.error.HTTPError):
        delay = retry_after if retry_after is not None else 30.0 * (2 ** attempt)
    else:
        delay = MIN_REQUEST_INTERVAL_SECONDS * (attempt + 1)
    return min(max(delay, MIN_REQUEST_INTERVAL_SECONDS), MAX_RETRY_DELAY_SECONDS)


def download_file(
    destination: Path,
    url: str,
    message: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    phase: str = "download",
) -> dict[str, Any]:
    """Fetch `url` to `destination`, resuming a partial transfer and revalidating a complete one.

    Returns the recorded HTTP metadata, which doubles as the source's content identity.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    metadata_path = destination.with_name(destination.name + ".http.json")
    metadata: dict[str, Any] = {}
    if destination.exists() and metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
    headers = {"User-Agent": user_agent()}
    if destination.exists():
        if metadata.get("etag"):
            headers["If-None-Match"] = metadata["etag"]
        if metadata.get("lastModified"):
            headers["If-Modified-Since"] = metadata["lastModified"]
    elif partial.exists() and partial.stat().st_size:
        headers["Range"] = f"bytes={partial.stat().st_size}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = opener(request, timeout=300)
    except urllib.error.HTTPError as error:
        if error.code == 304 and destination.exists() and metadata:
            return metadata
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
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if downloaded % PROGRESS_INTERVAL_BYTES < len(chunk):
                    emit_progress(phase, downloaded, total, message, "bytes")
        metadata = {
            "etag": response.headers.get("ETag"),
            "lastModified": response.headers.get("Last-Modified"),
            "url": response.geturl(),
            "bytes": downloaded,
        }
    os.replace(partial, destination)
    atomic_json(metadata_path, metadata)
    return metadata


def content_identity(metadata: dict[str, Any]) -> str:
    """Derive a stable identity for a downloaded artefact from whatever the server disclosed."""
    return "|".join(str(metadata.get(key) or "") for key in ("etag", "lastModified", "bytes"))

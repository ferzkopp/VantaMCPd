#!/usr/bin/env python3
"""Bounded HTTP(S) file downloads published directly into shared artifact storage."""
import hashlib
import html
import http.client
import math
import os
import random
import re
import socket
import ssl
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

from artifact_protocol import NAME_PATTERN, available, marker, publish_unreserved, verified_source
from browser import VERSION
from network_policy import sanitize_url, validate_browser_url

PRODUCER = "browser-retrieval"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MAX_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 35_000
DEFAULT_BACKGROUND_TIMEOUT_MS = 300_000
MAX_BACKGROUND_TIMEOUT_MS = 600_000
# Bounds one socket read, so a slow-dripping server overruns timeoutMs by at most this much.
READ_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 4
MAX_BACKGROUND_ATTEMPTS = 8
MAX_RETRY_AFTER_SECONDS = 10
MAX_BACKGROUND_RETRY_AFTER_SECONDS = 300
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0
BACKGROUND_BACKOFF_CAP_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 900
REUSE_SECONDS = 900
MAX_REUSE_ENTRIES = 256
BODY_EXCERPT_BYTES = 4096
BODY_EXCERPT_CHARACTERS = 300
CHUNK_BYTES = 64 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
RETRY_STATUSES = {429, 502, 503, 504}
# Only these tell the client to slow down; 502 and 504 are treated as transient gateway failures.
COOLDOWN_STATUSES = {429, 503}
CONTACT_PATTERN = re.compile(r"^[A-Za-z0-9._%+@:/?#=&~-]{3,200}$")
MIME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
MARKUP_PATTERN = re.compile(r"<(script|style)\b.*?</\1\s*>|<[^>]*>", re.IGNORECASE | re.DOTALL)
FILE_TYPES = {
    "pdf": ("application/pdf", ".pdf"),
    "png": ("image/png", ".png"),
    "jpeg": ("image/jpeg", ".jpg"),
    "gif": ("image/gif", ".gif"),
    "webp": ("image/webp", ".webp"),
    "tiff": ("image/tiff", ".tif"),
}
ARGUMENTS = {"url", "name", "expectedTypes", "maxBytes", "timeoutMs", "retentionDays", "reuse"}

# The broker is a long-lived service, so throttling state and recent downloads persist across calls.
STATE_LOCK = threading.Lock()
HOST_COOLDOWNS: dict[str, dict[str, Any]] = {}
RECENT_DOWNLOADS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


class DownloadError(ValueError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = {key: value for key, value in details.items() if value is not None}


def user_agent() -> str:
    product = f"VantaMCPd-browser-retrieval/{VERSION}"
    contact = os.environ.get("VANTA_BROWSER_DOWNLOAD_CONTACT", "")
    return f"{product} (+{contact})" if CONTACT_PATTERN.fullmatch(contact) else product


def _integer(arguments: dict[str, Any], field: str, default: int | None, minimum: int, maximum: int) -> int | None:
    value = arguments.get(field, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DownloadError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def validate_download(arguments: Any, background: bool = False) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise DownloadError("web_download arguments must be an object")
    unknown = sorted(set(arguments) - ARGUMENTS)
    if unknown:
        raise DownloadError(f"unknown web_download arguments: {', '.join(unknown)}")
    name = arguments.get("name")
    if name is not None and (not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None):
        raise DownloadError("name must be 1-128 letters, digits, '.', '_' or '-' and start and end with a letter or digit")
    expected = arguments.get("expectedTypes")
    if expected is not None and (
        not isinstance(expected, list)
        or not expected
        or len(set(map(str, expected))) != len(expected)
        or any(item not in FILE_TYPES for item in expected)
    ):
        raise DownloadError(f"expectedTypes must list distinct values from: {', '.join(FILE_TYPES)}")
    reuse = arguments.get("reuse", True)
    if not isinstance(reuse, bool):
        raise DownloadError("reuse must be a boolean")
    timeout = arguments.get("timeoutMs")
    if not background and isinstance(timeout, int) and not isinstance(timeout, bool) and MAX_TIMEOUT_MS < timeout <= MAX_BACKGROUND_TIMEOUT_MS:
        raise DownloadError(f"timeoutMs above {MAX_TIMEOUT_MS} requires execution \"background\", which allows up to {MAX_BACKGROUND_TIMEOUT_MS}")
    return {
        "url": validate_browser_url(arguments.get("url")),
        "name": name,
        "expectedTypes": expected,
        "maxBytes": _integer(arguments, "maxBytes", DEFAULT_MAX_BYTES, 1, MAX_BYTES),
        "timeoutMs": _integer(
            arguments, "timeoutMs",
            DEFAULT_BACKGROUND_TIMEOUT_MS if background else DEFAULT_TIMEOUT_MS,
            1000, MAX_BACKGROUND_TIMEOUT_MS if background else MAX_TIMEOUT_MS,
        ),
        "retentionDays": _integer(arguments, "retentionDays", None, 1, 90),
        "reuse": reuse,
    }


def detect_type(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    return None


def _mime(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.split(";", 1)[0].strip().lower()
    return candidate if MIME_PATTERN.fullmatch(candidate) else None


def _disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    message = Message()
    try:
        message["content-disposition"] = value
        filename = message.get_filename()
    except (TypeError, ValueError, LookupError):
        return None
    return filename if isinstance(filename, str) else None


def artifact_name(requested: str | None, disposition: str | None, url: str, detected: str | None) -> str:
    if requested:
        return requested
    extension = FILE_TYPES[detected][1] if detected else ""
    raw = _disposition_filename(disposition) or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"_{2,}", "_", re.sub(r"[^A-Za-z0-9._-]+", "_", raw)).strip("._-")
    if not cleaned:
        cleaned = "download"
    if extension and "." not in cleaned:
        cleaned += extension
    if len(cleaned) > 128:
        stem, dot, suffix = cleaned.rpartition(".")
        if dot and 1 <= len(suffix) <= 10:
            cleaned = f"{stem[:127 - len(suffix)].rstrip('._-')}.{suffix}"
        else:
            cleaned = cleaned[:128].rstrip("._-")
    return cleaned if NAME_PATTERN.fullmatch(cleaned) else f"download{extension}"


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, math.ceil((when - datetime.now(timezone.utc)).total_seconds()))


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DownloadError("download exceeded timeoutMs")
    return remaining


def _host(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}"


def _cooldown_remaining(host: str) -> tuple[float, int | None]:
    with STATE_LOCK:
        entry = HOST_COOLDOWNS.get(host)
        if entry is None:
            return 0.0, None
        remaining = entry["until"] - time.monotonic()
        if remaining <= 0:
            del HOST_COOLDOWNS[host]
            return 0.0, None
        return remaining, entry["status"]


def _start_cooldown(host: str, seconds: float, status: int) -> None:
    until = time.monotonic() + min(seconds, MAX_COOLDOWN_SECONDS)
    with STATE_LOCK:
        entry = HOST_COOLDOWNS.get(host)
        if entry is None or entry["until"] < until:
            HOST_COOLDOWNS[host] = {"until": until, "status": status}


def _clear_cooldown(host: str) -> None:
    with STATE_LOCK:
        HOST_COOLDOWNS.pop(host, None)


def _backoff(failures: int, cap: float) -> float:
    ceiling = min(cap, BACKOFF_BASE_SECONDS * 2 ** max(0, failures - 1))
    return round(ceiling / 2 + random.uniform(0, ceiling / 2), 1)


def _transient(error: BaseException) -> bool:
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == getattr(socket, "EAI_AGAIN", None)
    return isinstance(error, (ConnectionError, TimeoutError, ssl.SSLError, http.client.IncompleteRead, http.client.BadStatusLine))


def _wait(seconds: float, abandoned: Callable[[], bool] | None) -> None:
    end = time.monotonic() + seconds
    while (remaining := end - time.monotonic()) > 0:
        if abandoned is not None and abandoned():
            raise DownloadError("web_download was cancelled while waiting to retry")
        time.sleep(min(remaining, 1.0))


def _request(url: str, deadline: float, agent: str, extra: dict[str, str] | None = None) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    parts = urlsplit(url)
    timeout = min(_remaining(deadline), READ_TIMEOUT_SECONDS)
    if parts.scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=timeout, context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    target = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        target += "?" + quote(parts.query, safe="/%:@!$&'()*+,;=?-._~")
    try:
        connection.request("GET", target, headers={"User-Agent": agent, "Accept": "*/*", "Accept-Encoding": "identity", **(extra or {})})
        return connection, connection.getresponse()
    except BaseException:
        connection.close()
        raise


def _body_excerpt(response: http.client.HTTPResponse) -> str | None:
    """A short plain-text preview of an error page, which often says why a request was refused."""
    kind = _mime(response.getheader("Content-Type")) or ""
    if not (kind.startswith("text/") or kind.endswith(("json", "xml"))):
        return None
    try:
        raw = response.read(BODY_EXCERPT_BYTES)
    except (OSError, http.client.HTTPException):
        return None
    text = html.unescape(MARKUP_PATTERN.sub(" ", raw.decode("utf-8", "replace")))
    text = " ".join("".join(character if character.isprintable() else " " for character in text).split())
    return text[:BODY_EXCERPT_CHARACTERS] or None


def _range_validator(response: http.client.HTTPResponse) -> str | None:
    """The If-Range value that makes resuming safe, or None when the server cannot resume this body."""
    if "bytes" not in (response.getheader("Accept-Ranges") or "").lower():
        return None
    etag = (response.getheader("ETag") or "").strip()
    validator = etag if etag and not etag.startswith("W/") else (response.getheader("Last-Modified") or "").strip()
    return validator if 0 < len(validator) <= 200 else None


def _content_range_start(response: http.client.HTTPResponse) -> int | None:
    match = CONTENT_RANGE_PATTERN.fullmatch((response.getheader("Content-Range") or "").strip())
    return int(match.group(1)) if match else None


class _Transfer:
    """One response body, possibly assembled from a first response and ranged continuations."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.restart()

    def restart(self) -> None:
        self.digest = hashlib.sha256()
        self.size = 0
        self.head = b""
        with open(self.target, "wb"):
            pass

    def stream(self, response: http.client.HTTPResponse, limit: int, deadline: float, expected: int | None) -> None:
        received = 0
        with open(self.target, "ab") as handle:
            while True:
                _remaining(deadline)
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                if self.size + len(chunk) > limit:
                    raise DownloadError(f"download exceeds the {limit}-byte limit")
                if len(self.head) < 16:
                    self.head += chunk[:16 - len(self.head)]
                self.digest.update(chunk)
                handle.write(chunk)
                self.size += len(chunk)
                received += len(chunk)
        # http.client returns b"" instead of raising when a server closes before Content-Length is met.
        if expected is not None and received != expected:
            raise http.client.IncompleteRead(b"", max(0, expected - received))


def _remember(url: str, result: dict[str, Any]) -> None:
    with STATE_LOCK:
        RECENT_DOWNLOADS[url] = {"storedAt": time.monotonic(), "result": dict(result)}
        RECENT_DOWNLOADS.move_to_end(url)
        while len(RECENT_DOWNLOADS) > MAX_REUSE_ENTRIES:
            RECENT_DOWNLOADS.popitem(last=False)


def _reusable(options: dict[str, Any], root: str, limit: int, retention_days: int, started: float) -> dict[str, Any] | None:
    if not options["reuse"]:
        return None
    with STATE_LOCK:
        entry = RECENT_DOWNLOADS.get(options["url"])
    if entry is None or time.monotonic() - entry["storedAt"] > REUSE_SECONDS:
        return None
    previous = entry["result"]
    expected = options["expectedTypes"]
    if (expected and previous["detectedType"] not in expected) or (options["name"] and options["name"] != previous["artifact"]["name"]) or previous["bytes"] > limit:
        return None
    try:
        _source, metadata = verified_source(root, previous["artifactId"], limit)
    except (OSError, ValueError):
        with STATE_LOCK:
            RECENT_DOWNLOADS.pop(options["url"], None)
        return None
    # A reused artifact must live about as long as a fresh download would.
    if float(metadata.get("expiresEpoch", 0)) < time.time() + retention_days * 86400 - REUSE_SECONDS:
        return None
    return {
        **previous,
        "artifact": metadata,
        "reused": True,
        "attempts": 0,
        "retries": [],
        "resumed": 0,
        "retriedAfterSeconds": None,
        "elapsedMs": round((time.monotonic() - started) * 1000),
        "warnings": previous["warnings"] + [f"Reused the artifact downloaded at {previous['fetchedAt']}; pass reuse=false to fetch the URL again."],
    }


def download(arguments: Any, root: str | None = None, background: bool = False, abandoned: Callable[[], bool] | None = None) -> dict[str, Any]:
    options = validate_download(arguments, background)
    root = root if root is not None else os.environ.get("VANTA_ARTIFACT_ROOT")
    if not available(root):
        raise DownloadError("shared artifact storage is unavailable on this node")
    policy = marker(root)["policy"]
    limit = min(options["maxBytes"], int(policy["maxArtifactBytes"]))
    retention_days = options["retentionDays"] or int(policy.get("defaultRetentionDays", 7))
    started = time.monotonic()
    reused = _reusable(options, root, limit, retention_days, started)
    if reused is not None:
        return reused
    agent = user_agent()
    deadline = started + options["timeoutMs"] / 1000
    max_attempts = MAX_BACKGROUND_ATTEMPTS if background else MAX_ATTEMPTS
    max_wait = MAX_BACKGROUND_RETRY_AFTER_SECONDS if background else MAX_RETRY_AFTER_SECONDS
    backoff_cap = BACKGROUND_BACKOFF_CAP_SECONDS if background else BACKOFF_CAP_SECONDS
    current = options["url"]
    redirects = 0
    requests = 0
    failures = 0
    resumed = 0
    retries: list[dict[str, Any]] = []
    retried_after: int | None = None

    def fits(delay: float) -> bool:
        return delay <= max_wait and time.monotonic() + delay < deadline

    def failure(message: str, **details: Any) -> DownloadError:
        return DownloadError(message, host=_host(current), url=sanitize_url(current), attempts=requests, retries=retries or None, **details)

    try:
        with tempfile.TemporaryDirectory(prefix="vanta-download-") as workspace:
            transfer = _Transfer(os.path.join(workspace, "content"))
            validator: str | None = None
            while True:
                host = _host(current)
                cooling, cooling_status = _cooldown_remaining(host)
                if cooling > 0:
                    delay = math.ceil(cooling)
                    if not fits(delay):
                        raise failure(f"{host} asked clients to slow down (HTTP {cooling_status}); retry in {delay} s", status=cooling_status, retryAfterSeconds=delay, cooldown=True)
                    _wait(cooling, abandoned)
                requests += 1
                wait, reason = 0.0, ""
                extra = {"Range": f"bytes={transfer.size}-", "If-Range": validator} if validator and transfer.size else None
                try:
                    connection, response = _request(current, deadline, agent, extra)
                except (OSError, http.client.HTTPException) as error:
                    if not _transient(error):
                        raise failure(f"download failed: {type(error).__name__}: {error}") from error
                    failures += 1
                    wait, reason = _backoff(failures, backoff_cap), type(error).__name__
                    if failures >= max_attempts or not fits(wait):
                        raise failure(f"download failed after {requests} attempt(s): {type(error).__name__}: {error}") from error
                else:
                    try:
                        status = response.status
                        if status in REDIRECT_STATUSES:
                            location = response.getheader("Location")
                            if not location:
                                raise failure(f"HTTP {status} redirect has no Location header", status=status)
                            if redirects >= MAX_REDIRECTS:
                                raise failure(f"more than {MAX_REDIRECTS} redirects")
                            # Every hop gets the same scheme and credential checks as the requested URL.
                            current = validate_browser_url(urljoin(current, location))
                            redirects += 1
                            continue
                        if status in RETRY_STATUSES:
                            retry_after = _retry_after(response.getheader("Retry-After"))
                            failures += 1
                            wait = float(retry_after) if retry_after is not None else _backoff(failures, backoff_cap)
                            if status in COOLDOWN_STATUSES:
                                _start_cooldown(host, wait, status)
                            if failures >= max_attempts or not fits(wait):
                                suffix = f" (Retry-After: {retry_after})" if retry_after is not None else ""
                                raise failure(
                                    f"HTTP {status} {response.reason}".rstrip() + suffix,
                                    status=status, retryAfterSeconds=math.ceil(wait), bodyExcerpt=_body_excerpt(response),
                                )
                            reason = f"HTTP {status}"
                            if retry_after is not None:
                                retried_after = max(retried_after or 0, retry_after)
                        elif status == 200 or (status == 206 and validator and transfer.size and _content_range_start(response) == transfer.size):
                            if status == 200:
                                transfer.restart()
                                validator = _range_validator(response)
                            else:
                                resumed += 1
                            declared = response.getheader("Content-Length", "")
                            if declared.isdigit() and transfer.size + int(declared) > limit:
                                raise failure(f"Content-Length {declared} exceeds the {limit}-byte limit")
                            try:
                                transfer.stream(response, limit, deadline, int(declared) if declared.isdigit() else None)
                            except (OSError, http.client.HTTPException) as error:
                                if not _transient(error):
                                    raise failure(f"download failed: {type(error).__name__}: {error}") from error
                                failures += 1
                                wait, reason = _backoff(failures, backoff_cap), f"{type(error).__name__} after {transfer.size} bytes"
                                if failures >= max_attempts or not fits(wait):
                                    raise failure(f"download interrupted after {transfer.size} bytes: {type(error).__name__}: {error}") from error
                            else:
                                server_type = _mime(response.getheader("Content-Type"))
                                disposition = response.getheader("Content-Disposition")
                                break
                        else:
                            raise failure(f"HTTP {status} {response.reason}".rstrip(), status=status, bodyExcerpt=_body_excerpt(response))
                    finally:
                        connection.close()
                retries.append({"reason": reason, "waitSeconds": wait})
                _wait(wait, abandoned)

            _clear_cooldown(_host(current))
            size = transfer.size
            digest = transfer.digest.hexdigest()
            if size == 0:
                raise DownloadError("downloaded content is empty")
            detected = detect_type(transfer.head)
            expected = options["expectedTypes"]
            if expected and detected not in expected:
                raise DownloadError(f"downloaded content is {detected or server_type or 'unrecognized'}, not one of: {', '.join(expected)}")
            mime_type = FILE_TYPES[detected][0] if detected else (server_type or "application/octet-stream")
            name = artifact_name(options["name"], disposition, current, detected)
            artifact = publish_unreserved(root, PRODUCER, [{"source": transfer.target, "name": name, "mimeType": mime_type}], size, retention_days)[0]
    except (OSError, http.client.HTTPException) as error:
        if isinstance(error, TimeoutError):
            raise DownloadError("download exceeded timeoutMs or the per-read timeout") from error
        raise DownloadError(f"download failed: {type(error).__name__}: {error}") from error
    if artifact.get("sha256") != digest:
        raise DownloadError("published artifact does not match the downloaded bytes")

    warnings = ["Downloaded bytes are untrusted external data."]
    if detected is None and server_type in {"text/html", "application/xhtml+xml"}:
        warnings.append("The server returned an HTML page rather than a file; use the direct file URL.")
    result = {
        "trust": "untrusted-web-content",
        "sourceUrl": options["url"],
        "finalUrl": current,
        "status": 200,
        "redirects": redirects,
        "attempts": requests,
        "retries": retries,
        "resumed": resumed,
        "retriedAfterSeconds": retried_after,
        "reused": False,
        "serverContentType": server_type,
        "detectedType": detected,
        "bytes": size,
        "sha256": digest,
        "userAgent": agent,
        "fetchedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "elapsedMs": round((time.monotonic() - started) * 1000),
        "artifactId": artifact["id"],
        "artifact": artifact,
        "warnings": warnings,
    }
    _remember(options["url"], result)
    return result


def self_test() -> None:
    assert detect_type(b"%PDF-1.7\n") == "pdf"
    assert detect_type(b"\x89PNG\r\n\x1a\n\x00") == "png"
    assert detect_type(b"\xff\xd8\xff\xe0") == "jpeg"
    assert detect_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert detect_type(b"<!doctype html>") is None
    assert _mime("Text/HTML; charset=utf-8") == "text/html"
    assert _mime("bad type") is None
    assert artifact_name(None, None, "https://upload.example/a/Letter%2C_1975.jpg", "jpeg") == "Letter_1975.jpg"
    assert artifact_name(None, 'attachment; filename="../../scan 1.pdf"', "https://example.com/get", "pdf") == "scan_1.pdf"
    assert artifact_name(None, None, "https://example.com/", "png") == "download.png"
    assert len(artifact_name(None, None, "https://example.com/" + "a" * 300 + ".pdf", "pdf")) == 128
    assert artifact_name("chosen.bin", None, "https://example.com/x", None) == "chosen.bin"
    assert _retry_after("7") == 7 and _retry_after("soon") is None
    defaults = validate_download({"url": "https://Example.com/file.pdf#page=2"})
    assert defaults["url"] == "https://example.com/file.pdf"
    assert defaults["maxBytes"] == DEFAULT_MAX_BYTES and defaults["timeoutMs"] == DEFAULT_TIMEOUT_MS and defaults["reuse"] is True
    assert validate_download({"url": "https://example.com/f.pdf"}, background=True)["timeoutMs"] == DEFAULT_BACKGROUND_TIMEOUT_MS
    assert validate_download({"url": "https://example.com/f.pdf", "timeoutMs": MAX_BACKGROUND_TIMEOUT_MS}, background=True)["timeoutMs"] == MAX_BACKGROUND_TIMEOUT_MS
    for failures in range(1, 10):
        assert BACKOFF_BASE_SECONDS / 2 <= _backoff(failures, BACKOFF_CAP_SECONDS) <= BACKOFF_CAP_SECONDS
    assert _transient(ConnectionResetError()) and _transient(TimeoutError()) and _transient(http.client.IncompleteRead(b""))
    assert not _transient(ssl.SSLCertVerificationError()) and not _transient(PermissionError())
    for invalid in (
        {"url": "file:///etc/passwd"},
        {"url": "https://user:secret@example.com/"},
        {"url": "https://example.com/", "maxBytes": MAX_BYTES + 1},
        {"url": "https://example.com/", "expectedTypes": ["exe"]},
        {"url": "https://example.com/", "name": "../escape"},
        {"url": "https://example.com/", "headers": {"Cookie": "x"}},
        {"url": "https://example.com/", "timeoutMs": 60_000},
        {"url": "https://example.com/", "reuse": "yes"},
    ):
        try:
            validate_download(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid download arguments were accepted: {invalid}")
    assert user_agent().startswith("VantaMCPd-browser-retrieval/")
    assert "Mozilla" not in user_agent()


if __name__ == "__main__":
    self_test()

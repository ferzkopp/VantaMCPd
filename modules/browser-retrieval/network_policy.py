#!/usr/bin/env python3
import sys
from urllib.parse import SplitResult, urlsplit, urlunsplit

MAX_URL_LENGTH = 8192
ALLOWED_SCHEMES = {"http", "https"}


class NetworkPolicyError(ValueError):
    pass


def _canonical_host(hostname: str) -> str:
    lowered = hostname.rstrip(".").lower()
    if not lowered:
        raise NetworkPolicyError("URL hostname is required")
    try:
        return lowered.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise NetworkPolicyError("URL hostname is not valid IDNA") from error


def validate_browser_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise NetworkPolicyError(f"URL must contain from 1 to {MAX_URL_LENGTH} characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise NetworkPolicyError("URL is malformed") from error
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise NetworkPolicyError("only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkPolicyError("URL credentials are not allowed")
    if parsed.hostname is None:
        raise NetworkPolicyError("URL hostname is required")
    if port == 0:
        raise NetworkPolicyError("URL port must be from 1 to 65535")

    host = _canonical_host(parsed.hostname)
    host_for_url = f"[{host}]" if ":" in host else host
    netloc = host_for_url if port is None else f"{host_for_url}:{port}"
    normalized = SplitResult(scheme, netloc, parsed.path or "/", parsed.query, "")
    return urlunsplit(normalized)


def sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or "invalid"
        port = f":{parsed.port}" if parsed.port is not None else ""
        host_for_url = f"[{host}]" if ":" in host else host
        return urlunsplit((scheme, f"{host_for_url}{port}", parsed.path or "/", "", ""))
    except (TypeError, ValueError):
        return "[invalid-url]"


def self_test() -> None:
    assert validate_browser_url("https://Example.COM/docs?q=1#part") == "https://example.com/docs?q=1"
    assert validate_browser_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080/"
    assert validate_browser_url("http://[::1]:3000/") == "http://[::1]:3000/"
    assert validate_browser_url("https://internal.example/") == "https://internal.example/"
    for blocked in (
        "file:///etc/passwd",
        "https://user:pass@example.com/",
        "http://example.com:0/",
    ):
        try:
            validate_browser_url(blocked)
        except NetworkPolicyError:
            pass
        else:
            raise AssertionError(f"blocked URL was accepted: {blocked}")
    assert sanitize_url("https://user:pass@example.com/path?token=x#secret") == "https://example.com/path"


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        self_test()
    else:
        raise SystemExit("usage: network_policy.py --self-test")

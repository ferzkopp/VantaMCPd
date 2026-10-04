#!/usr/bin/env python3
"""Install a pinned Pyright npm tarball without provisioning the npm toolchain."""
import base64
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

VERSION = "1.1.414"
URL = f"https://registry.npmjs.org/pyright/-/pyright-{VERSION}.tgz"
INTEGRITY = "FPZZb51jepDX4eP7TEYDeNFtmE3WgwkkEcJpvH3/QmUSsj0EAy3LXu+xB4T/FWDejtsXlCfFr/rbWFjwuwuXww=="
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024


def install(target: str, payload: bytes) -> None:
    expected = base64.b64decode(INTEGRITY)
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise ValueError("Pyright package exceeds the download limit")
    if hashlib.sha512(payload).digest() != expected:
        raise ValueError("Pyright package integrity check failed")
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix=".pyright-", dir=parent)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive:
                parts = member.name.split("/")
                if not parts or parts[0] != "package" or any(part in ("", ".", "..") for part in parts[1:]):
                    raise ValueError(f"unsafe Pyright package path: {member.name}")
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise ValueError(f"unsupported Pyright package entry: {member.name}")
                relative = os.path.join(*parts[1:]) if len(parts) > 1 else ""
                destination = os.path.join(temporary, relative)
                if member.isdir():
                    os.makedirs(destination, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read Pyright package entry: {member.name}")
                with source, open(destination, "wb") as output:
                    shutil.copyfileobj(source, output)
        if not os.path.isfile(os.path.join(temporary, "index.js")):
            raise ValueError("Pyright package does not contain index.js")
        shutil.rmtree(target, ignore_errors=True)
        os.replace(temporary, target)
        temporary = ""
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def download() -> bytes:
    request = urllib.request.Request(URL, headers={"User-Agent": "VantaMCPd-python-compute"})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    return payload


def self_test() -> None:
    assert VERSION and len(base64.b64decode(INTEGRITY)) == hashlib.sha512().digest_size
    try:
        install(os.path.join(tempfile.gettempdir(), "never-created"), b"invalid")
    except ValueError as error:
        assert "integrity" in str(error)
    else:
        raise AssertionError("invalid Pyright package was accepted")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    elif len(sys.argv) == 2:
        install(sys.argv[1], download())
    else:
        raise SystemExit("usage: install_pyright.py TARGET | --self-test")

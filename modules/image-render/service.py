#!/usr/bin/env python3
"""Image Render broker.

A long-lived systemd service owned by an unprivileged system user. It accepts length-prefixed JSON requests
on a Unix socket from the VantaMCPd SSH user only, answers library lookups in-process, and runs every scene
check and render in a fresh sandbox. Scene text never executes in this process.
"""
import hashlib
import json
import logging
import os
import platform
import select
import shutil
import socket
import socketserver
import struct
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import artifact_io
import polyhaven
import schemas
from library import Library
from sandbox import Sandbox

SOCKET_PATH = "/run/vantamcpd-render/render.sock"
MAX_REQUEST_BYTES = 4_200_000
MAX_RESPONSE_BYTES = 3_900_000

INSTALL_DIR = os.environ.get("VANTA_RENDER_INSTALL_DIR", "/opt/vantamcpd/modules/image-render/current")
STATE_DIR = os.environ.get("VANTA_RENDER_STATE_DIR", "/var/lib/vantamcpd-render")
LIBRARY_DIR = os.path.join(INSTALL_DIR, "library")
MEMORY_MB = int(os.environ.get("VANTA_RENDER_MEMORY_MB", "512"))
THREADS = int(os.environ.get("VANTA_RENDER_THREADS", "1"))
LIMITS = {
    "maxTimeoutMs": int(os.environ.get("VANTA_RENDER_MAX_TIMEOUT_MS", "600000")),
    "maxBackgroundTimeoutMs": int(os.environ.get("VANTA_RENDER_MAX_BACKGROUND_TIMEOUT_MS", "14400000")),
}
CONCURRENT_RENDERS = int(os.environ.get("VANTA_RENDER_CONCURRENT_RENDERS", "1"))
CALLS_PER_MINUTE = int(os.environ.get("VANTA_RENDER_CALLS_PER_MINUTE", "20"))
ARTIFACT_ROOT = os.environ.get("VANTA_ARTIFACT_ROOT")
REPORT_DIR = os.environ.get("VANTA_RENDER_REPORT_DIR", "/var/lib/vantamcpd/module-reports/image-render")
RENDER_ACTIONS = ("render_check", "render_scene", "render_benchmark")
BACKGROUND_ACTIONS = ("render_scene", "render_benchmark")
LIBRARY_ACTIONS = ("render_environment", "render_docs", "render_library", "render_polyhaven")

RENDER_SLOTS = threading.BoundedSemaphore(CONCURRENT_RENDERS)
# Checks get their own slot so a long render never blocks syntax iteration.
CHECK_SLOT = threading.BoundedSemaphore(1)
IMPORT_SLOT = threading.BoundedSemaphore(1)
RATE_LOCK = threading.Lock()
RECENT_CALLS: deque[float] = deque()

logging.basicConfig(level=logging.INFO, format="image-render %(levelname)s %(message)s")
LOGGER = logging.getLogger("image-render")
SANDBOX = Sandbox(INSTALL_DIR, STATE_DIR, LIBRARY_DIR, MEMORY_MB, THREADS, ARTIFACT_ROOT)
LIBRARY: Library | None = None
CATALOG: polyhaven.Catalog | None = None


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(min(remaining, 65_536))
        if not chunk:
            raise ValueError("client closed the request")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise PermissionError("SO_PEERCRED is unavailable")
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def _client_gone(connection: socket.socket) -> bool:
    """The adapter sends nothing after its request, so a readable socket means it closed."""
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        return bool(readable) and connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def _check_rate_limit(now: float) -> None:
    with RATE_LOCK:
        while RECENT_CALLS and now - RECENT_CALLS[0] >= 60:
            RECENT_CALLS.popleft()
        if len(RECENT_CALLS) >= CALLS_PER_MINUTE:
            raise ValueError(f"image-render accepts at most {CALLS_PER_MINUTE} render and check calls per minute")
        RECENT_CALLS.append(now)


def _library() -> Library:
    global LIBRARY
    if LIBRARY is None:
        LIBRARY = Library(LIBRARY_DIR)
    return LIBRARY


def _catalog() -> polyhaven.Catalog:
    global CATALOG
    if CATALOG is None:
        CATALOG = polyhaven.Catalog(LIBRARY_DIR)
    return CATALOG


def _polyhaven_summary() -> dict[str, Any]:
    try:
        return _catalog().summary()
    except (OSError, ValueError) as error:
        return {"available": False, "error": str(error)}


def _polyhaven(request: dict[str, Any]) -> dict[str, Any]:
    catalog = _catalog()
    if request["operation"] == "search":
        return catalog.search(request["query"], request["kind"], request["limit"])
    item = catalog.get(request["assetId"])
    if not artifact_io.available(ARTIFACT_ROOT):
        raise ValueError("shared artifact storage is unavailable on this node; Poly Haven imports need it")
    call_dir = os.path.join(STATE_DIR, "calls", uuid.uuid4().hex)
    os.makedirs(call_dir, mode=0o700)
    try:
        listing_text = artifact_io.read_text(ARTIFACT_ROOT, request["filesArtifactId"], call_dir, polyhaven.MAX_LISTING_BYTES)
        try:
            listing = json.loads(listing_text)
        except json.JSONDecodeError:
            listing = None
        if not isinstance(listing, dict):
            raise ValueError(f"filesArtifactId is not a Poly Haven file listing; download {polyhaven.API_FILES.format(request['assetId'])} again")
        files = polyhaven.plan(item, listing, request["resolution"])
        summary = {"assetId": request["assetId"], "kind": item["kind"], "name": item["name"], "resolution": request["resolution"]}
        if request["operation"] == "plan":
            return {
                **summary,
                "downloads": [
                    {"name": entry["name"], "role": entry["role"], "required": entry["required"], "bytes": entry["bytes"],
                     "webDownload": {"url": entry["url"], "name": entry["name"], "maxBytes": entry["bytes"]}}
                    for entry in files
                ],
                "totalBytes": sum(entry["bytes"] for entry in files),
                "next": "Call browser-retrieval web_download with each webDownload object, then import with files [{name, artifactId}]. Optional files may be skipped.",
                "credit": polyhaven.CREDIT,
            }
        _check_rate_limit(time.monotonic())
        if not IMPORT_SLOT.acquire(timeout=5):
            raise ValueError("image-render is already importing an asset on this node; retry shortly")
        try:
            planned = {entry["name"] for entry in files}
            unknown = sorted(entry["name"] for entry in request["files"] if entry["name"] not in planned)
            if unknown:
                raise ValueError(f"files not in the plan for this asset and resolution: {', '.join(unknown)}")
            inputs = os.path.join(call_dir, "inputs")
            os.makedirs(inputs, mode=0o700)
            artifact_io.stage_named(ARTIFACT_ROOT, request["files"], inputs, artifact_io.InputBudget(polyhaven.MAX_ASSET_BYTES))
            staged = {entry["name"]: os.path.join(inputs, entry["name"]) for entry in request["files"]}
            polyhaven.verify(files, staged)
            text, needed, details = polyhaven.build_include(request["assetId"], item, request["resolution"], files, staged)
            name = polyhaven.include_name(request["assetId"], request["resolution"])
            path = os.path.join(call_dir, name)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            reservation = artifact_io.reserve(ARTIFACT_ROOT, "image-render", os.path.getsize(path))
            try:
                include = artifact_io.publish(ARTIFACT_ROOT, reservation, path, name, request["retentionDays"], "text/plain")
                reservation = None
            finally:
                artifact_io.release(ARTIFACT_ROOT, reservation)
        finally:
            IMPORT_SLOT.release()
        artifact_ids = {entry["name"]: entry["artifactId"] for entry in request["files"]}
        return {
            **summary,
            "include": {"name": name, "artifactId": include["id"], "bytes": include["bytes"]},
            "assets": [{"artifactId": include["id"], "name": name}] + [{"artifactId": artifact_ids[file], "name": file} for file in needed],
            "sceneInclude": f'#include "{name}"',
            **details,
            "next": "Pass assets to render_scene unchanged, add sceneInclude to the scene, and credit Poly Haven.",
            "credit": polyhaven.CREDIT,
        }
    finally:
        shutil.rmtree(call_dir, ignore_errors=True)


def _installed() -> dict[str, Any]:
    try:
        with open(os.path.join(INSTALL_DIR, "environment.json"), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_report() -> dict[str, Any] | None:
    try:
        with open(os.path.join(REPORT_DIR, "report.json"), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def benchmark_report(result: dict[str, Any], povray: str | None, finished: float) -> dict[str, Any]:
    """The display-only installation report VantaMCPd reads with the module receipts."""
    statistics = result["statistics"]
    seconds = result["durationMs"] / 1000
    version = ".".join((povray or "unknown").split(":", 1)[-1].split("-", 1)[0].split(".")[:3])
    phases = " · ".join(f"{name} {statistics[key]:,.1f} s" for name, key in (("parse", "parseSeconds"), ("photons", "photonSeconds"), ("trace", "traceSeconds")) if key in statistics)
    details = {key: value for key, value in result.items() if key not in ("ok", "diagnostics", "log", "reportUpdated", "reportError")}
    details.update({"machine": platform.machine(), "cpuCount": os.cpu_count(), "povray": povray})
    return {
        "schemaVersion": 1,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished)),
        "summary": {
            "benchmark": f"{result['benchmark']['width']}x{result['benchmark']['height']} in {seconds:,.1f} s · {result['threads']} threads · {platform.machine()} · POV-Ray {version}",
            "benchmark phases": f"{phases} · {result['pixelsPerSecond']:,} px/s",
        },
        "details": details,
    }


def _write_report(report: dict[str, Any]) -> None:
    temporary = os.path.join(REPORT_DIR, f".report-{os.getpid()}.json")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, separators=(",", ":"))
    os.chmod(temporary, 0o644)
    os.replace(temporary, os.path.join(REPORT_DIR, "report.json"))


def _environment() -> dict[str, Any]:
    installed = _installed()
    report = _read_report()
    return {
        "povray": installed.get("povray"),
        "assetSelection": installed.get("assets"),
        "benchmark": report.get("details") if isinstance(report, dict) else None,
        "library": _library().summary(),
        "polyhaven": _polyhaven_summary(),
        "includePaths": SANDBOX.library_paths(),
        "limits": {
            "width": schemas.MAX_DIMENSION,
            "height": schemas.MAX_DIMENSION,
            "pixels": schemas.MAX_PIXELS,
            "sceneCharacters": schemas.MAX_SCENE_CHARACTERS,
            "files": {"maximum": schemas.MAX_FILES, "totalCharacters": schemas.MAX_FILE_TOTAL_CHARACTERS},
            "assets": schemas.MAX_ASSETS,
            "artifactReferences": schemas.MAX_ARTIFACT_REFERENCES,
            "artifactInputBytes": schemas.MAX_ARTIFACT_INPUT_BYTES,
            "inlineOutputBytes": schemas.MAX_INLINE_OUTPUT_BYTES,
            "timeoutMs": {"default": min(schemas.DEFAULT_TIMEOUT_MS, LIMITS["maxTimeoutMs"]), "maximum": LIMITS["maxTimeoutMs"]},
            "backgroundTimeoutMs": {"default": LIMITS["maxBackgroundTimeoutMs"], "maximum": LIMITS["maxBackgroundTimeoutMs"]},
            "memoryMb": MEMORY_MB,
            "threads": THREADS,
            "concurrentRenders": CONCURRENT_RENDERS,
            "callsPerMinute": CALLS_PER_MINUTE,
        },
        "sharedArtifacts": artifact_io.available(ARTIFACT_ROOT),
        "isolation": SANDBOX.isolation,
        "conventions": [
            "Inline files[] are written beside the scene in /work; assets[] artifacts are staged read-only in /inputs. Both are on the library path, so refer to them by bare name.",
            "A string literal \"artifact:<32-hex id>\" in the scene or files is replaced with the staged path of that artifact, e.g. image_map { png \"artifact:...\" }.",
            "Standard includes (colors.inc, textures.inc, woods.inc, metals.inc, glass.inc, stones.inc, skies.inc, ...) and #include \"vanta_assets.inc\" are always available.",
            "Any other Poly Haven HDRI, texture or model can be imported on demand with render_polyhaven and browser-retrieval web_download; credit Poly Haven when you use one.",
            "File I/O is limited to reading the library path and writing inside /work; there is no network and no shell-out.",
            "Start scenes with #version 3.7; and global_settings { assumed_gamma 1.0 }. Use render_check before a large render.",
        ],
    }


def execute(action: str, arguments: dict[str, Any], background: bool = False, abandoned: Any = None) -> dict[str, Any]:
    if action == "ping":
        return {"ok": True, "isolation": SANDBOX.isolation.get("level")}
    if action == "render_environment":
        schemas.validate_environment(arguments)
        return _environment()
    if action == "render_docs":
        request = schemas.validate_docs(arguments)
        library = _library()
        if request["operation"] == "search":
            return library.docs_search(request["query"], request["limit"])
        if request["operation"] == "read":
            return library.docs_read(request["sectionId"], request["offset"], request["maxCharacters"])
        return library.docs_toc(request["sectionId"], request["depth"])
    if action == "render_library":
        request = schemas.validate_library(arguments)
        library = _library()
        if request["operation"] == "search":
            return library.search(request["query"], request["kind"], request["limit"])
        if request["operation"] == "read":
            return library.read(request["path"], request["offset"], request["maxCharacters"])
        return library.declarations(request["query"], request["path"], request["limit"])
    if action == "render_polyhaven":
        return _polyhaven(schemas.validate_polyhaven(arguments))
    if action == "render_check":
        return SANDBOX.run(schemas.validate_check(arguments), abandoned)
    if action == "render_scene":
        request = schemas.validate_render(arguments, LIMITS, background=background)
        result = SANDBOX.run(request, abandoned)
        result["request"] = {key: request[key] for key in ("width", "height", "quality", "bitDepth", "alpha", "timeoutMs")}
        return result
    if action == "render_benchmark":
        schemas.validate_benchmark(arguments)
        if not background:
            raise ValueError("render_benchmark runs only in the background; call it with execution: \"background\"")
        result = SANDBOX.benchmark(abandoned)
        result["reportUpdated"] = False
        if result["ok"]:
            try:
                _write_report(benchmark_report(result, _installed().get("povray"), time.time()))
                result["reportUpdated"] = True
            except OSError as error:
                result["reportError"] = str(error)
        return result
    raise ValueError(f"unknown action: {action}")


def _summary(action: str, arguments: Any) -> str:
    """Scene text can be sensitive, so only digests, sizes and artifact ID prefixes are logged."""
    if not isinstance(arguments, dict) or action not in ("render_check", "render_scene"):
        return f"action={action}"
    scene = arguments.get("scene") if isinstance(arguments.get("scene"), dict) else {}
    if isinstance(scene.get("text"), str):
        payload = scene["text"].encode("utf-8", errors="replace")
        source = f"inline:sha256:{hashlib.sha256(payload).hexdigest()[:12]} bytes={len(payload)}"
    elif isinstance(scene.get("artifactId"), str):
        source = "artifact:" + scene["artifactId"][:12]
    else:
        source = "example:" + str(scene.get("example", "-"))[:80]
    return f"action={action} scene={source} size={arguments.get('width', '-')}x{arguments.get('height', '-')}"


class RequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        started = time.monotonic()
        action = "unknown"
        arguments: dict[str, Any] = {}
        try:
            if _peer_uid(self.request) != EXPECTED_UID:
                raise PermissionError("caller is not authorized to use image-render")
            size = struct.unpack("!I", _receive_exact(self.request, 4))[0]
            if size < 2 or size > MAX_REQUEST_BYTES:
                raise ValueError("request size is invalid")
            message = json.loads(_receive_exact(self.request, size))
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            action = message.get("action")
            arguments = message.get("arguments", {})
            execution = message.get("execution", "immediate")
            if action not in ("ping", *LIBRARY_ACTIONS, *RENDER_ACTIONS):
                raise ValueError("unknown broker action")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            if execution not in ("immediate", "background") or (execution == "background" and action not in BACKGROUND_ACTIONS):
                raise ValueError("invalid execution mode")
            abandoned = lambda: _client_gone(self.request)
            if action not in RENDER_ACTIONS:
                result = execute(action, arguments)
            else:
                slot = CHECK_SLOT if action == "render_check" else RENDER_SLOTS
                if not slot.acquire(timeout=5):
                    busy = "a check" if action == "render_check" else f"{CONCURRENT_RENDERS} render(s)"
                    raise ValueError(f"image-render is already running {busy} on this node; retry shortly or target another node")
                try:
                    _check_rate_limit(time.monotonic())
                    result = execute(action, arguments, background=execution == "background", abandoned=abandoned)
                finally:
                    slot.release()
            response = {"ok": True, "result": result}
            LOGGER.info("%s ok=%s duration_ms=%d", _summary(action, arguments), str(result.get("ok", True)).lower(), round((time.monotonic() - started) * 1000))
        except Exception as error:
            response = {"ok": False, "error": str(error)}
            LOGGER.warning("%s ok=false duration_ms=%d error=%s", _summary(action, arguments), round((time.monotonic() - started) * 1000), type(error).__name__)
        payload = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = json.dumps({"ok": False, "error": "the result exceeded the response limit; use artifact output or read less text"}, separators=(",", ":")).encode("utf-8")
        try:
            self.request.sendall(struct.pack("!I", len(payload)) + payload)
        except OSError:
            LOGGER.info("action=%s caller disconnected before the response", action)


if hasattr(socketserver, "ThreadingUnixStreamServer"):
    class BrokerServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        request_queue_size = 8
else:
    class BrokerServer:
        def __init__(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("Unix-domain socket servers are unavailable on this platform")


def self_test() -> None:
    assert execute("ping", {})["ok"] is True
    summary = _summary("render_scene", {"scene": {"text": "secret scene"}, "width": 64, "height": 48})
    assert "secret" not in summary and "inline:sha256:" in summary and "64x48" in summary
    assert _summary("render_docs", {"query": "camera"}) == "action=render_docs"
    report = benchmark_report({
        "benchmark": {"width": 512, "height": 512},
        "threads": 4,
        "durationMs": 123_456,
        "pixelsPerSecond": 2_500.5,
        "statistics": {"parseSeconds": 0.4, "photonSeconds": 1.6, "traceSeconds": 120.0},
        "diagnostics": [],
        "ok": True,
    }, "1:3.7.0.10-3+b2", 0)
    assert report["updatedAt"] == "1970-01-01T00:00:00Z"
    assert report["summary"]["benchmark"].startswith("512x512 in 123.5 s · 4 threads") and report["summary"]["benchmark"].endswith("POV-Ray 3.7.0")
    assert report["summary"]["benchmark phases"] == "parse 0.4 s · photons 1.6 s · trace 120.0 s · 2,500.5 px/s"
    assert "diagnostics" not in report["details"] and len(json.dumps(report)) < 8_192
    try:
        execute("render_benchmark", {})
    except ValueError as error:
        assert "background" in str(error)
    else:
        raise AssertionError("an immediate benchmark was accepted")
    try:
        execute("render_shell", {})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown action accepted")


def main() -> None:
    SANDBOX.purge_stale_calls()
    _library()
    SANDBOX.probe()
    LOGGER.info("sandbox verified: %s", SANDBOX.isolation["level"])
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o755, exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    with BrokerServer(SOCKET_PATH, RequestHandler) as server:
        os.chown(SOCKET_PATH, -1, CLIENT_GID)
        os.chmod(SOCKET_PATH, 0o660)
        LOGGER.info("broker ready memory_mb=%d threads=%d renders=%d", MEMORY_MB, THREADS, CONCURRENT_RENDERS)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    else:
        EXPECTED_UID = int(os.environ["VANTA_RENDER_CALLER_UID"])
        CLIENT_GID = int(os.environ["VANTA_RENDER_CLIENT_GID"])
        main()

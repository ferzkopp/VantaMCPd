#!/usr/bin/env python3
"""Sandboxed POV-Ray execution for Image Render.

Each call runs /usr/bin/povray under bubblewrap with private user, mount, PID, IPC, UTS, cgroup and network
namespaces and POSIX resource limits. The mount namespace is the file boundary: the scene sees a read-only
system view without /etc or home directories, its own writable /work and /tmp (both on watched per-call
disk), read-only staged inputs (/inputs) and the read-only module library (/vanta-library). Debian's POV-Ray
3.7.0.10 accepts a restricted povray.conf but does not enforce it for scene file access, so that policy is
installed only as defense in depth.
"""
import base64
import hashlib
import os
import posixpath
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Callable

import artifact_io
import scene as scene_tools
import schemas

BWRAP = "/usr/bin/bwrap"
PRLIMIT = "/usr/bin/prlimit"
POVRAY = "/usr/bin/povray"
SYSTEM_PATHS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/usr")
EXTRA_READ_ONLY = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives")
POV_INCLUDE = "/usr/share/povray-3.7/include"
LIBRARY_MOUNT = "/vanta-library"
CONF_TARGET = "/etc/povray/3.7/povray.conf"
USER_CONF_TARGET = "/tmp/home/.povray/3.7/povray.conf"
INI_TARGET = "/vanta/povray.ini"
BENCHMARK_INI_TARGET = "/vanta/benchmark.ini"
BENCHMARK_SCENE = "advanced/benchmark/benchmark.pov"
BENCHMARK_WIDTH = 512
BENCHMARK_HEIGHT = 512
# A fixed, trusted workload: a 2-core ARM board needs about four hours, so it gets the full manifest ceiling.
BENCHMARK_TIMEOUT_MS = 86_400_000
OUTPUT_NAME = "render.png"
GRACE_SECONDS = 5
WORKSPACE_SLACK_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_FILES = 2_000
CAPTURE_HEAD_BYTES = 16 * 1024
CAPTURE_TAIL_BYTES = 64 * 1024
LOG_TAIL_LINES = 40
PROBE_SCENE = (
    "#version 3.7;\n"
    "global_settings { assumed_gamma 1.0 }\n"
    "camera { location <0, 1, -3> look_at 0 }\n"
    "light_source { <2, 4, -3> color rgb 1 }\n"
    "sphere { 0, 1 pigment { color rgb <0.2, 0.4, 0.8> } }\n"
)


def build_limited_argv(memory_mb: int, cpu_seconds: int, file_bytes: int, threads: int) -> list[str]:
    """POSIX limits for the sandbox. RLIMIT_CPU is summed over all render threads."""
    return [
        PRLIMIT,
        f"--as={memory_mb * 1024 * 1024}",
        f"--cpu={cpu_seconds}",
        f"--fsize={file_bytes}",
        "--nofile=256",
        f"--nproc={threads + 128}",
        "--core=0",
        "--",
    ]


def build_bwrap_argv(
    call_dir: str,
    module_dir: str,
    library_dir: str,
    system_paths=SYSTEM_PATHS,
    extra_read_only=EXTRA_READ_ONLY,
    exists=os.path.exists,
    islink=os.path.islink,
    readlink=os.readlink,
) -> list[str]:
    argv = [BWRAP, "--unshare-all", "--unshare-user", "--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session"]
    for path in system_paths:
        if not exists(path):
            continue
        if islink(path):
            argv += ["--symlink", readlink(path), path]
        else:
            argv += ["--ro-bind", path, path]
    for path in extra_read_only:
        if exists(path):
            argv += ["--ro-bind", path, path]
    argv += [
        "--proc", "/proc",
        "--dev", "/dev",
        # A disk-backed, watched /tmp instead of tmpfs, so scratch writes cannot consume RAM unnoticed.
        "--bind", posixpath.join(call_dir, "tmp"), "/tmp",
        # POV-Ray refuses to start without a user policy; both slots get the same restricted file.
        "--ro-bind", posixpath.join(module_dir, "povray.conf"), CONF_TARGET,
        "--ro-bind", posixpath.join(module_dir, "povray.conf"), USER_CONF_TARGET,
        "--ro-bind", posixpath.join(module_dir, "povray.ini"), INI_TARGET,
        "--ro-bind", posixpath.join(module_dir, "benchmark.ini"), BENCHMARK_INI_TARGET,
        "--ro-bind", library_dir, LIBRARY_MOUNT,
        "--ro-bind", posixpath.join(call_dir, "inputs"), "/inputs",
        "--bind", posixpath.join(call_dir, "workspace"), "/work",
        "--chdir", "/work",
        "--clearenv",
        "--setenv", "HOME", "/tmp/home",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "POVINI", INI_TARGET,
        "--setenv", "LC_ALL", "C.UTF-8",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "MALLOC_ARENA_MAX", "2",
    ]
    return argv


def _number(value: float) -> str:
    return f"{value:.10g}"


def build_povray_argv(request: dict[str, Any], scene_path: str, threads: int, library_paths: list[str]) -> list[str]:
    argv = [POVRAY, f"+I{scene_path}", "-D", "-P", "-C", f"+WT{threads}", f"+W{request['width']}", f"+H{request['height']}", f"+Q{request['quality']}"]
    if request["mode"] == "check":
        argv.append("-F")
    else:
        argv += [f"+O/work/{OUTPUT_NAME}", f"+FN{request['bitDepth']}"]
    antialias = request["antialias"]
    if antialias.get("enabled"):
        argv += [f"+A{_number(antialias['threshold'])}", f"+AM{antialias['method']}", f"+R{antialias['depth']}"]
    else:
        argv.append("-A")
    argv.append("+UA" if request["alpha"] else "-UA")
    argv += [f"+L{path}" for path in library_paths]
    if request.get("clock") is not None:
        argv.append(f"+K{_number(request['clock'])}")
    argv += [f"Declare={name}={_number(value)}" for name, value in request["declares"].items()]
    return argv


class BoundedCapture(threading.Thread):
    """Drains a pipe, keeping its head and tail so verbose #debug output cannot exhaust memory."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def run(self) -> None:
        for chunk in iter(lambda: self.stream.read(65_536), b""):
            self.total += len(chunk)
            room = CAPTURE_HEAD_BYTES - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self.tail += chunk
                if len(self.tail) > CAPTURE_TAIL_BYTES:
                    del self.tail[: len(self.tail) - CAPTURE_TAIL_BYTES]

    def text(self) -> tuple[str, bool]:
        truncated = self.total > len(self.head) + len(self.tail)
        middle = f"\n[... {self.total - len(self.head) - len(self.tail)} bytes of console output omitted ...]\n" if truncated else ""
        # Head and tail are cut at byte offsets, so a split UTF-8 sequence must not demote the whole buffer to Latin-1.
        return bytes(self.head).decode("utf-8", errors="replace") + middle + bytes(self.tail).decode("utf-8", errors="replace"), truncated


def _workspace_usage(path: str) -> tuple[int, int]:
    total = 0
    count = 0
    for current, directories, files in os.walk(path):
        count += len(files) + len(directories)
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
    return total, count


class Sandbox:
    def __init__(self, module_dir: str, state_dir: str, library_dir: str, memory_mb: int, threads: int, artifact_root: str | None):
        self.module_dir = os.path.realpath(module_dir)
        self.library_dir = os.path.realpath(library_dir)
        self.calls_dir = os.path.join(state_dir, "calls")
        self.memory_mb = memory_mb
        self.threads = threads
        self.artifact_root = artifact_root
        os.makedirs(self.calls_dir, mode=0o700, exist_ok=True)
        self.isolation: dict[str, Any] = {"level": "unverified", "network": "none"}

    def purge_stale_calls(self) -> None:
        for name in os.listdir(self.calls_dir):
            shutil.rmtree(os.path.join(self.calls_dir, name), ignore_errors=True)

    def probe(self) -> dict[str, Any]:
        for path in (BWRAP, PRLIMIT, POVRAY, POV_INCLUDE, self.library_dir, os.path.join(self.module_dir, "povray.conf"), os.path.join(self.module_dir, "povray.ini"), os.path.join(self.module_dir, "benchmark.ini")):
            if not os.path.exists(path):
                raise RuntimeError(f"required sandbox path is missing: {path}")
        result = self.run(schemas.validate_check({"scene": {"text": PROBE_SCENE}, "timeoutMs": 60_000}))
        if not result["ok"]:
            raise RuntimeError(f"sandbox probe failed: {result.get('exitReason')} {result.get('diagnostics') or result.get('log')}")
        self.isolation = {
            "level": "namespaces",
            "namespaces": ["user", "mount", "pid", "ipc", "uts", "cgroup", "network"],
            "network": "none",
            "filesystem": "read-only /usr and /lib system view without /etc or home directories; per-call writable /work and /tmp; read-only /inputs and /vanta-library",
            "povrayIo": "a restricted povray.conf is installed as defense in depth; Debian's POV-Ray 3.7.0.10 does not enforce it for scene file access, so the mount namespace is the boundary",
        }
        return self.isolation

    def library_paths(self, example_dir: str | None = None) -> list[str]:
        paths = ["/work", "/inputs", POV_INCLUDE]
        assets = os.path.join(self.library_dir, "assets")
        if os.path.isdir(assets):
            paths.append(f"{LIBRARY_MOUNT}/assets")
            for name in sorted(os.listdir(assets)):
                if os.path.isdir(os.path.join(assets, name)):
                    paths.append(f"{LIBRARY_MOUNT}/assets/{name}")
        if example_dir:
            paths.append(f"{LIBRARY_MOUNT}/examples/{example_dir}" if example_dir != "." else f"{LIBRARY_MOUNT}/examples")
        return paths

    def _example(self, example: str) -> tuple[str, str]:
        root = os.path.join(self.library_dir, "examples")
        host = os.path.realpath(os.path.join(root, *example.split("/")))
        if os.path.commonpath((os.path.realpath(root), host)) != os.path.realpath(root) or not os.path.isfile(host):
            raise ValueError(f"example scene not found: {example}; search with render_library kind=example")
        return host, posixpath.dirname(example) or "."

    def run(self, request: dict[str, Any], abandoned: Callable[[], bool] | None = None) -> dict[str, Any]:
        call_dir = os.path.join(self.calls_dir, uuid.uuid4().hex)
        workspace = os.path.join(call_dir, "workspace")
        inputs = os.path.join(call_dir, "inputs")
        os.makedirs(workspace, mode=0o700)
        os.makedirs(inputs, mode=0o700)
        os.makedirs(os.path.join(call_dir, "tmp", "home"), mode=0o700)
        reservation_id = None
        try:
            scene = request["scene"]
            sources: dict[str, str] = {}
            example_dir = None
            if "example" in scene:
                host_path, example_dir = self._example(scene["example"])
                scene_path = f"{LIBRARY_MOUNT}/examples/{scene['example']}"
                with open(host_path, "rb") as handle:
                    sources[scene_path] = scene_tools.decode_text(handle.read(schemas.MAX_SCENE_ARTIFACT_BYTES))
                scene_text = None
            else:
                if "artifactId" in scene:
                    self._require_artifacts()
                    scene_text = artifact_io.read_text(self.artifact_root, scene["artifactId"], call_dir, schemas.MAX_SCENE_ARTIFACT_BYTES)
                else:
                    scene_text = scene["text"]
                scene_path = f"/work/{scene['name']}"

            staged: list[dict[str, Any]] = []
            texts = ([scene_text] if scene_text is not None else []) + [entry["text"] for entry in request["files"]]
            referenced = scene_tools.references(texts)
            if len(referenced) > schemas.MAX_ARTIFACT_REFERENCES:
                raise ValueError(f"a scene may reference at most {schemas.MAX_ARTIFACT_REFERENCES} artifacts")
            if request["assets"] or referenced:
                self._require_artifacts()
                budget = artifact_io.InputBudget(schemas.MAX_ARTIFACT_INPUT_BYTES)
                staged += artifact_io.stage_named(self.artifact_root, request["assets"], inputs, budget)
                taken = {entry["name"].lower() for entry in request["assets"] + request["files"]}
                taken.add(scene_path.rsplit("/", 1)[-1].lower())
                named_ids = {entry["artifactId"] for entry in request["assets"]}
                unnamed = [artifact_id for artifact_id in referenced if artifact_id not in named_ids]
                staged += artifact_io.stage_references(self.artifact_root, unnamed, inputs, budget, taken)
            mapping = {entry["artifactId"]: entry["path"] for entry in staged if entry["artifactId"] in referenced}

            if scene_text is not None:
                scene_text = scene_tools.rewrite(scene_text, mapping)
                self._write(workspace, scene_path.rsplit("/", 1)[-1], scene_text)
                sources[scene_path] = scene_text
            for entry in request["files"]:
                text = scene_tools.rewrite(entry["text"], mapping)
                self._write(workspace, entry["name"], text)
                sources[f"/work/{entry['name']}"] = text

            output = request.get("output")
            output_budget = schemas.estimated_png_bytes(request["width"], request["height"], request["bitDepth"], request["alpha"])
            if output and output["mode"] == "artifact":
                self._require_artifacts("artifact output; use output.mode inline for small images")
                reservation_id = artifact_io.reserve(self.artifact_root, "image-render", output_budget)

            povray = build_povray_argv(request, scene_path, self.threads, self.library_paths(example_dir))
            scratch_budget = output_budget + schemas.image_buffer_bytes(request["width"], request["height"])
            outcome = self._execute(call_dir, povray, request["timeoutMs"], scratch_budget, abandoned)
            lines = scene_tools.clean_output(outcome.pop("console"))
            result: dict[str, Any] = {
                "ok": False,
                "exitReason": outcome["exitReason"],
                "diagnostics": scene_tools.diagnostics(lines, sources),
                "statistics": scene_tools.statistics(lines),
                "durationMs": outcome["durationMs"],
                "threads": self.threads,
                "inputs": [{key: entry[key] for key in ("artifactId", "path", "bytes")} for entry in staged],
            }
            fatal = [entry for entry in result["diagnostics"] if entry["kind"] == "Fatal"]
            completed = outcome["exitReason"] == "completed" and outcome["returnCode"] == 0 and not fatal
            if not completed:
                if outcome["exitReason"] == "completed":
                    result["exitReason"] = "error"
                # Skip the startup banner and option dump so the tail shows what went wrong.
                start = next((index for index, line in enumerate(lines) if line.startswith("==== [")), 0)
                result["returnCode"] = outcome["returnCode"]
                result["log"] = lines[start:][-LOG_TAIL_LINES:]
                result["logTruncated"] = outcome["consoleTruncated"] or len(lines) - start > LOG_TAIL_LINES
                result["isolation"] = self.isolation
                return result
            result["ok"] = True
            if request["mode"] == "render":
                result["image"] = self._deliver(workspace, request, output_budget, reservation_id)
                reservation_id = None
            result["isolation"] = self.isolation
            return result
        finally:
            artifact_io.release(self.artifact_root, reservation_id)
            shutil.rmtree(call_dir, ignore_errors=True)

    def _require_artifacts(self, purpose: str = "artifact inputs") -> None:
        if not artifact_io.available(self.artifact_root):
            raise ValueError(f"shared artifact storage is unavailable on this node for {purpose}")

    @staticmethod
    def _write(directory: str, name: str, text: str) -> None:
        with open(os.path.join(directory, name), "x", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def _deliver(self, workspace: str, request: dict[str, Any], output_budget: int, reservation_id: str | None) -> dict[str, Any]:
        path = os.path.join(workspace, OUTPUT_NAME)
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError("POV-Ray finished without writing the PNG output")
        size = os.path.getsize(path)
        if size > output_budget:
            raise ValueError("the rendered PNG exceeds its size budget")
        with open(path, "rb") as handle:
            dimensions = scene_tools.png_dimensions(handle.read(24))
        if dimensions != (request["width"], request["height"]):
            raise ValueError(f"the rendered file is not a {request['width']}x{request['height']} PNG")
        image: dict[str, Any] = {"width": dimensions[0], "height": dimensions[1], "bitDepth": request["bitDepth"], "alpha": request["alpha"], "bytes": size, "mimeType": "image/png"}
        output = request["output"]
        if output["mode"] == "inline":
            if size > schemas.MAX_INLINE_OUTPUT_BYTES:
                raise ValueError(f"the PNG is {size} bytes and exceeds the {schemas.MAX_INLINE_OUTPUT_BYTES}-byte inline limit; use artifact output")
            with open(path, "rb") as handle:
                image.update({"mode": "inline", "encoding": "base64", "data": base64.b64encode(handle.read()).decode("ascii")})
            return image
        image["mode"] = "artifact"
        image["artifact"] = artifact_io.publish(self.artifact_root, reservation_id, path, output["name"], output["retentionDays"])
        return image

    def benchmark(self, abandoned: Callable[[], bool] | None = None) -> dict[str, Any]:
        """The standard POV-Ray 2.01 benchmark with its documented INI settings at 512x512, without image output."""
        scene_host = os.path.join(self.library_dir, "examples", *BENCHMARK_SCENE.split("/"))
        with open(scene_host, "rb") as handle:
            scene_bytes = handle.read()
        call_dir = os.path.join(self.calls_dir, uuid.uuid4().hex)
        for directory in ("workspace", "inputs", os.path.join("tmp", "home")):
            os.makedirs(os.path.join(call_dir, directory), mode=0o700)
        try:
            povray = [POVRAY, BENCHMARK_INI_TARGET, f"+I{LIBRARY_MOUNT}/examples/{BENCHMARK_SCENE}", f"+WT{self.threads}", f"+L{POV_INCLUDE}"]
            outcome = self._execute(call_dir, povray, BENCHMARK_TIMEOUT_MS, schemas.image_buffer_bytes(BENCHMARK_WIDTH, BENCHMARK_HEIGHT), abandoned)
        finally:
            shutil.rmtree(call_dir, ignore_errors=True)
        lines = scene_tools.clean_output(outcome.pop("console"))
        statistics = scene_tools.statistics(lines)
        ok = outcome["exitReason"] == "completed" and outcome["returnCode"] == 0 and "traceSeconds" in statistics
        result: dict[str, Any] = {
            "ok": ok,
            "exitReason": outcome["exitReason"] if outcome["exitReason"] != "completed" or ok else "error",
            "benchmark": {
                "scene": f"examples/{BENCHMARK_SCENE}",
                "sceneVersion": "2.01" if b"Benchmark Vers:  2.01" in scene_bytes else "unknown",
                "sceneSha256": hashlib.sha256(scene_bytes).hexdigest(),
                "settings": "benchmark.pov documented INI options",
                "width": BENCHMARK_WIDTH,
                "height": BENCHMARK_HEIGHT,
                "deviations": ["Input_File_Name is passed on the command line", "the empty *_Command shell-out hooks are omitted"],
            },
            "threads": self.threads,
            "statistics": statistics,
            "durationMs": outcome["durationMs"],
            "pixelsPerSecond": round(BENCHMARK_WIDTH * BENCHMARK_HEIGHT / statistics["traceSeconds"], 1) if statistics.get("traceSeconds") else None,
            "diagnostics": scene_tools.diagnostics(lines, {}),
        }
        if not ok:
            start = next((index for index, line in enumerate(lines) if line.startswith("==== [")), 0)
            result["log"] = lines[start:][-LOG_TAIL_LINES:]
        return result

    def _execute(self, call_dir: str, povray: list[str], timeout_ms: int, scratch_budget: int, abandoned: Callable[[], bool] | None) -> dict[str, Any]:
        timeout_seconds = timeout_ms / 1000
        workspace = os.path.join(call_dir, "workspace")
        workspace_budget = scratch_budget + WORKSPACE_SLACK_BYTES
        argv = (
            build_limited_argv(self.memory_mb, int(timeout_seconds * self.threads) + GRACE_SECONDS, workspace_budget, self.threads)
            + build_bwrap_argv(call_dir, self.module_dir, self.library_dir)
            + povray
        )
        started = time.monotonic()
        deadline = started + timeout_seconds
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.calls_dir,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
        capture = BoundedCapture(process.stdout)
        capture.start()
        reason = "completed"
        last_scan = 0.0
        # Wake every second so a vanished caller, the deadline or a runaway workspace stops the render promptly.
        while True:
            try:
                process.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if abandoned is not None and abandoned():
                    reason = "disconnected"
                elif now >= deadline:
                    reason = "timeout"
                elif now - last_scan >= 2.0:
                    last_scan = now
                    used, count = _workspace_usage(workspace)
                    scratch_used, scratch_count = _workspace_usage(os.path.join(call_dir, "tmp"))
                    if used + scratch_used > workspace_budget or count + scratch_count > MAX_WORKSPACE_FILES:
                        reason = "workspace"
                if reason != "completed":
                    self._terminate(process)
                    process.wait()
                    break
        capture.join(timeout=5)
        console, truncated = capture.text()
        messages = {
            "disconnected": "the caller disconnected, so the render was stopped",
            "timeout": f"the render exceeded its {timeout_ms} ms wall-clock limit",
            "workspace": "the scene wrote more files or bytes into /work and /tmp than the render budget allows",
        }
        if reason in messages:
            console += f"\nRender stopped: {messages[reason]}\n"
        elif process.returncode is not None and (process.returncode < 0 or 128 < process.returncode <= 128 + 64):
            # bwrap's PID-namespace init reports a signalled child as 128 + signal.
            signal_number = -process.returncode if process.returncode < 0 else process.returncode - 128
            reason = "killed"
            console += f"\nRender stopped by signal {signal_number}; the memory, CPU or file-size limit may have been reached\n"
        return {
            "exitReason": reason,
            "returnCode": process.returncode,
            "durationMs": round((time.monotonic() - started) * 1000),
            "console": console,
            "consoleTruncated": truncated,
        }

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (ProcessLookupError, PermissionError):
            process.kill()


def smoke_test() -> None:
    """Prove that a real sandbox renders and that scene file access stays inside it. Run as the service user."""
    module_dir = os.environ.get("VANTA_RENDER_INSTALL_DIR", os.path.dirname(os.path.realpath(__file__)))
    sandbox = Sandbox(
        module_dir,
        os.environ.get("VANTA_RENDER_STATE_DIR", "/var/lib/vantamcpd-render"),
        os.path.join(module_dir, "library"),
        int(os.environ.get("VANTA_RENDER_MEMORY_MB", "512")),
        int(os.environ.get("VANTA_RENDER_THREADS", "1")),
        None,
    )
    sandbox.probe()
    limits = {"maxTimeoutMs": 120_000, "maxBackgroundTimeoutMs": 120_000}
    rendered = sandbox.run(schemas.validate_render({"scene": {"text": PROBE_SCENE + '#include "colors.inc"\n#include "textures.inc"\n'}, "width": 64, "height": 48, "output": {"mode": "inline"}, "timeoutMs": 120_000}, limits))
    assert rendered["ok"], rendered
    assert rendered["image"]["width"] == 64 and base64.b64decode(rendered["image"]["data"])[:8] == b"\x89PNG\r\n\x1a\n", rendered
    if os.path.isfile(os.path.join(module_dir, "library", "assets", "models", "vanta_model_wooden_chair.inc")):
        props = (
            PROBE_SCENE + '#include "vanta_assets.inc"\n#include "vanta_model_wooden_chair.inc"\n'
            "object { Vanta_Model_Wooden_Chair }\n#ifdef (T_Vanta_Plank_Flooring) plane { y, 0 texture { T_Vanta_Plank_Flooring rotate x*90 } } #end\n"
        )
        staged = sandbox.run(schemas.validate_render({"scene": {"text": props}, "width": 64, "height": 48, "output": {"mode": "inline"}, "timeoutMs": 120_000}, limits))
        assert staged["ok"], staged
    for probe in ('#fopen Leak "/etc/passwd" read\n', '#fopen Leak "/usr/escape.txt" write\n', '#fopen Leak "/inputs/escape.txt" write\n', '#fopen Leak "/vanta-library/escape.txt" write\n'):
        denied = sandbox.run(schemas.validate_check({"scene": {"text": PROBE_SCENE + probe}, "timeoutMs": 60_000}))
        assert not denied["ok"], f"the sandbox did not block: {probe.strip()}"
    marker = f"vanta-render-escape-{uuid.uuid4().hex}.txt"
    scratch = sandbox.run(schemas.validate_check({"scene": {"text": PROBE_SCENE + f'#fopen Note "/tmp/{marker}" write\n#write (Note, "x")\n#fclose Note\n'}, "timeoutMs": 60_000}))
    assert scratch["ok"] and not os.path.exists(f"/tmp/{marker}"), "sandbox /tmp writes reached the host"
    allowed = sandbox.run(schemas.validate_check({"scene": {"text": PROBE_SCENE + '#fopen Note "/work/note.txt" write\n#write (Note, "ok\\n")\n#fclose Note\n'}, "timeoutMs": 60_000}))
    assert allowed["ok"], allowed


def self_test() -> None:
    present = {"/usr", "/bin", "/lib", "/etc/ld.so.cache"}
    links = {"/bin": "usr/bin", "/lib": "usr/lib"}
    argv = build_bwrap_argv(
        "/state/calls/abc",
        "/opt/vantamcpd/modules/image-render/current",
        "/opt/vantamcpd/modules/image-render/current/library",
        exists=lambda path: path in present,
        islink=lambda path: path in links,
        readlink=lambda path: links[path],
    )
    for required in ("--unshare-net", "--unshare-user", "--unshare-pid", "--die-with-parent", "--new-session", "--clearenv"):
        assert required in argv, required
    assert argv[argv.index(CONF_TARGET) - 1].endswith("/povray.conf")
    assert argv[argv.index(USER_CONF_TARGET) - 1].endswith("/povray.conf")
    assert argv[argv.index("POVINI") + 1] == INI_TARGET
    assert argv[argv.index(BENCHMARK_INI_TARGET) - 1].endswith("/benchmark.ini")
    assert argv[argv.index("HOME") + 1] == "/tmp/home"
    assert "/work" in argv and "/inputs" in argv and "--share-net" not in argv
    assert "/etc" not in argv, "the host /etc must not be visible"
    request = schemas.validate_render({"scene": {"text": "x"}, "declares": {"Size": 2.5}, "clock": 0.25, "alpha": True, "bitDepth": 16}, {"maxTimeoutMs": 600_000, "maxBackgroundTimeoutMs": 600_000})
    povray = build_povray_argv(request, "/work/scene.pov", 4, ["/work", "/inputs", POV_INCLUDE])
    assert povray[:2] == [POVRAY, "+I/work/scene.pov"]
    for flag in ("-D", "-P", "+WT4", "+W800", "+H600", "+Q9", "+A0.3", "+AM1", "+R3", "+UA", "+FN16", "+K0.25", "Declare=Size=2.5", f"+O/work/{OUTPUT_NAME}"):
        assert flag in povray, flag
    check = build_povray_argv(schemas.validate_check({"scene": {"text": "x"}}), "/work/scene.pov", 1, [])
    assert "-F" in check and "-A" in check and not any(part.startswith("+O") for part in check)
    limited = build_limited_argv(512, 100, 1_000, 4)
    assert "--as=536870912" in limited and "--cpu=100" in limited and "--core=0" in limited and "--nproc=132" in limited


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--smoke-test":
        smoke_test()
    else:
        self_test()

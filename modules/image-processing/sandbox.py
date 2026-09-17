#!/usr/bin/env python3
import base64
import json
import os
import posixpath
import shutil
import subprocess
import time
import uuid
from typing import Any

import artifact_io
from schemas import MAX_ARTIFACT_BYTES, MAX_INLINE_BYTES, MAX_OUTPUT_BYTES

BWRAP = "/usr/bin/bwrap"
PRLIMIT = "/usr/bin/prlimit"
PYTHON = "/usr/bin/python3"
SYSTEM_PATHS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/usr")
EXTRA_READ_ONLY = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives", "/etc/fonts")
GRACE_SECONDS = 5


def build_limited_argv(memory_mb: int, timeout_seconds: float) -> list[str]:
    return [
        PRLIMIT,
        f"--as={memory_mb * 1024 * 1024}",
        f"--cpu={int(timeout_seconds) + GRACE_SECONDS}",
        f"--fsize={MAX_OUTPUT_BYTES}",
        "--nofile=128",
        "--nproc=32",
        "--core=0",
        "--",
    ]


def build_bwrap_argv(
    call_dir: str,
    module_dir: str,
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
        "--tmpfs", "/tmp",
        "--dir", "/vanta",
        "--ro-bind", os.path.join(module_dir, "worker.py"), "/vanta/worker.py",
        "--ro-bind", os.path.join(module_dir, "processing.py"), "/vanta/processing.py",
        "--ro-bind", os.path.join(module_dir, "schemas.py"), "/vanta/schemas.py",
        "--dir", "/vanta-policy",
        "--ro-bind", os.path.join(module_dir, "policy.xml"), "/vanta-policy/policy.xml",
        "--ro-bind", posixpath.join(call_dir, "request.json"), "/vanta-request.json",
        "--ro-bind", posixpath.join(call_dir, "inputs"), "/inputs",
        "--bind", posixpath.join(call_dir, "workspace"), "/work",
        "--chdir", "/work",
        "--clearenv",
        "--setenv", "HOME", "/work",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "LC_ALL", "C.UTF-8",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "MAGICK_CONFIGURE_PATH", "/vanta-policy",
        "--setenv", "MAGICK_TMPDIR", "/tmp",
        "--setenv", "OMP_NUM_THREADS", "1",
        "--setenv", "MALLOC_ARENA_MAX", "2",
        PYTHON, "-I", "-B", "/vanta/worker.py",
    ]
    return argv


def _source_values(tool: str, request: dict[str, Any]) -> list[dict[str, Any]]:
    if tool in ("image_inspect", "image_edit"):
        return [request["source"]]
    if tool == "image_compare":
        return [request["source"], request["reference"]]
    if tool == "image_compose":
        if request["operation"] == "overlay":
            return [request["source"], request["overlay"]]
        if request["operation"] == "watermark":
            return [request["source"]]
        return request["sources"]
    raise ValueError(f"unsupported sandbox tool: {tool}")


def _output_value(tool: str, request: dict[str, Any]) -> dict[str, Any] | None:
    if tool in ("image_edit", "image_compose"):
        return request["output"]
    if tool == "image_compare":
        return request.get("diffOutput")
    return None


class Sandbox:
    def __init__(self, module_dir: str, state_dir: str, memory_mb: int, timeout_ms: int, artifact_root: str | None):
        self.module_dir = os.path.realpath(module_dir)
        self.calls_dir = os.path.join(state_dir, "calls")
        self.memory_mb = memory_mb
        self.timeout_ms = timeout_ms
        self.artifact_root = artifact_root
        os.makedirs(self.calls_dir, mode=0o700, exist_ok=True)
        self.isolation = {
            "level": "namespaces",
            "namespaces": ["user", "mount", "pid", "ipc", "uts", "cgroup", "network"],
            "network": "none",
            "filesystem": "read-only system and inputs with a private writable workspace",
        }

    def purge_stale_calls(self) -> None:
        for name in os.listdir(self.calls_dir):
            shutil.rmtree(os.path.join(self.calls_dir, name), ignore_errors=True)

    def probe(self) -> None:
        for path in (BWRAP, PRLIMIT, PYTHON, os.path.join(self.module_dir, "worker.py"), os.path.join(self.module_dir, "policy.xml")):
            if not os.path.exists(path):
                raise RuntimeError(f"required sandbox path is missing: {path}")

    def _stage_sources(self, sources: list[dict[str, Any]], inputs_dir: str) -> list[dict[str, str]]:
        os.makedirs(inputs_dir, mode=0o700)
        artifact_entries = []
        staged: list[dict[str, str] | None] = [None] * len(sources)
        artifact_indexes = []
        inline_total = 0
        for index, source in enumerate(sources):
            name = f"source-{index}.img"
            if "artifactId" in source:
                artifact_entries.append({"artifactId": source["artifactId"], "name": name})
                artifact_indexes.append(index)
                continue
            payload = base64.b64decode(source["data"], validate=True)
            inline_total += len(payload)
            if inline_total > MAX_INLINE_BYTES * len(sources):
                raise ValueError("combined inline image input is too large")
            path = os.path.join(inputs_dir, name)
            with open(path, "xb") as handle:
                handle.write(payload)
            os.chmod(path, 0o400)
            staged[index] = {"path": f"/inputs/{name}", "mimeType": source["mimeType"]}
        if artifact_entries:
            if not artifact_io.available(self.artifact_root):
                raise ValueError("shared artifact storage is unavailable on this node")
            artifact_sources = artifact_io.stage_inputs(self.artifact_root, artifact_entries, inputs_dir, MAX_ARTIFACT_BYTES)
            for index, source in zip(artifact_indexes, artifact_sources):
                staged[index] = source
        return [source for source in staged if source is not None]

    def run(self, tool: str, request: dict[str, Any], backend: str) -> dict[str, Any]:
        call_dir = os.path.join(self.calls_dir, uuid.uuid4().hex)
        inputs_dir = os.path.join(call_dir, "inputs")
        workspace = os.path.join(call_dir, "workspace")
        os.makedirs(workspace, mode=0o700)
        reservation_id = None
        try:
            sources = self._stage_sources(_source_values(tool, request), inputs_dir)
            output = _output_value(tool, request)
            if output and output["mode"] == "artifact":
                if not artifact_io.available(self.artifact_root):
                    raise ValueError("shared artifact storage is unavailable for artifact output")
                reservation_id = artifact_io.reserve(self.artifact_root, "image-processing", MAX_OUTPUT_BYTES)
            worker_request = {
                "tool": tool,
                "request": request,
                "sources": sources,
                "backend": backend,
                "timeoutSeconds": self.timeout_ms / 1000,
            }
            with open(os.path.join(call_dir, "request.json"), "w", encoding="utf-8") as handle:
                json.dump(worker_request, handle, ensure_ascii=False, separators=(",", ":"))
            result = self._execute(call_dir)
            value = result["value"]
            descriptor = value.get("output")
            if descriptor:
                host_path = self._output_host_path(workspace, descriptor["path"])
                if descriptor["mode"] == "inline":
                    size = os.path.getsize(host_path)
                    if size > MAX_INLINE_BYTES:
                        raise ValueError(f"inline image output exceeds {MAX_INLINE_BYTES} bytes; use artifact output")
                    with open(host_path, "rb") as handle:
                        data = base64.b64encode(handle.read()).decode("ascii")
                    value["output"] = {key: descriptor[key] for key in ("name", "mimeType", "bytes", "mode")}
                    value["output"].update({"encoding": "base64", "data": data})
                else:
                    value["output"] = artifact_io.publish(
                        self.artifact_root,
                        reservation_id,
                        host_path,
                        descriptor["name"],
                        descriptor["mimeType"],
                        output["retentionDays"],
                    )
                    reservation_id = None
            value["durationMs"] = result["durationMs"]
            value["isolation"] = self.isolation
            return value
        finally:
            artifact_io.release(self.artifact_root, reservation_id)
            shutil.rmtree(call_dir, ignore_errors=True)

    @staticmethod
    def _output_host_path(workspace: str, sandbox_path: str) -> str:
        if not isinstance(sandbox_path, str) or not sandbox_path.startswith("/work/output.") or "/" in sandbox_path[6:]:
            raise ValueError("worker output path escaped the workspace")
        path = os.path.join(workspace, sandbox_path[6:])
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError("worker output is missing or unsafe")
        return path

    def _execute(self, call_dir: str) -> dict[str, Any]:
        timeout_seconds = self.timeout_ms / 1000
        argv = build_limited_argv(self.memory_mb, timeout_seconds) + build_bwrap_argv(call_dir, self.module_dir)
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.calls_dir,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds + GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise ValueError("image operation exceeded its wall-clock limit")
        duration_ms = round((time.monotonic() - started) * 1000)
        if stdout:
            raise ValueError("sandbox worker wrote unexpected standard output")
        result_path = os.path.join(call_dir, "workspace", ".vanta-result.json")
        try:
            with open(result_path, "r", encoding="utf-8") as handle:
                result = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError) as error:
            detail = stderr[:2_000].decode("utf-8", errors="replace").strip()
            raise ValueError(f"sandbox worker failed before returning a result: {detail or process.returncode}") from error
        if result.get("status") != "completed":
            error = result.get("error", {})
            raise ValueError(str(error.get("message", "image operation failed")))
        if process.returncode != 0:
            raise ValueError(f"sandbox worker exited with status {process.returncode}")
        result["durationMs"] = result.get("durationMs") or duration_ms
        return result


def self_test() -> None:
    present = {"/usr", "/bin", "/lib", "/etc/fonts"}
    links = {"/bin": "usr/bin", "/lib": "usr/lib"}
    argv = build_bwrap_argv(
        "/state/calls/abc",
        "/opt/vantamcpd/modules/image-processing/current",
        exists=lambda path: path in present,
        islink=lambda path: path in links,
        readlink=lambda path: links[path],
    )
    for required in ("--unshare-net", "--unshare-user", "--unshare-pid", "--die-with-parent", "--clearenv"):
        assert required in argv
    assert argv[-4:] == [PYTHON, "-I", "-B", "/vanta/worker.py"]
    assert "/inputs" in argv and "/work" in argv and "/vanta-policy/policy.xml" in argv
    assert "--share-net" not in argv
    limited = build_limited_argv(256, 30)
    assert "--as=268435456" in limited and "--core=0" in limited
    request = {"source": {"artifactId": "a" * 32}, "reference": {"artifactId": "b" * 32}}
    assert len(_source_values("image_compare", request)) == 2


def smoke_test() -> None:
    module_dir = os.environ.get("VANTA_IMAGE_INSTALL_DIR", os.path.dirname(os.path.realpath(__file__)))
    state_dir = os.environ.get("VANTA_IMAGE_STATE_DIR", "/var/lib/vantamcpd-image")
    sandbox = Sandbox(module_dir, state_dir, int(os.environ.get("VANTA_IMAGE_MEMORY_MB", "384")), 30_000, None)
    sandbox.probe()
    tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    result = sandbox.run("image_edit", {
        "source": {"data": tiny_png, "mimeType": "image/png"},
        "edits": [{"operation": "resize", "width": 2, "height": 2, "mode": "stretch"}],
        "output": {"mode": "inline", "format": "png", "quality": 85, "stripMetadata": True},
    }, os.environ.get("VANTA_IMAGE_BACKEND", "auto"))
    assert result["output"]["mimeType"] == "image/png"
    assert result["output"]["bytes"] > 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke-test":
        smoke_test()
    else:
        self_test()
#!/usr/bin/env python3
"""Sandboxed execution for Python Compute.

Each call runs a fresh `python3` under bubblewrap: private mount, PID, IPC, UTS and user namespaces,
an empty network namespace, a read-only system view, a per-call writable working directory, and POSIX
resource limits. Nothing from the call survives except the bounded result the broker reads back.
"""
import base64
import json
import os
import posixpath
import shutil
import subprocess
import time
import uuid
from typing import Any

BWRAP = "/usr/bin/bwrap"
PRLIMIT = "/usr/bin/prlimit"
PYTHON = "/usr/bin/python3"
WORK_MOUNT = "/work"
SYSTEM_PATHS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/usr")
EXTRA_READ_ONLY = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives", "/etc/ssl/certs", "/etc/fonts", "/etc/matplotlibrc")
GRACE_SECONDS = 5
MAX_PROCESS_OUTPUT_BYTES = 8_192
FILE_LIMIT_BYTES = 32 * 1024 * 1024


def _read_bounded(path: str, limit: int) -> tuple[str, bool]:
    try:
        with open(path, "rb") as handle:
            payload = handle.read(limit + 1)
    except FileNotFoundError:
        return "", False
    truncated = len(payload) > limit
    return payload[:limit].decode("utf-8", errors="replace"), truncated


def build_limited_argv(memory_mb: int, timeout_seconds: float) -> list[str]:
    """POSIX resource limits applied to the sandbox. prlimit avoids preexec_fn in a threaded broker."""
    return [
        PRLIMIT,
        f"--as={memory_mb * 1024 * 1024}",
        f"--cpu={int(timeout_seconds) + GRACE_SECONDS}",
        f"--fsize={FILE_LIMIT_BYTES}",
        "--nofile=256",
        "--nproc=96",
        "--core=0",
        "--",
    ]


def build_bwrap_argv(call_dir: str, runner_path: str, system_paths=SYSTEM_PATHS, extra_read_only=EXTRA_READ_ONLY, exists=os.path.exists, islink=os.path.islink, readlink=os.readlink) -> list[str]:
    """Argument vector for one sandboxed interpreter. Injectable probes keep this unit-testable."""
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
        "--ro-bind", runner_path, "/vanta-runner.py",
        "--ro-bind", posixpath.join(call_dir, "request.json"), "/vanta-request.json",
        "--bind", posixpath.join(call_dir, "workspace"), "/work",
        "--bind", posixpath.join(call_dir, "mplconfig"), "/mplconfig",
        "--chdir", "/work",
        "--clearenv",
        "--setenv", "HOME", "/work",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "LC_ALL", "C.UTF-8",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "MPLBACKEND", "Agg",
        "--setenv", "MPLCONFIGDIR", "/mplconfig",
        "--setenv", "OPENBLAS_NUM_THREADS", "1",
        "--setenv", "OMP_NUM_THREADS", "1",
        "--setenv", "MKL_NUM_THREADS", "1",
        "--setenv", "NUMEXPR_NUM_THREADS", "1",
        "--setenv", "MALLOC_ARENA_MAX", "2",
        PYTHON, "-I", "-B", "/vanta-runner.py",
    ]
    return argv


class Sandbox:
    def __init__(self, install_dir: str, state_dir: str, default_memory_mb: int = 384):
        self.install_dir = os.path.realpath(install_dir)
        self.state_dir = state_dir
        self.default_memory_mb = default_memory_mb
        self.calls_dir = os.path.join(state_dir, "calls")
        os.makedirs(self.calls_dir, mode=0o700, exist_ok=True)
        self.isolation: dict[str, Any] = {"level": "unverified", "namespaces": [], "network": "none"}

    @property
    def runner_path(self) -> str:
        return os.path.join(self.install_dir, "runner.py")

    def purge_stale_calls(self) -> None:
        for name in os.listdir(self.calls_dir):
            shutil.rmtree(os.path.join(self.calls_dir, name), ignore_errors=True)

    def probe(self) -> dict[str, Any]:
        """Verify that an unprivileged sandbox can actually be created. Called once at broker start."""
        result = self.run({"mode": "check", "imports": ["json"], "timeoutMs": 20_000, "memoryMb": 256, "maxStdoutBytes": 1_024, "artifacts": False, "code": "", "inputs": {}, "files": [], "maxArtifacts": 0, "maxArtifactBytes": 0, "maxArtifactTotalBytes": 0})
        if result.get("exitReason") != "completed":
            detail = "; ".join(str(part) for part in [result.get("error", {}).get("message"), result.get("diagnostics"), result.get("stderr")] if part)
            raise RuntimeError(f"sandbox probe failed: {detail or result.get('exitReason')}")
        self.isolation = {
            "level": "namespaces",
            "namespaces": ["user", "mount", "pid", "ipc", "uts", "cgroup", "network"],
            "network": "none",
            "filesystem": "read-only system view with an empty per-call working directory",
            "note": "Submitted code is isolated by bubblewrap namespaces and POSIX resource limits, not by a hostile-code security boundary.",
        }
        return self.isolation

    def check(self, imports: list[str]) -> dict[str, Any]:
        request = {
            "mode": "check",
            "imports": imports,
            "timeoutMs": 60_000,
            "memoryMb": self.default_memory_mb,
            "maxStdoutBytes": 4_096,
            "artifacts": False,
            "inputs": {},
            "files": [],
            "maxArtifacts": 0,
            "maxArtifactBytes": 0,
            "maxArtifactTotalBytes": 0,
        }
        outcome = self.run(request)
        if outcome["exitReason"] != "completed":
            raise ValueError(outcome.get("error", {}).get("message") or "import check failed")
        return {"imports": outcome.get("imports", []), "durationMs": outcome["durationMs"]}

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        call_dir = os.path.join(self.calls_dir, uuid.uuid4().hex)
        workspace = os.path.join(call_dir, "workspace")
        os.makedirs(workspace, mode=0o700)
        try:
            self._prepare_matplotlib(call_dir)
            for entry in request.get("files", []):
                with open(os.path.join(workspace, entry["name"]), "w", encoding="utf-8") as handle:
                    handle.write(entry["text"])
            with open(os.path.join(call_dir, "request.json"), "w", encoding="utf-8") as handle:
                json.dump(request, handle, ensure_ascii=False)
            return self._execute(call_dir, workspace, request)
        finally:
            shutil.rmtree(call_dir, ignore_errors=True)

    def _prepare_matplotlib(self, call_dir: str) -> None:
        target = os.path.join(call_dir, "mplconfig")
        source = os.path.join(self.install_dir, "mplconfig")
        if os.path.isdir(source):
            shutil.copytree(source, target)
        else:
            os.makedirs(target, mode=0o700)

    def _execute(self, call_dir: str, workspace: str, request: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = request["timeoutMs"] / 1000
        argv = build_limited_argv(request["memoryMb"], timeout_seconds) + build_bwrap_argv(call_dir, self.runner_path)
        started = time.monotonic()
        killed = False
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin"},
            cwd=self.state_dir,
            start_new_session=True,
        )
        try:
            process_stdout, process_stderr = process.communicate(timeout=timeout_seconds + GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            killed = True
            self._terminate(process)
            process_stdout, process_stderr = process.communicate()
        duration_ms = round((time.monotonic() - started) * 1000)

        outcome = self._collect(workspace, request)
        outcome["durationMs"] = outcome.get("durationMs") or duration_ms
        outcome["totalDurationMs"] = duration_ms
        if outcome.get("exitReason") is None:
            diagnostics = (process_stderr or b"")[:MAX_PROCESS_OUTPUT_BYTES].decode("utf-8", errors="replace").strip()
            outcome["exitReason"] = "timeout" if killed else ("memory" if process.returncode == -9 else "killed")
            outcome["error"] = {
                "type": "SandboxError",
                "message": "the sandboxed process was stopped before it produced a result"
                + (f" (exit {process.returncode})" if process.returncode is not None else ""),
                "traceback": "",
            }
            if diagnostics:
                outcome["diagnostics"] = diagnostics
        outcome["ok"] = outcome["exitReason"] == "completed"
        outcome["isolation"] = self.isolation
        return outcome

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (ProcessLookupError, PermissionError):
            process.kill()

    def _collect(self, workspace: str, request: dict[str, Any]) -> dict[str, Any]:
        stdout_limit = request["maxStdoutBytes"]
        stdout, stdout_truncated = _read_bounded(os.path.join(workspace, ".vanta-stdout"), stdout_limit)
        stderr, stderr_truncated = _read_bounded(os.path.join(workspace, ".vanta-stderr"), stdout_limit)
        outcome: dict[str, Any] = {
            "exitReason": None,
            "stdout": stdout,
            "stdoutTruncated": stdout_truncated,
            "stderr": stderr,
            "stderrTruncated": stderr_truncated,
        }
        try:
            with open(os.path.join(workspace, ".vanta-result.json"), "r", encoding="utf-8") as handle:
                result = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError):
            return outcome

        status = result.get("status")
        outcome["exitReason"] = status if status in ("completed", "error", "timeout") else "killed"
        if status == "error" and (result.get("error") or {}).get("type") == "MemoryError":
            outcome["exitReason"] = "memory"
        outcome["durationMs"] = result.get("durationMs")
        if "imports" in result:
            outcome["imports"] = result["imports"]
        else:
            outcome["hasResult"] = bool(result.get("hasResult"))
            outcome["result"] = result.get("result")
            outcome["error"] = result.get("error")
            outcome["artifacts"] = self._encode_artifacts(result.get("artifacts", []), request, workspace)
        return outcome

    @staticmethod
    def _encode_artifacts(artifacts: list[dict[str, Any]], request: dict[str, Any], workspace: str) -> list[dict[str, Any]]:
        encoded = []
        budget = request.get("maxArtifactTotalBytes", 0)
        for artifact in artifacts[: request.get("maxArtifacts", 0)]:
            # The harness records paths as the sandbox sees them; /work is this call's workspace.
            path = artifact.get("path", "")
            if not path.startswith(WORK_MOUNT + "/"):
                continue
            host_path = os.path.join(workspace, *path[len(WORK_MOUNT) + 1 :].split("/"))
            try:
                with open(host_path, "rb") as handle:
                    payload = handle.read(min(request.get("maxArtifactBytes", 0), max(budget, 0)) + 1)
            except OSError:
                continue
            truncated = len(payload) > request.get("maxArtifactBytes", 0) or len(payload) > budget
            payload = payload[: min(request.get("maxArtifactBytes", 0), max(budget, 0))]
            budget -= len(payload)
            encoded.append({
                "name": artifact.get("name"),
                "kind": artifact.get("kind"),
                "mimeType": artifact.get("mimeType"),
                "bytes": len(payload),
                "encoding": "base64",
                "data": base64.b64encode(payload).decode("ascii"),
                "truncated": truncated,
            })
        return encoded


def smoke_test() -> None:
    """Prove that a real sandbox starts, isolates, and returns a result. Run as the service user."""
    sandbox = Sandbox(os.environ.get("VANTA_PYTHON_INSTALL_DIR", os.path.dirname(os.path.realpath(__file__))), os.environ.get("VANTA_PYTHON_STATE_DIR", "/var/lib/vantamcpd-python"))
    sandbox.probe()
    outcome = sandbox.run({
        "code": "import socket, os\nprint('sandbox ready')\nvanta.result({'home': os.environ['HOME'], 'net': hasattr(socket, 'AF_INET')})",
        "inputs": {},
        "files": [],
        "timeoutMs": 30_000,
        "memoryMb": 256,
        "artifacts": True,
        "maxStdoutBytes": 4_096,
        "maxArtifacts": 1,
        "maxArtifactBytes": 1_000,
        "maxArtifactTotalBytes": 1_000,
    })
    assert outcome["exitReason"] == "completed", outcome
    assert outcome["stdout"].strip() == "sandbox ready", outcome
    assert outcome["result"]["home"] == "/work", outcome
    isolated = sandbox.run({
        "code": "import urllib.request\nurllib.request.urlopen('http://192.0.2.1/', timeout=3)",
        "inputs": {},
        "files": [],
        "timeoutMs": 20_000,
        "memoryMb": 256,
        "artifacts": False,
        "maxStdoutBytes": 1_024,
        "maxArtifacts": 0,
        "maxArtifactBytes": 0,
        "maxArtifactTotalBytes": 0,
    })
    assert isolated["exitReason"] in ("error", "timeout"), isolated


def self_test() -> None:
    present = {"/usr", "/bin", "/lib", "/etc/ld.so.cache"}
    links = {"/bin": "usr/bin", "/lib": "usr/lib"}
    argv = build_bwrap_argv(
        "/var/lib/vantamcpd-python/calls/abc",
        "/opt/vantamcpd/modules/python-compute/current/runner.py",
        exists=lambda path: path in present,
        islink=lambda path: path in links,
        readlink=lambda path: links[path],
    )
    assert argv[0] == BWRAP
    assert argv[-4:] == [PYTHON, "-I", "-B", "/vanta-runner.py"]
    for required in ("--unshare-net", "--unshare-user", "--unshare-pid", "--die-with-parent", "--new-session", "--clearenv", "--proc", "--dev"):
        assert required in argv, required
    assert argv[argv.index("--symlink") + 1 : argv.index("--symlink") + 3] == ["usr/bin", "/bin"]
    assert "--ro-bind" in argv and "/usr" in argv
    assert "/lib32" not in argv, "absent system paths must not be bound"
    assert argv[argv.index("--bind") + 1 : argv.index("--bind") + 3] == ["/var/lib/vantamcpd-python/calls/abc/workspace", "/work"]
    for setting in ("MPLBACKEND", "OPENBLAS_NUM_THREADS", "MALLOC_ARENA_MAX", "HOME"):
        assert setting in argv, setting
    assert "--share-net" not in argv

    request = {"maxArtifacts": 2, "maxArtifactBytes": 10, "maxArtifactTotalBytes": 12}
    assert Sandbox._encode_artifacts([{"path": "/work/.vanta-artifacts/00-a.txt", "name": "a"}], request, "/state/calls/x/workspace") == []
    assert Sandbox._encode_artifacts([{"path": "/etc/shadow", "name": "a"}], request, "/state/calls/x/workspace") == []

    limited = build_limited_argv(384, 30)
    assert limited[0] == PRLIMIT and limited[-1] == "--"
    assert "--as=402653184" in limited
    assert f"--cpu={30 + GRACE_SECONDS}" in limited
    assert "--core=0" in limited


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--smoke-test":
        smoke_test()
    else:
        self_test()

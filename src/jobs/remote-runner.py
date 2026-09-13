#!/usr/bin/env python3
import fcntl
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROGRESS_PREFIX = "VANTA_PROGRESS "
HEARTBEAT_SECONDS = 10


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"), ensure_ascii=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def finish(state_path, state, status, retention_ms, **values):
    now = datetime.now(timezone.utc)
    state.update(values)
    state["status"] = status
    state["heartbeatAt"] = now.isoformat().replace("+00:00", "Z")
    state["finishedAt"] = state["heartbeatAt"]
    state["expiresAt"] = (now + timedelta(milliseconds=retention_ms)).isoformat().replace("+00:00", "Z")
    write_json(state_path, state)


def cancel(spec_path):
    spec = read_json(spec_path)
    state_path = spec_path.with_name("state.json")
    state = read_json(state_path)
    if state.get("status") not in ("succeeded", "failed", "canceled"):
        finish(state_path, state, "canceled", spec["retentionMs"], error="Canceled by operator.")


def fail(spec_path, message):
    spec = read_json(spec_path)
    state_path = spec_path.with_name("state.json")
    state = read_json(state_path)
    if state.get("status") not in ("succeeded", "failed", "canceled"):
        finish(state_path, state, "failed", spec["retentionMs"], error=message[:4000])


def acquire_locks(resource_keys):
    lock_dir = Path("/run/lock/vantamcpd")
    lock_dir.mkdir(parents=True, exist_ok=True)
    handles = []
    for key in sorted(resource_keys):
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()
        handle = (lock_dir / f"{name}.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            for held in handles:
                held.close()
            handle.close()
            raise RuntimeError(f"Resource is already locked: {key}")
        handles.append(handle)
    return handles


def apply_progress(state, line):
    if not line.startswith(PROGRESS_PREFIX):
        return False
    try:
        value = json.loads(line[len(PROGRESS_PREFIX):])
    except json.JSONDecodeError:
        return False
    if not isinstance(value, dict):
        return False
    progress = {}
    if isinstance(value.get("current"), (int, float)) and value["current"] >= 0:
        progress["current"] = value["current"]
    if isinstance(value.get("total"), (int, float)) and value["total"] > 0:
        progress["total"] = value["total"]
    if isinstance(value.get("unit"), str):
        progress["unit"] = value["unit"][:32]
    if isinstance(value.get("message"), str):
        progress["message"] = value["message"][:500]
    if isinstance(value.get("phase"), str) and value["phase"]:
        state["phase"] = value["phase"][:100]
    if progress:
        state["progress"] = progress
    return True


def run(spec_path):
    spec = read_json(spec_path)
    state_path = spec_path.with_name("state.json")
    log_path = spec_path.with_name("job.log")
    state = read_json(state_path)
    if state.get("status") in ("succeeded", "failed", "canceled"):
        return

    locks = []
    process = None
    interrupted = False

    def stop(_signal, _frame):
        nonlocal interrupted
        interrupted = True
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        locks = acquire_locks(spec["resourceKeys"])
        state.update({"status": "running", "phase": "executing", "startedAt": state.get("startedAt") or timestamp(), "heartbeatAt": timestamp()})
        state.pop("finishedAt", None)
        state.pop("expiresAt", None)
        state.pop("error", None)
        state.pop("result", None)
        write_json(state_path, state)

        environment = os.environ.copy()
        environment.update(spec.get("environment", {}))
        process = subprocess.Popen(
            spec["command"], cwd=spec["cwd"], env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + spec["timeoutMs"] / 1000
        next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
        buffered = b""
        logged = log_path.stat().st_size if log_path.exists() else 0

        with log_path.open("ab") as log:
            while process.poll() is None:
                if interrupted:
                    break
                if time.monotonic() >= deadline:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    raise TimeoutError(f"Job exceeded {spec['timeoutMs']}ms runtime limit")
                for key, _ in selector.select(timeout=1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        continue
                    room = max(0, spec["maxLogBytes"] - logged)
                    if room:
                        saved = chunk[:room]
                        log.write(saved)
                        log.flush()
                        logged += len(saved)
                    if len(chunk) > room:
                        state["logTruncated"] = True
                    buffered += chunk
                    while b"\n" in buffered:
                        raw_line, buffered = buffered.split(b"\n", 1)
                        if apply_progress(state, raw_line.decode("utf-8", errors="replace")):
                            state["heartbeatAt"] = timestamp()
                            write_json(state_path, state)
                if time.monotonic() >= next_heartbeat:
                    state["heartbeatAt"] = timestamp()
                    write_json(state_path, state)
                    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

        code = process.wait()
        if interrupted:
            raise SystemExit(75)
        elif code == 0:
            finish(state_path, state, "succeeded", spec["retentionMs"], result={"exitCode": 0, "summary": "Job completed."})
        else:
            finish(state_path, state, "failed", spec["retentionMs"], error=f"Job command exited with code {code}.", result={"exitCode": code})
    except Exception as error:
        finish(state_path, state, "failed", spec["retentionMs"], error=str(error)[:4000])
    finally:
        for handle in locks:
            handle.close()


def main():
    if len(sys.argv) not in (2, 3, 4):
        raise SystemExit("usage: remote-runner.py SPEC [--cancel | --fail MESSAGE]")
    spec_path = Path(sys.argv[1]).resolve()
    if spec_path.name != "spec.json" or spec_path.parent.parent != Path("/var/lib/vantamcpd/jobs"):
        raise SystemExit("invalid job specification path")
    if len(sys.argv) == 3 and sys.argv[2] == "--cancel":
        cancel(spec_path)
    elif len(sys.argv) == 4 and sys.argv[2] == "--fail":
        fail(spec_path, sys.argv[3])
    elif len(sys.argv) != 2:
        raise SystemExit("unknown option")
    else:
        run(spec_path)


if __name__ == "__main__":
    main()
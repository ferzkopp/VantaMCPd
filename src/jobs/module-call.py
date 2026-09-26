#!/usr/bin/env python3
"""Run one module tool call inside a durable job and publish its MCP result to out/result.json."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
PROGRESS_PREFIX = "VANTA_PROGRESS "
EXIT_TOOL_ERROR = 3
MAX_STDERR_BYTES = 64 * 1024


def progress(phase, message):
    print(PROGRESS_PREFIX + json.dumps({"phase": phase, "message": message[:500]}), flush=True)


def write_result(out_dir, value):
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    temporary = out_dir / f".result.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(data)
    os.replace(temporary, out_dir / "result.json")


def error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


class ModuleSession:
    def __init__(self, entrypoint, max_output_bytes):
        self.process = subprocess.Popen(
            entrypoint, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.max_output_bytes = max_output_bytes
        self.output_bytes = 0
        self.lines = queue.Queue()
        self.stderr = bytearray()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.process.stdout:
            self.output_bytes += len(line)
            if self.output_bytes > self.max_output_bytes:
                self.lines.put(OverflowError(f"module output exceeds {self.max_output_bytes} bytes"))
                return
            self.lines.put(line)
        self.lines.put(None)

    def _read_stderr(self):
        for chunk in iter(lambda: self.process.stderr.read(4096), b""):
            room = MAX_STDERR_BYTES - len(self.stderr)
            if room > 0:
                self.stderr.extend(chunk[:room])

    def send(self, message):
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def response(self, request_id, timeout_seconds):
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"module did not answer within {round(timeout_seconds * 1000)}ms")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if isinstance(line, Exception):
                raise line
            if line is None:
                detail = self.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError("module exited before answering" + (f": {detail}" if detail else ""))
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id and ("result" in message or "error" in message):
                return message

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def run(out_dir):
    request = json.loads((out_dir / "input.json").read_text(encoding="utf-8"))
    session = None
    try:
        progress("starting", f"starting {request['moduleId']}")
        session = ModuleSession(request["entrypoint"], request["maxOutputBytes"])
        session.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "vantamcpd-job", "version": "1"}},
        })
        initialized = session.response(1, request["startupMs"] / 1000)
        if "error" in initialized:
            raise RuntimeError(f"module initialization failed: {initialized['error'].get('message', 'unknown error')}")
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        progress("calling", f"calling {request['toolName']}")
        session.send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": request["toolName"], "arguments": request["arguments"], "_meta": {"vantamcpd/execution": "background"}},
        })
        answer = session.response(2, request["callMs"] / 1000)
        result = answer.get("result")
        if not isinstance(result, dict):
            result = error_result(str(answer.get("error", {}).get("message", "module returned no result")))
    except Exception as error:
        result = error_result(str(error))
    finally:
        if session is not None:
            session.close()
    write_result(out_dir, result)
    failed = result.get("isError") is True
    progress("failed" if failed else "finished", "tool reported an error" if failed else "tool call completed")
    return EXIT_TOOL_ERROR if failed else 0


def fake_server():
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            reply = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
        elif message.get("method") == "tools/call":
            params = message["params"]
            text = json.dumps({"echo": params["arguments"], "execution": params["_meta"]["vantamcpd/execution"]})
            reply = {"content": [{"type": "text", "text": text}], "isError": params["name"] == "fail"}
        else:
            continue
        print("log line that is not JSON-RPC", flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": reply}), flush=True)


def self_test():
    with tempfile.TemporaryDirectory() as directory:
        out_dir = Path(directory)
        base = {"moduleId": "fake", "entrypoint": [sys.executable, __file__, "--fake-server"], "startupMs": 10_000, "callMs": 10_000, "maxOutputBytes": 65_536}
        (out_dir / "input.json").write_text(json.dumps({**base, "toolName": "echo", "arguments": {"value": 7}}), encoding="utf-8")
        assert run(out_dir) == 0
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        assert json.loads(result["content"][0]["text"]) == {"echo": {"value": 7}, "execution": "background"}

        (out_dir / "input.json").write_text(json.dumps({**base, "toolName": "fail", "arguments": {}}), encoding="utf-8")
        assert run(out_dir) == EXIT_TOOL_ERROR

        (out_dir / "input.json").write_text(json.dumps({**base, "toolName": "echo", "arguments": {}, "maxOutputBytes": 16}), encoding="utf-8")
        assert run(out_dir) == EXIT_TOOL_ERROR
        assert "exceeds" in json.loads((out_dir / "result.json").read_text(encoding="utf-8"))["content"][0]["text"]
    print("module-call self-test passed")


def main():
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    elif sys.argv[1:] == ["--fake-server"]:
        fake_server()
    elif len(sys.argv) == 2:
        raise SystemExit(run(Path(sys.argv[1])))
    elif len(sys.argv) == 1 and os.environ.get("VANTA_JOB_OUT"):
        raise SystemExit(run(Path(os.environ["VANTA_JOB_OUT"])))
    else:
        raise SystemExit("usage: module-call.py [OUT_DIR] | --self-test")


if __name__ == "__main__":
    main()

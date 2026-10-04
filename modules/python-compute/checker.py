#!/usr/bin/env python3
"""Run the bundled Pyright CLI and normalize its JSON diagnostics."""
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any

REQUEST_PATH = "/vanta-request.json"
WORKSPACE = "/work"
PYRIGHT = "/vanta-pyright/index.js"
MAX_DIAGNOSTICS = 1_000
MAX_PYRIGHT_OUTPUT_BYTES = 8 * 1024 * 1024


def normalize(payload: dict[str, Any], workspace: str = WORKSPACE) -> dict[str, Any]:
    diagnostics = []
    prefix = workspace.rstrip("/") + "/"
    for item in payload.get("generalDiagnostics", [])[:MAX_DIAGNOSTICS]:
        path = str(item.get("file", ""))
        if path.startswith(prefix):
            path = path[len(prefix):]
        diagnostics.append({
            "file": path,
            "severity": item.get("severity"),
            "message": item.get("message"),
            "rule": item.get("rule"),
            "range": item.get("range"),
        })
    summary = payload.get("summary", {})
    return {
        "ok": not summary.get("errorCount", 0),
        "engine": {"name": "Pyright", "relationship": "Static-analysis engine that powers Pylance"},
        "version": payload.get("version"),
        "timeSeconds": summary.get("timeInSec"),
        "summary": {
            "filesAnalyzed": summary.get("filesAnalyzed", 0),
            "errors": summary.get("errorCount", 0),
            "warnings": summary.get("warningCount", 0),
            "information": summary.get("informationCount", 0),
        },
        "diagnostics": diagnostics,
        "diagnosticsTruncated": len(payload.get("generalDiagnostics", [])) > MAX_DIAGNOSTICS,
    }


def run(request: dict[str, Any]) -> dict[str, Any]:
    config = {
        "include": [entry["name"] for entry in request["files"]],
        "pythonVersion": request["pythonVersion"],
        "typeCheckingMode": request["typeCheckingMode"],
        "useLibraryCodeForTypes": True,
    }
    config_path = os.path.join(WORKSPACE, ".vanta-pyrightconfig.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        completed = subprocess.run(
            ["/usr/bin/node", PYRIGHT, "--outputjson", "--project", config_path],
            cwd=WORKSPACE,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
        stdout.seek(0)
        stderr.seek(0)
        stdout_payload = stdout.read(MAX_PYRIGHT_OUTPUT_BYTES + 1)
        stderr_payload = stderr.read(MAX_PYRIGHT_OUTPUT_BYTES + 1)
    if len(stdout_payload) > MAX_PYRIGHT_OUTPUT_BYTES or len(stderr_payload) > MAX_PYRIGHT_OUTPUT_BYTES:
        raise RuntimeError("Pyright output exceeded the 8 MiB internal limit")
    if completed.returncode not in (0, 1):
        detail = stderr_payload.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"Pyright exited with status {completed.returncode}")
    try:
        payload = json.loads(stdout_payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("Pyright returned invalid JSON") from error
    return normalize(payload)


def main() -> None:
    started = time.monotonic()
    try:
        with open(REQUEST_PATH, "r", encoding="utf-8") as handle:
            request = json.load(handle)
        result = run(request)
        envelope = {"status": "completed", "hasResult": True, "result": result}
    except Exception as error:
        envelope = {
            "status": "error",
            "hasResult": False,
            "error": {"type": type(error).__name__, "message": str(error), "traceback": ""},
        }
    envelope["durationMs"] = round((time.monotonic() - started) * 1000)
    with open(os.path.join(WORKSPACE, ".vanta-result.json"), "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False)


def self_test() -> None:
    sample = {
        "version": "1.2.3",
        "time": "0.01sec",
        "generalDiagnostics": [{
            "file": "/work/main.py",
            "severity": "error",
            "message": "Expression is not allowed here",
            "rule": "reportGeneralTypeIssues",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
        }],
        "summary": {"filesAnalyzed": 1, "errorCount": 1, "warningCount": 0, "informationCount": 0},
    }
    result = normalize(sample)
    assert result["ok"] is False
    assert result["diagnostics"][0]["file"] == "main.py"
    assert result["summary"]["errors"] == 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        main()

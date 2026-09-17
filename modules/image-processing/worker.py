#!/usr/bin/env python3
"""Sandbox entrypoint for one validated image operation."""
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import processing
from schemas import MAX_OUTPUT_BYTES

REQUEST_PATH = "/vanta-request.json"
RESULT_PATH = "/work/.vanta-result.json"
FORMAT_MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def _output_path(output: dict[str, Any]) -> str:
    return f"/work/output.{output['format']}"


def _output_descriptor(output: dict[str, Any], path: str) -> dict[str, Any]:
    size = os.path.getsize(path)
    if size > MAX_OUTPUT_BYTES:
        raise ValueError(f"image output exceeds {MAX_OUTPUT_BYTES} bytes")
    return {
        "path": path,
        "name": output.get("name", f"image.{output['format']}"),
        "mimeType": FORMAT_MIME[output["format"]],
        "bytes": size,
        "mode": output["mode"],
    }


def execute(message: dict[str, Any]) -> dict[str, Any]:
    tool = message["tool"]
    request = message["request"]
    sources = message.get("sources", [])
    preference = message["backend"]
    timeout_seconds = float(message["timeoutSeconds"])
    if tool == "image_inspect":
        inspect_request = {**request, "mimeType": sources[0].get("mimeType") or None}
        return processing.inspect_image(sources[0]["path"], inspect_request, preference, timeout_seconds)
    if tool == "image_edit":
        path = _output_path(request["output"])
        backend = processing.edit_image(sources[0]["path"], request["edits"], path, request["output"], preference, timeout_seconds)
        return {"backend": backend, "output": _output_descriptor(request["output"], path)}
    if tool == "image_compose":
        path = _output_path(request["output"])
        backend = processing.compose_image(request, [source["path"] for source in sources], path, preference, timeout_seconds)
        return {"backend": backend, "output": _output_descriptor(request["output"], path)}
    if tool == "image_compare":
        path = _output_path(request["diffOutput"]) if request.get("diffOutput") else None
        result = processing.compare_images(sources[0]["path"], sources[1]["path"], request, path)
        if path:
            result["output"] = _output_descriptor(request["diffOutput"], path)
        return result
    raise ValueError(f"unsupported sandbox tool: {tool}")


def run(request_path: str = REQUEST_PATH, result_path: str = RESULT_PATH) -> None:
    started = time.monotonic()
    try:
        with open(request_path, "r", encoding="utf-8") as handle:
            message = json.load(handle)
        if not isinstance(message, dict):
            raise ValueError("worker request must be an object")
        value = execute(message)
        result = {"status": "completed", "durationMs": round((time.monotonic() - started) * 1000), "value": value}
    except Exception as error:
        result = {
            "status": "error",
            "durationMs": round((time.monotonic() - started) * 1000),
            "error": {"type": type(error).__name__, "message": str(error)[:2_000]},
        }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, separators=(",", ":"))


def self_test() -> None:
    assert _output_path({"format": "webp"}) == "/work/output.webp"
    assert FORMAT_MIME["jpeg"] == "image/jpeg"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    else:
        run()
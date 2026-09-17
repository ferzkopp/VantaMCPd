#!/usr/bin/env python3
"""Python Compute adapter for the shared artifact filesystem protocol."""
import os
from typing import Any

from artifact_protocol import NAME_PATTERN, available, copy_verified, marker, publish_reserved, release, reserve


def stage_inputs(root: str, inputs: list[dict[str, str]], destination: str, max_bytes: int) -> dict[str, str]:
    marker(root)
    os.makedirs(destination, mode=0o700)
    paths: dict[str, str] = {}
    total = 0
    for entry in inputs:
        artifact_id = entry["artifactId"]
        name = entry["name"]
        if NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("artifact input name is invalid")
        target = os.path.join(destination, name)
        metadata = copy_verified(root, artifact_id, target, max_bytes - total)
        total += int(metadata["bytes"])
        paths[name] = f"/inputs/{name}"
    return paths


def publish(root: str, reservation_id: str, producer: str, artifacts: list[dict[str, Any]], workspace: str, retention_days: int) -> list[dict[str, Any]]:
    workspace_root = os.path.realpath(workspace)
    items = []
    for artifact in artifacts:
        sandbox_path = str(artifact.get("path", ""))
        if not sandbox_path.startswith("/work/"):
            raise ValueError("artifact output path escaped the workspace")
        source = os.path.realpath(os.path.join(workspace_root, *sandbox_path[6:].split("/")))
        if os.path.commonpath((workspace_root, source)) != workspace_root or source == workspace_root:
            raise ValueError("artifact output path escaped the workspace")
        items.append({
            "source": source,
            "name": str(artifact.get("name", "artifact.bin"))[:128],
            "mimeType": str(artifact.get("mimeType", "application/octet-stream"))[:200],
        })
    return publish_reserved(root, reservation_id, producer, items, retention_days)


def self_test() -> None:
    workspace = os.path.realpath("/tmp/workspace")
    escaped = os.path.realpath(os.path.join(workspace, *"../outside".split("/")))
    assert os.path.commonpath((workspace, escaped)) != workspace


if __name__ == "__main__":
    self_test()

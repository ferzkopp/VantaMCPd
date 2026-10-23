#!/usr/bin/env python3
"""Image Render adapter for the shared artifact filesystem protocol."""
import os
from typing import Any

from artifact_protocol import NAME_PATTERN, available, copy_verified, marker, publish_reserved, release, reserve

import scene as scene_tools


class InputBudget:
    def __init__(self, max_bytes: int):
        self.remaining = max_bytes

    def take(self, metadata: dict[str, Any]) -> None:
        self.remaining -= int(metadata["bytes"])


def stage_named(root: str, entries: list[dict[str, str]], destination: str, budget: InputBudget) -> list[dict[str, str]]:
    """Copy explicitly named artifacts to destination/<name>."""
    marker(root)
    staged = []
    for entry in entries:
        if NAME_PATTERN.fullmatch(entry["name"]) is None:
            raise ValueError("artifact input name is invalid")
        metadata = copy_verified(root, entry["artifactId"], os.path.join(destination, entry["name"]), budget.remaining)
        budget.take(metadata)
        staged.append({"artifactId": entry["artifactId"], "name": entry["name"], "path": f"/inputs/{entry['name']}", "bytes": int(metadata["bytes"])})
    return staged


def stage_references(root: str, artifact_ids: list[str], destination: str, budget: InputBudget, taken: set[str]) -> list[dict[str, str]]:
    """Copy artifacts referenced inside the scene, naming each after its stored artifact name."""
    marker(root)
    staged = []
    for artifact_id in artifact_ids:
        temporary = os.path.join(destination, f".staging-{artifact_id}")
        metadata = copy_verified(root, artifact_id, temporary, budget.remaining)
        budget.take(metadata)
        name = scene_tools.staged_name(artifact_id, str(metadata.get("name", "")), taken)
        os.rename(temporary, os.path.join(destination, name))
        staged.append({"artifactId": artifact_id, "name": name, "path": f"/inputs/{name}", "bytes": int(metadata["bytes"])})
    return staged


def read_text(root: str, artifact_id: str, scratch: str, max_bytes: int) -> str:
    """Verify and read a text artifact such as a scene file."""
    marker(root)
    target = os.path.join(scratch, f".scene-{artifact_id}")
    copy_verified(root, artifact_id, target, max_bytes)
    try:
        with open(target, "rb") as handle:
            payload = handle.read()
    finally:
        os.chmod(target, 0o600)  # Windows refuses to delete read-only files.
        os.unlink(target)
    if b"\x00" in payload:
        raise ValueError(f"scene artifact {artifact_id} is not a text file")
    return scene_tools.decode_text(payload)


def publish(root: str, reservation_id: str, source: str, name: str, retention_days: int, mime_type: str = "image/png") -> dict[str, Any]:
    if NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("artifact output name is invalid")
    return publish_reserved(root, reservation_id, "image-render", [{"source": source, "name": name, "mimeType": mime_type}], retention_days)[0]


def self_test() -> None:
    assert NAME_PATTERN.fullmatch("render.png")
    assert not NAME_PATTERN.fullmatch("../render.png")
    budget = InputBudget(10)
    budget.take({"bytes": 4})
    assert budget.remaining == 6
    assert callable(available) and callable(reserve) and callable(release)


if __name__ == "__main__":
    self_test()

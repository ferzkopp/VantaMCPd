#!/usr/bin/env python3
"""Image Processing adapter for the shared artifact filesystem protocol."""
import os
from typing import Any

from artifact_protocol import NAME_PATTERN, available, copy_verified, marker, publish_reserved, release, reserve


def stage_inputs(root: str, inputs: list[dict[str, str]], destination: str, max_bytes: int) -> list[dict[str, str]]:
    marker(root)
    os.makedirs(destination, mode=0o700, exist_ok=True)
    staged = []
    total = 0
    for entry in inputs:
        artifact_id = entry["artifactId"]
        name = entry["name"]
        if NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("artifact input name is invalid")
        target = os.path.join(destination, name)
        metadata = copy_verified(root, artifact_id, target, max_bytes - total)
        total += int(metadata["bytes"])
        staged.append({"path": f"/inputs/{name}", "mimeType": str(metadata.get("mimeType", ""))[:200]})
    return staged


def publish(root: str, reservation_id: str, source: str, name: str, mime_type: str, retention_days: int) -> dict[str, Any]:
    if NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("artifact output name is invalid")
    return publish_reserved(root, reservation_id, "image-processing", [{
        "source": source,
        "name": name,
        "mimeType": mime_type,
    }], retention_days)[0]


def self_test() -> None:
    assert NAME_PATTERN.fullmatch("result.webp")
    assert not NAME_PATTERN.fullmatch("../result.webp")


if __name__ == "__main__":
    self_test()

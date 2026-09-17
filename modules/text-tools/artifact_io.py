#!/usr/bin/env python3
"""Text Tools adapter for the shared artifact filesystem protocol."""
import os
from typing import Any

from artifact_protocol import marker, publish_unreserved, verified_source


def root() -> str:
	value = os.environ.get("VANTA_ARTIFACT_ROOT")
	if not value:
		raise ValueError("shared artifact storage is not configured for text-tools")
	marker(value)
	return value


def publish_file(store_root: str, source: str, name: str, mime_type: str, budget_bytes: int, retention_days: int) -> dict[str, Any]:
	return publish_unreserved(store_root, "text-tools", [{
		"source": source,
		"name": name[:128],
		"mimeType": mime_type[:200],
	}], budget_bytes, retention_days)[0]

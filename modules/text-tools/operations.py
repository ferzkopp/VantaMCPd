#!/usr/bin/env python3
from typing import Any

from operation_common import category_schema, dispatch
from operations_data import (
    CODEC_OPERATIONS,
    DATA_OPERATIONS,
    GENERATE_OPERATIONS,
    SECURITY_OPERATIONS,
    TABLE_OPERATIONS,
)
from operations_developer import COMMAND_OPERATIONS, DEVELOPER_OPERATIONS, DOCUMENT_OPERATIONS
from operations_text import ANALYZE_OPERATIONS, EXTRACT_OPERATIONS, TRANSFORM_OPERATIONS

CATEGORIES = {
    "text_transform": ("Perform bounded deterministic text cleanup and transformation.", TRANSFORM_OPERATIONS),
    "text_extract": ("Extract common structured values from caller-provided text.", EXTRACT_OPERATIONS),
    "text_analyze": ("Calculate lightweight statistics, readability, similarity, and tokenization heuristics.", ANALYZE_OPERATIONS),
    "text_codec": ("Encode and decode common text representations.", CODEC_OPERATIONS),
    "data_convert": ("Parse and convert bounded JSON, CSV, TOML, INI, XML, and query-string data.", DATA_OPERATIONS),
    "text_security": ("Calculate digests and checksums or validate UUIDs.", SECURITY_OPERATIONS),
    "text_generate": ("Generate random identifiers, passwords, and placeholder text.", GENERATE_OPERATIONS),
    "developer_text": ("Perform bounded regex replacement, unified diff, and semantic-version operations.", DEVELOPER_OPERATIONS),
    "document_process": ("Process Markdown structure and JSON or TOML frontmatter.", DOCUMENT_OPERATIONS),
    "table_transform": ("Render and sort flat JSON tables.", TABLE_OPERATIONS),
    "command_text": ("Use constrained stdin-only wrappers around rg, jq, awk, and sed.", COMMAND_OPERATIONS),
}


def category_tools() -> dict[str, dict[str, Any]]:
    tools = {}
    for name, (description, operations) in CATEGORIES.items():
        tools[name] = {
            "description": description,
            "inputSchema": category_schema(operations),
            "handler": lambda arguments, category=name, registry=operations: dispatch(registry, category, arguments),
        }
    return tools

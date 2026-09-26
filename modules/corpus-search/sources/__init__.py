#!/usr/bin/env python3
"""Registry of corpus source adapters.

Each adapter owns everything specific to one upstream dataset: how its records are acquired and
parsed, which identifiers it issues, what subject scheme it uses, and the licence and terms recorded
as provenance. The provisioner, the query layer, and the server treat sources only through this
interface, so adding a dataset does not change the pipeline or the tools.
"""
from typing import Any

from sources.arxiv import ArxivSource
from sources.wikipedia import WikipediaSource

_ADAPTERS = [ArxivSource(), WikipediaSource()]
_BY_KEY = {adapter.key: adapter for adapter in _ADAPTERS}
_BY_ID = {adapter.id: adapter for adapter in _ADAPTERS}


def keys() -> list[str]:
    return sorted(_BY_KEY)


def identifiers() -> list[str]:
    return sorted(_BY_ID)


def by_key(key: str) -> Any:
    adapter = _BY_KEY.get(key)
    if adapter is None:
        raise ValueError(f"unknown corpus source '{key}'")
    return adapter


def by_id(identifier: str) -> Any:
    adapter = _BY_ID.get(identifier)
    if adapter is None:
        raise ValueError(f"unknown corpus source '{identifier}'")
    return adapter


def find(identifier: str) -> Any | None:
    return _BY_ID.get(identifier)


def resolve_identifier(value: str) -> tuple[Any, str]:
    """Map a caller-supplied record identifier or URL onto its owning adapter and canonical form.

    Prefixed adapters are tried first so that a bare arXiv identifier, which carries no prefix,
    remains the fallback and keeps working exactly as it did before other sources existed.
    """
    for adapter in sorted(_ADAPTERS, key=lambda item: 0 if item.prefix else 1):
        if adapter.accepts(value):
            return adapter, adapter.normalize(value)
    raise ValueError("id must be an arXiv identifier or a prefixed record identifier such as wikipedia:Title")


def describe_subject(source: str, subject: str) -> tuple[str | None, str | None]:
    adapter = _BY_ID.get(source)
    return adapter.describe(subject) if adapter is not None else (None, None)


def describe_any(subject: str) -> tuple[str | None, str | None]:
    """Resolve a subject identifier against whichever source owns a scheme containing it."""
    for adapter in _ADAPTERS:
        group, name = adapter.describe(subject)
        if name is not None:
            return group, name
    return None, None

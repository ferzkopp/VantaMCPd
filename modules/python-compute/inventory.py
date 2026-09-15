#!/usr/bin/env python3
"""Package inventory for Python Compute.

Produces the environment description an agent reads before writing code, and the apt plan the
installer uses to provision the selected bundle.
"""
import argparse
import importlib.metadata
import importlib.util
import json
import platform
import sys
from datetime import datetime, timezone
from typing import Any

BUNDLES = ("core", "science", "full")

# module, apt package, distribution name for version lookup, lowest bundle that provides it
GROUPS: list[dict[str, Any]] = [
    {
        "name": "numeric",
        "summary": "Arrays, linear algebra, optimization and signal processing",
        "members": [
            ("numpy", "python3-numpy", "numpy", "core"),
            ("scipy", "python3-scipy", "scipy", "science"),
        ],
    },
    {
        "name": "dataframe",
        "summary": "Tabular data, spreadsheets and table rendering",
        "members": [
            ("pandas", "python3-pandas", "pandas", "science"),
            ("tabulate", "python3-tabulate", "tabulate", "core"),
            ("openpyxl", "python3-openpyxl", "openpyxl", "full"),
        ],
    },
    {
        "name": "plotting",
        "summary": "Chart rendering to PNG and SVG with the non-interactive Agg backend",
        "members": [("matplotlib", "python3-matplotlib", "matplotlib", "science")],
    },
    {
        "name": "imaging",
        "summary": "Image loading, resizing, filtering and encoding",
        "members": [("PIL", "python3-pil", "Pillow", "science")],
    },
    {
        "name": "symbolic",
        "summary": "Symbolic algebra, calculus, equation solving and exact arithmetic",
        "members": [("sympy", "python3-sympy", "sympy", "science")],
    },
    {
        "name": "statistics",
        "summary": "Statistical models, hypothesis tests and classical machine learning",
        "members": [
            ("sklearn", "python3-sklearn", "scikit-learn", "full"),
            ("statsmodels", "python3-statsmodels", "statsmodels", "full"),
        ],
    },
    {
        "name": "graph",
        "summary": "Graph construction, traversal, centrality and shortest paths",
        "members": [("networkx", "python3-networkx", "networkx", "science")],
    },
    {
        "name": "geometry",
        "summary": "Planar geometry, GeoJSON shapes, buffers and spatial predicates",
        "members": [("shapely", "python3-shapely", "shapely", "full")],
    },
    {
        "name": "text",
        "summary": "Markup parsing, templating and advanced regular expressions",
        "members": [
            ("lxml", "python3-lxml", "lxml", "full"),
            ("bs4", "python3-bs4", "beautifulsoup4", "full"),
            ("jinja2", "python3-jinja2", "Jinja2", "full"),
            ("regex", "python3-regex", "regex", "full"),
        ],
    },
    {
        "name": "serialization",
        "summary": "Configuration and date formats beyond the standard library",
        "members": [
            ("yaml", "python3-yaml", "PyYAML", "core"),
            ("dateutil", "python3-dateutil", "python-dateutil", "core"),
        ],
    },
]

# Support packages with no importable module of their own.
EXTRA_APT = {"science": ["fonts-dejavu-core"]}

STANDARD_LIBRARY = [
    "argparse", "array", "ast", "base64", "binascii", "bisect", "calendar", "collections", "colorsys",
    "csv", "dataclasses", "datetime", "decimal", "difflib", "fractions", "functools", "gzip", "hashlib",
    "heapq", "hmac", "html", "http", "io", "ipaddress", "itertools", "json", "math", "operator",
    "pathlib", "pickle", "pprint", "random", "re", "secrets", "shutil", "sqlite3", "statistics",
    "string", "struct", "textwrap", "time", "tomllib", "typing", "unicodedata", "urllib", "uuid",
    "wave", "xml", "zipfile", "zlib", "zoneinfo",
]


def bundle_rank(bundle: str) -> int:
    return BUNDLES.index(bundle)


def apt_plan(bundle: str) -> list[tuple[str, list[str]]]:
    """Package installation phases for the selected bundle, ordered from cheapest to heaviest."""
    if bundle not in BUNDLES:
        raise ValueError(f"unknown bundle: {bundle}")
    selected = bundle_rank(bundle)
    phases = []
    for tier in BUNDLES[: selected + 1]:
        packages = [
            member[1]
            for group in GROUPS
            for member in group["members"]
            if member[3] == tier
        ]
        packages.extend(EXTRA_APT.get(tier, []))
        if packages:
            phases.append((tier, sorted(set(packages))))
    return phases


def module_version(module: str, distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        pass
    except Exception:
        return None
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    return "present" if spec is not None else None


def module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def describe(bundle: str) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    missing: list[str] = []
    for group in GROUPS:
        packages = []
        for module, apt_package, distribution, tier in group["members"]:
            if not module_available(module):
                if bundle_rank(tier) <= bundle_rank(bundle):
                    missing.append(module)
                continue
            packages.append({
                "module": module,
                "version": module_version(module, distribution),
                "aptPackage": apt_package,
            })
        if packages:
            groups.append({"name": group["name"], "summary": group["summary"], "packages": packages})
    groups.append({
        "name": "standard-library",
        "summary": "Python standard library modules that are always importable",
        "packages": [{"module": name, "version": platform.python_version()} for name in STANDARD_LIBRARY if module_available(name)],
    })
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bundle": bundle,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
            "machine": platform.machine(),
        },
        "groups": groups,
        "missing": sorted(set(missing)),
        "unavailable": "Installing packages from submitted code is not possible; the node provides only the listed modules.",
    }


def self_test() -> None:
    assert [tier for tier, _ in apt_plan("core")] == ["core"]
    assert [tier for tier, _ in apt_plan("full")] == ["core", "science", "full"]
    science = dict(apt_plan("science"))
    assert "python3-numpy" in science["core"]
    assert "python3-scipy" in science["science"]
    assert "fonts-dejavu-core" in science["science"]
    assert "python3-sklearn" not in science["core"] + science["science"]
    modules = [member[0] for group in GROUPS for member in group["members"]]
    assert len(modules) == len(set(modules)), "module names must be unique across groups"
    for group in GROUPS:
        for _module, apt_package, _distribution, tier in group["members"]:
            assert tier in BUNDLES
            assert apt_package.startswith("python3-")
    description = describe("core")
    assert description["python"]["version"] == platform.python_version()
    assert any(group["name"] == "standard-library" for group in description["groups"])
    assert "json" in [package["module"] for group in description["groups"] if group["name"] == "standard-library" for package in group["packages"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Python Compute package inventory")
    parser.add_argument("--bundle", default="science", choices=BUNDLES)
    parser.add_argument("--apt-plan", action="store_true", help="Print one 'tier package...' line per installation phase.")
    parser.add_argument("--write", help="Write the environment description to this path.")
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args()
    if arguments.self_test:
        self_test()
        return
    if arguments.apt_plan:
        for tier, packages in apt_plan(arguments.bundle):
            print(f"{tier} {' '.join(packages)}")
        return
    description = describe(arguments.bundle)
    text = json.dumps(description, indent=2, ensure_ascii=False) + "\n"
    if arguments.write:
        with open(arguments.write, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    sys.stdout.write(text)


if __name__ == "__main__":
    main()

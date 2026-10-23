#!/usr/bin/env python3
"""Tool schemas and argument validation for Image Render."""
import math
import re
from typing import Any

MAX_SCENE_CHARACTERS = 1_000_000
MAX_FILES = 16
MAX_FILE_TOTAL_CHARACTERS = 2_000_000
MAX_ASSETS = 32
MAX_ARTIFACT_REFERENCES = 32
MAX_ARTIFACT_INPUT_BYTES = 256 * 1024 * 1024
MAX_SCENE_ARTIFACT_BYTES = 2_000_000
MAX_DIMENSION = 8192
MAX_PIXELS = 16_777_216
MAX_INLINE_OUTPUT_BYTES = 2_000_000
MAX_DECLARES = 32
MIN_TIMEOUT_MS = 1_000
DEFAULT_TIMEOUT_MS = 300_000
CHECK_TIMEOUT_MS = 60_000
MAX_CHECK_TIMEOUT_MS = 120_000
MAX_QUERY_CHARACTERS = 200
MAX_READ_CHARACTERS = 40_000
DEFAULT_READ_CHARACTERS = 12_000

POLYHAVEN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$"
POLYHAVEN_RESOLUTIONS = ("1k", "2k")
MAX_POLYHAVEN_FILES = 16

ARTIFACT_ID_PATTERN = r"^[0-9a-f]{32}$"
NAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
SCENE_NAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,122})\.pov$"
OUTPUT_NAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,122})\.png$"
LIBRARY_PATH_PATTERN = r"^(?:examples|include|assets)/[A-Za-z0-9][A-Za-z0-9 _.+-]*(?:/[A-Za-z0-9][A-Za-z0-9 _.+-]*)*$"
EXAMPLE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.+-]*(?:/[A-Za-z0-9][A-Za-z0-9_.+-]*)*\.pov$"
DECLARE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,39}$"
DOC_SECTION_PATTERN = r"^[0-9]+(?:\.[0-9]+){0,6}$"
# Configuration and render-state names that POV-Ray reads or writes on its own.
FORBIDDEN_SUFFIXES = (".ini", ".conf", ".pov-state")
RESERVED_NAMES = ("render.png",)

_ARTIFACT_ID = re.compile(ARTIFACT_ID_PATTERN)
_NAME = re.compile(NAME_PATTERN)
_SCENE_NAME = re.compile(SCENE_NAME_PATTERN)
_OUTPUT_NAME = re.compile(OUTPUT_NAME_PATTERN)
_LIBRARY_PATH = re.compile(LIBRARY_PATH_PATTERN)
_EXAMPLE = re.compile(EXAMPLE_PATTERN)
_DECLARE = re.compile(DECLARE_PATTERN)
_DOC_SECTION = re.compile(DOC_SECTION_PATTERN)
_POLYHAVEN_ID = re.compile(POLYHAVEN_ID_PATTERN)


def _integer(minimum: int, maximum: int, default: int | None = None, description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "minimum": minimum, "maximum": maximum}
    if default is not None:
        schema["default"] = default
    if description:
        schema["description"] = description
    return schema


SCENE_SCHEMA = {
    "description": "The main scene: inline SDL text, a text artifact, or a packaged example path from render_library.",
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": MAX_SCENE_CHARACTERS},
                "name": {"type": "string", "pattern": SCENE_NAME_PATTERN, "default": "scene.pov"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "artifactId": {"type": "string", "pattern": ARTIFACT_ID_PATTERN},
                "name": {"type": "string", "pattern": SCENE_NAME_PATTERN, "default": "scene.pov"},
            },
            "required": ["artifactId"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"example": {"type": "string", "pattern": EXAMPLE_PATTERN, "description": "Path below examples/, e.g. advanced/teapot/teapot.pov."}},
            "required": ["example"],
            "additionalProperties": False,
        },
    ],
}

FILES_SCHEMA = {
    "type": "array",
    "maxItems": MAX_FILES,
    "description": "Extra inline text files such as .inc include files or .csv data, written beside the scene and reachable by bare name.",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": NAME_PATTERN},
            "text": {"type": "string", "maxLength": MAX_FILE_TOTAL_CHARACTERS},
        },
        "required": ["name", "text"],
        "additionalProperties": False,
    },
}

ASSETS_SCHEMA = {
    "type": "array",
    "maxItems": MAX_ASSETS,
    "description": "Shared artifacts staged read-only under /inputs/<name> and reachable by bare name, e.g. png \"photo.png\" or #include \"part.inc\".",
    "items": {
        "type": "object",
        "properties": {
            "artifactId": {"type": "string", "pattern": ARTIFACT_ID_PATTERN},
            "name": {"type": "string", "pattern": NAME_PATTERN},
        },
        "required": ["artifactId", "name"],
        "additionalProperties": False,
    },
}

DECLARES_SCHEMA = {
    "type": "object",
    "maxProperties": MAX_DECLARES,
    "propertyNames": {"pattern": DECLARE_PATTERN},
    "additionalProperties": {"type": "number"},
    "description": "Numeric identifiers declared before parsing, like the Declare=Name=Value INI option.",
}

OUTPUT_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "mode": {"const": "artifact"},
                "name": {"type": "string", "pattern": OUTPUT_NAME_PATTERN, "default": "render.png"},
                "retentionDays": _integer(1, 90, 7),
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"mode": {"const": "inline"}},
            "required": ["mode"],
            "additionalProperties": False,
            "description": f"Base64 PNG in the result; the PNG must not exceed {MAX_INLINE_OUTPUT_BYTES} bytes.",
        },
    ],
    "default": {"mode": "artifact"},
}

ANTIALIAS_SCHEMA = {
    "type": "object",
    "properties": {
        "enabled": {"type": "boolean", "default": True},
        "threshold": {"type": "number", "minimum": 0, "maximum": 3, "default": 0.3},
        "method": {"type": "integer", "enum": [1, 2], "default": 1, "description": "1 non-recursive, 2 adaptive recursive supersampling."},
        "depth": _integer(1, 9, 3),
    },
    "additionalProperties": False,
}

COMMON_SCENE_PROPERTIES = {
    "scene": SCENE_SCHEMA,
    "files": FILES_SCHEMA,
    "assets": ASSETS_SCHEMA,
    "declares": DECLARES_SCHEMA,
    "clock": {"type": "number", "description": "Value of the clock identifier, for rendering one animation frame."},
}

TOOLS: dict[str, dict[str, Any]] = {
    "render_environment": {
        "description": (
            "Report the POV-Ray version, render limits, include search paths, the local documentation and example "
            "library, installed CC0 asset packs, and the conventions for artifact references. Call this first."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "render_docs": {
        "description": (
            "Search or read the local POV-Ray 3.7 manual (reference, tutorial and user guide). search ranks sections "
            "for keywords such as 'image_map hdr' or 'area_light'; read returns one numbered section such as 3.4.2.1; "
            "toc lists section titles below an optional section number. Use it to verify syntax before rendering."
        ),
        "inputSchema": {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "search"},
                        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARACTERS},
                        "limit": _integer(1, 20, 8),
                    },
                    "required": ["operation", "query"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "read"},
                        "sectionId": {"type": "string", "pattern": DOC_SECTION_PATTERN},
                        "offset": _integer(0, 10_000_000, 0),
                        "maxCharacters": _integer(500, MAX_READ_CHARACTERS, DEFAULT_READ_CHARACTERS),
                    },
                    "required": ["operation", "sectionId"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "toc"},
                        "sectionId": {"type": "string", "pattern": DOC_SECTION_PATTERN},
                        "depth": _integer(1, 4, 1),
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "render_library": {
        "description": (
            "Search and read the packaged POV-Ray example scenes, standard include files (colors.inc, textures.inc, "
            "woods.inc, ...) and installed CC0 assets. declarations finds predefined colors, textures, finishes, "
            "objects and macros by name with the include file that defines them. Example paths can be rendered "
            "directly with render_scene scene.example."
        ),
        "inputSchema": {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "search"},
                        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARACTERS},
                        "kind": {"type": "string", "enum": ["example", "include", "asset"]},
                        "limit": _integer(1, 30, 10),
                    },
                    "required": ["operation", "query"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "read"},
                        "path": {"type": "string", "pattern": LIBRARY_PATH_PATTERN, "maxLength": 300},
                        "offset": _integer(0, 10_000_000, 0),
                        "maxCharacters": _integer(500, MAX_READ_CHARACTERS, DEFAULT_READ_CHARACTERS),
                    },
                    "required": ["operation", "path"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "declarations"},
                        "query": {"type": "string", "maxLength": MAX_QUERY_CHARACTERS},
                        "path": {"type": "string", "pattern": LIBRARY_PATH_PATTERN, "maxLength": 300},
                        "limit": _integer(1, 200, 40),
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "render_polyhaven": {
        "description": (
            "Use any of about 2,400 CC0 Poly Haven HDRIs, textures and models in a scene. search the installed index; "
            "download the result's filesUrl with browser-retrieval web_download, then plan with that artifactId to get the "
            "exact web_download calls for the 1k or 2k files; import verifies every file against Poly Haven's size and MD5, "
            "builds a POV-Ray include (models are converted to mesh2) and returns the assets[] entries to pass to "
            "render_scene. Results carry the required 'Powered by Poly Haven' credit; show it to the user."
        ),
        "inputSchema": {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "search"},
                        "query": {"type": "string", "maxLength": MAX_QUERY_CHARACTERS, "description": "Words matched against name, tags, category and description; omit to list the most downloaded."},
                        "kind": {"type": "string", "enum": ["hdri", "texture", "model"]},
                        "limit": _integer(1, 30, 10),
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "plan"},
                        "assetId": {"type": "string", "pattern": POLYHAVEN_ID_PATTERN},
                        "resolution": {"type": "string", "enum": list(POLYHAVEN_RESOLUTIONS), "default": "1k"},
                        "filesArtifactId": {"type": "string", "pattern": ARTIFACT_ID_PATTERN, "description": "Artifact of the asset's filesUrl JSON, from web_download."},
                    },
                    "required": ["operation", "assetId", "filesArtifactId"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "import"},
                        "assetId": {"type": "string", "pattern": POLYHAVEN_ID_PATTERN},
                        "resolution": {"type": "string", "enum": list(POLYHAVEN_RESOLUTIONS), "default": "1k"},
                        "filesArtifactId": {"type": "string", "pattern": ARTIFACT_ID_PATTERN},
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_POLYHAVEN_FILES,
                            "description": "The planned files as downloaded: name from plan and the web_download artifactId.",
                            "items": {
                                "type": "object",
                                "properties": {"name": {"type": "string", "pattern": NAME_PATTERN}, "artifactId": {"type": "string", "pattern": ARTIFACT_ID_PATTERN}},
                                "required": ["name", "artifactId"],
                                "additionalProperties": False,
                            },
                        },
                        "retentionDays": _integer(1, 90, 7),
                    },
                    "required": ["operation", "assetId", "filesArtifactId", "files"],
                    "additionalProperties": False,
                },
            ],
        },
    },
    "render_check": {
        "description": (
            "Parse a scene with a tiny low-quality render and return parse errors and warnings with file, line and the "
            "surrounding source lines, without producing an image. Resolves files, assets and artifact references "
            "exactly like render_scene. Use it to iterate on syntax cheaply before a full render."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **COMMON_SCENE_PROPERTIES,
                "timeoutMs": _integer(MIN_TIMEOUT_MS, MAX_CHECK_TIMEOUT_MS, CHECK_TIMEOUT_MS),
            },
            "required": ["scene"],
            "additionalProperties": False,
        },
    },
    "render_scene": {
        "description": (
            "Ray trace a POV-Ray 3.7 scene into a PNG inside a network-less sandbox with POV-Ray file I/O restricted to "
            "the scene workspace, staged inputs and the read-only library. Inline files and assets[] are reachable by "
            "bare name; a string literal \"artifact:<32-hex id>\" anywhere in the scene or files is replaced with the "
            "staged copy of that artifact. Output defaults to a PNG artifact; long renders can run in the background."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **COMMON_SCENE_PROPERTIES,
                "width": _integer(1, MAX_DIMENSION, 800),
                "height": _integer(1, MAX_DIMENSION, 600),
                "quality": _integer(0, 11, 9, "POV-Ray +Q render quality; 9 enables every feature."),
                "antialias": ANTIALIAS_SCHEMA,
                "alpha": {"type": "boolean", "default": False, "description": "Render a transparent background into the PNG alpha channel."},
                "bitDepth": {"type": "integer", "enum": [8, 16], "default": 8},
                "output": OUTPUT_SCHEMA,
                "timeoutMs": _integer(MIN_TIMEOUT_MS, 86_400_000, description="Wall-clock limit. Defaults to 5 minutes, or the node's background limit for background jobs."),
            },
            "required": ["scene"],
            "additionalProperties": False,
        },
    },
    "render_benchmark": {
        "description": (
            "Run the standard POV-Ray 3.7 benchmark scene (benchmark.pov, version 2.01) with the INI options documented in "
            "its header at the official 512x512, using every render thread of this node, and record the result as the installation's "
            "benchmark report shown by the dashboard and cluster_list_modules. Background-only: it takes minutes on x86 "
            "and hours on small ARM boards, and it occupies this node's render slot while it runs."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _only(value: dict[str, Any], field: str, allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"{field} has unsupported properties: {', '.join(unknown)}")


def _int(value: Any, field: str, minimum: int, maximum: int, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _number(value: Any, field: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError(f"{field} must be from {minimum} to {maximum}")
    return float(value)


def _bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _text(value: Any, field: str, maximum: int, minimum: int = 0) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} must be a string of {minimum} to {maximum} characters")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL characters")
    return value


def _pattern(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _file_name(value: Any, field: str) -> str:
    name = _pattern(value, field, _NAME)
    lowered = name.lower()
    if lowered.endswith(FORBIDDEN_SUFFIXES) or lowered in RESERVED_NAMES:
        raise ValueError(f"{field} must not be an INI, configuration, render-state or reserved output name")
    return name


def _scene(value: Any) -> dict[str, Any]:
    scene = _object(value, "scene")
    if "text" in scene:
        _only(scene, "scene", ("text", "name"))
        return {"text": _text(scene["text"], "scene.text", MAX_SCENE_CHARACTERS, 1), "name": _pattern(scene.get("name", "scene.pov"), "scene.name", _SCENE_NAME)}
    if "artifactId" in scene:
        _only(scene, "scene", ("artifactId", "name"))
        return {"artifactId": _pattern(scene["artifactId"], "scene.artifactId", _ARTIFACT_ID), "name": _pattern(scene.get("name", "scene.pov"), "scene.name", _SCENE_NAME)}
    if "example" in scene:
        _only(scene, "scene", ("example",))
        example = _pattern(scene["example"], "scene.example", _EXAMPLE)
        if any(part in ("..", ".") for part in example.split("/")):
            raise ValueError("scene.example is invalid")
        return {"example": example}
    raise ValueError("scene needs text, artifactId, or example")


def _scene_inputs(arguments: dict[str, Any]) -> dict[str, Any]:
    scene = _scene(arguments.get("scene"))
    files_value = arguments.get("files", [])
    if not isinstance(files_value, list) or len(files_value) > MAX_FILES:
        raise ValueError(f"files must be a list of at most {MAX_FILES} entries")
    files = []
    total = 0
    for index, entry in enumerate(files_value):
        entry = _object(entry, f"files[{index}]")
        _only(entry, f"files[{index}]", ("name", "text"))
        name = _file_name(entry.get("name"), f"files[{index}].name")
        text = _text(entry.get("text"), f"files[{index}].text", MAX_FILE_TOTAL_CHARACTERS)
        total += len(text)
        files.append({"name": name, "text": text})
    if total > MAX_FILE_TOTAL_CHARACTERS:
        raise ValueError(f"files may contain at most {MAX_FILE_TOTAL_CHARACTERS} characters in total")
    assets_value = arguments.get("assets", [])
    if not isinstance(assets_value, list) or len(assets_value) > MAX_ASSETS:
        raise ValueError(f"assets must be a list of at most {MAX_ASSETS} entries")
    assets = []
    for index, entry in enumerate(assets_value):
        entry = _object(entry, f"assets[{index}]")
        _only(entry, f"assets[{index}]", ("artifactId", "name"))
        assets.append({
            "artifactId": _pattern(entry.get("artifactId"), f"assets[{index}].artifactId", _ARTIFACT_ID),
            "name": _file_name(entry.get("name"), f"assets[{index}].name"),
        })
    names = [entry["name"] for entry in files] + [entry["name"] for entry in assets]
    if scene.get("name"):
        names.append(scene["name"])
    lowered = [name.lower() for name in names]
    if len(set(lowered)) != len(lowered):
        raise ValueError("scene, files and assets must use distinct names")
    declares_value = _object(arguments.get("declares", {}), "declares")
    if len(declares_value) > MAX_DECLARES:
        raise ValueError(f"declares may define at most {MAX_DECLARES} identifiers")
    declares = {_pattern(name, "declares name", _DECLARE): _number(value, f"declares.{name}") for name, value in declares_value.items()}
    clock = arguments.get("clock")
    return {
        "scene": scene,
        "files": files,
        "assets": assets,
        "declares": declares,
        "clock": None if clock is None else _number(clock, "clock", -1e9, 1e9),
    }


def validate_environment(arguments: dict[str, Any]) -> dict[str, Any]:
    _only(_object(arguments, "arguments"), "arguments", ())
    return {}


validate_benchmark = validate_environment


def validate_docs(arguments: dict[str, Any]) -> dict[str, Any]:
    arguments = _object(arguments, "arguments")
    operation = arguments.get("operation")
    if operation == "search":
        _only(arguments, "arguments", ("operation", "query", "limit"))
        return {"operation": operation, "query": _text(arguments.get("query"), "query", MAX_QUERY_CHARACTERS, 1), "limit": _int(arguments.get("limit"), "limit", 1, 20, 8)}
    if operation == "read":
        _only(arguments, "arguments", ("operation", "sectionId", "offset", "maxCharacters"))
        return {
            "operation": operation,
            "sectionId": _pattern(arguments.get("sectionId"), "sectionId", _DOC_SECTION),
            "offset": _int(arguments.get("offset"), "offset", 0, 10_000_000, 0),
            "maxCharacters": _int(arguments.get("maxCharacters"), "maxCharacters", 500, MAX_READ_CHARACTERS, DEFAULT_READ_CHARACTERS),
        }
    if operation == "toc":
        _only(arguments, "arguments", ("operation", "sectionId", "depth"))
        section = arguments.get("sectionId")
        return {
            "operation": operation,
            "sectionId": None if section is None else _pattern(section, "sectionId", _DOC_SECTION),
            "depth": _int(arguments.get("depth"), "depth", 1, 4, 1),
        }
    raise ValueError("operation must be search, read, or toc")


def _library_path(value: Any) -> str:
    path = _pattern(value, "path", _LIBRARY_PATH)
    if len(path) > 300 or any(part in ("..", ".") for part in path.split("/")):
        raise ValueError("path is invalid")
    return path


def validate_library(arguments: dict[str, Any]) -> dict[str, Any]:
    arguments = _object(arguments, "arguments")
    operation = arguments.get("operation")
    if operation == "search":
        _only(arguments, "arguments", ("operation", "query", "kind", "limit"))
        kind = arguments.get("kind")
        if kind is not None and kind not in ("example", "include", "asset"):
            raise ValueError("kind must be example, include, or asset")
        return {"operation": operation, "query": _text(arguments.get("query"), "query", MAX_QUERY_CHARACTERS, 1), "kind": kind, "limit": _int(arguments.get("limit"), "limit", 1, 30, 10)}
    if operation == "read":
        _only(arguments, "arguments", ("operation", "path", "offset", "maxCharacters"))
        return {
            "operation": operation,
            "path": _library_path(arguments.get("path")),
            "offset": _int(arguments.get("offset"), "offset", 0, 10_000_000, 0),
            "maxCharacters": _int(arguments.get("maxCharacters"), "maxCharacters", 500, MAX_READ_CHARACTERS, DEFAULT_READ_CHARACTERS),
        }
    if operation == "declarations":
        _only(arguments, "arguments", ("operation", "query", "path", "limit"))
        query = arguments.get("query")
        path = arguments.get("path")
        return {
            "operation": operation,
            "query": None if query is None else _text(query, "query", MAX_QUERY_CHARACTERS),
            "path": None if path is None else _library_path(path),
            "limit": _int(arguments.get("limit"), "limit", 1, 200, 40),
        }
    raise ValueError("operation must be search, read, or declarations")


def validate_polyhaven(arguments: dict[str, Any]) -> dict[str, Any]:
    arguments = _object(arguments, "arguments")
    operation = arguments.get("operation")
    if operation == "search":
        _only(arguments, "arguments", ("operation", "query", "kind", "limit"))
        query, kind = arguments.get("query"), arguments.get("kind")
        if kind is not None and kind not in ("hdri", "texture", "model"):
            raise ValueError("kind must be hdri, texture, or model")
        return {"operation": operation, "query": None if query is None else _text(query, "query", MAX_QUERY_CHARACTERS), "kind": kind, "limit": _int(arguments.get("limit"), "limit", 1, 30, 10)}
    if operation not in ("plan", "import"):
        raise ValueError("operation must be search, plan, or import")
    allowed = ("operation", "assetId", "resolution", "filesArtifactId") + (("files", "retentionDays") if operation == "import" else ())
    _only(arguments, "arguments", allowed)
    resolution = arguments.get("resolution", "1k")
    if resolution not in POLYHAVEN_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {', '.join(POLYHAVEN_RESOLUTIONS)}")
    request: dict[str, Any] = {
        "operation": operation,
        "assetId": _pattern(arguments.get("assetId"), "assetId", _POLYHAVEN_ID),
        "resolution": resolution,
        "filesArtifactId": _pattern(arguments.get("filesArtifactId"), "filesArtifactId", _ARTIFACT_ID),
    }
    if operation == "import":
        files_value = arguments.get("files")
        if not isinstance(files_value, list) or not 1 <= len(files_value) <= MAX_POLYHAVEN_FILES:
            raise ValueError(f"files must list 1 to {MAX_POLYHAVEN_FILES} downloaded files")
        files = []
        for index, entry in enumerate(files_value):
            entry = _object(entry, f"files[{index}]")
            _only(entry, f"files[{index}]", ("name", "artifactId"))
            files.append({"name": _file_name(entry.get("name"), f"files[{index}].name"), "artifactId": _pattern(entry.get("artifactId"), f"files[{index}].artifactId", _ARTIFACT_ID)})
        if len({entry["name"].lower() for entry in files}) != len(files):
            raise ValueError("files must use distinct names")
        request["files"] = files
        request["retentionDays"] = _int(arguments.get("retentionDays"), "retentionDays", 1, 90, 7)
    return request


def validate_check(arguments: dict[str, Any]) -> dict[str, Any]:
    arguments = _object(arguments, "arguments")
    _only(arguments, "arguments", (*COMMON_SCENE_PROPERTIES, "timeoutMs"))
    request = _scene_inputs(arguments)
    request.update({
        "mode": "check",
        "timeoutMs": _int(arguments.get("timeoutMs"), "timeoutMs", MIN_TIMEOUT_MS, MAX_CHECK_TIMEOUT_MS, CHECK_TIMEOUT_MS),
        "width": 32,
        "height": 24,
        "quality": 0,
        "antialias": {"enabled": False},
        "alpha": False,
        "bitDepth": 8,
        "output": None,
    })
    return request


def _antialias(value: Any) -> dict[str, Any]:
    value = _object({} if value is None else value, "antialias")
    _only(value, "antialias", ("enabled", "threshold", "method", "depth"))
    method = value.get("method", 1)
    if method not in (1, 2) or isinstance(method, bool):
        raise ValueError("antialias.method must be 1 or 2")
    return {
        "enabled": _bool(value.get("enabled"), "antialias.enabled", True),
        "threshold": _number(value.get("threshold", 0.3), "antialias.threshold", 0, 3),
        "method": method,
        "depth": _int(value.get("depth"), "antialias.depth", 1, 9, 3),
    }


def _output(value: Any) -> dict[str, Any]:
    value = _object({"mode": "artifact"} if value is None else value, "output")
    mode = value.get("mode")
    if mode == "inline":
        _only(value, "output", ("mode",))
        return {"mode": "inline"}
    if mode == "artifact":
        _only(value, "output", ("mode", "name", "retentionDays"))
        return {
            "mode": "artifact",
            "name": _pattern(value.get("name", "render.png"), "output.name", _OUTPUT_NAME),
            "retentionDays": _int(value.get("retentionDays"), "output.retentionDays", 1, 90, 7),
        }
    raise ValueError("output.mode must be artifact or inline")


def validate_render(arguments: dict[str, Any], limits: dict[str, int], background: bool = False) -> dict[str, Any]:
    arguments = _object(arguments, "arguments")
    _only(arguments, "arguments", (*COMMON_SCENE_PROPERTIES, "width", "height", "quality", "antialias", "alpha", "bitDepth", "output", "timeoutMs"))
    request = _scene_inputs(arguments)
    width = _int(arguments.get("width"), "width", 1, MAX_DIMENSION, 800)
    height = _int(arguments.get("height"), "height", 1, MAX_DIMENSION, 600)
    if width * height > MAX_PIXELS:
        raise ValueError(f"width x height must not exceed {MAX_PIXELS} pixels")
    bit_depth = arguments.get("bitDepth", 8)
    if bit_depth not in (8, 16) or isinstance(bit_depth, bool):
        raise ValueError("bitDepth must be 8 or 16")
    maximum = limits["maxBackgroundTimeoutMs"] if background else limits["maxTimeoutMs"]
    default = maximum if background else min(DEFAULT_TIMEOUT_MS, maximum)
    timeout = arguments.get("timeoutMs")
    if timeout is not None and not isinstance(timeout, bool) and isinstance(timeout, int) and timeout > maximum:
        hint = "" if background else "; submit it with execution: \"background\" for a longer limit"
        raise ValueError(f"timeoutMs exceeds this node's {'background' if background else 'immediate'} limit of {maximum} ms{hint}")
    request.update({
        "mode": "render",
        "width": width,
        "height": height,
        "quality": _int(arguments.get("quality"), "quality", 0, 11, 9),
        "antialias": _antialias(arguments.get("antialias")),
        "alpha": _bool(arguments.get("alpha"), "alpha", False),
        "bitDepth": bit_depth,
        "output": _output(arguments.get("output")),
        "timeoutMs": _int(timeout, "timeoutMs", MIN_TIMEOUT_MS, maximum, default),
    })
    return request


def estimated_png_bytes(width: int, height: int, bit_depth: int, alpha: bool) -> int:
    """Upper bound for an unfiltered, incompressible PNG of these dimensions."""
    channels = 4 if alpha else 3
    raw = height * (1 + width * channels * bit_depth // 8)
    return raw + raw // 100 + 64 * 1024


def image_buffer_bytes(width: int, height: int) -> int:
    """POV-Ray 3.7 spills its 5-float RGBFT image buffer to a /tmp file above 128 MB (Max_Image_Buffer_Memory)."""
    return width * height * 5 * 4


def self_test() -> None:
    limits = {"maxTimeoutMs": 600_000, "maxBackgroundTimeoutMs": 14_400_000}
    request = validate_render({"scene": {"text": "sphere { 0, 1 }"}}, limits)
    assert request["width"] == 800 and request["output"] == {"mode": "artifact", "name": "render.png", "retentionDays": 7}
    assert request["timeoutMs"] == DEFAULT_TIMEOUT_MS
    assert validate_render({"scene": {"text": "x"}}, limits, background=True)["timeoutMs"] == 14_400_000
    for bad in (
        {"scene": {"text": "x"}, "files": [{"name": "povray.ini", "text": ""}]},
        {"scene": {"text": "x"}, "files": [{"name": "../escape.inc", "text": ""}]},
        {"scene": {"text": "x"}, "assets": [{"artifactId": "a" * 32, "name": "render.png"}]},
        {"scene": {"text": "x"}, "width": 8192, "height": 8192},
        {"scene": {"example": "../etc/passwd.pov"}},
        {"scene": {"text": "x"}, "declares": {"1bad": 1}},
        {"scene": {"text": "x"}, "timeoutMs": 700_000},
        {"scene": {"text": "x"}, "files": [{"name": "a.inc", "text": ""}, {"name": "A.inc", "text": ""}]},
        {"scene": {"text": "x"}, "shell": "rm"},
    ):
        try:
            validate_render(bad, limits)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid render arguments: {bad}")
    check = validate_check({"scene": {"example": "advanced/teapot/teapot.pov"}})
    assert check["mode"] == "check" and check["output"] is None
    assert validate_docs({"operation": "toc"})["sectionId"] is None
    assert validate_library({"operation": "read", "path": "include/colors.inc"})["path"] == "include/colors.inc"
    for bad_path in ("include/../x.inc", "/etc/passwd", "other/x.inc"):
        try:
            validate_library({"operation": "read", "path": bad_path})
        except ValueError:
            continue
        raise AssertionError(f"accepted library path {bad_path}")
    assert estimated_png_bytes(10, 10, 8, False) > 300
    assert image_buffer_bytes(3840, 2160) > 128_000_000


if __name__ == "__main__":
    self_test()

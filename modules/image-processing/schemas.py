#!/usr/bin/env python3
import base64
import binascii
import json
import re
from typing import Any

MAX_INLINE_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 16 * 1024 * 1024
MAX_SOURCES = 8
MAX_EDITS = 8
MAX_METADATA_ITEMS = 128
ARTIFACT_ID_PATTERN = r"^[0-9a-f]{32}$"
OUTPUT_NAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
FORMATS = ("png", "jpeg", "webp", "gif")
MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def _integer(minimum: int, maximum: int, default: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "minimum": minimum, "maximum": maximum}
    if default is not None:
        schema["default"] = default
    return schema


SOURCE_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"artifactId": {"type": "string", "pattern": ARTIFACT_ID_PATTERN}},
            "required": ["artifactId"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "contentEncoding": "base64",
                    "description": f"Base64 image data decoding to at most {MAX_INLINE_BYTES} bytes.",
                },
                "mimeType": {"type": "string", "enum": list(MIME_TYPES)},
            },
            "required": ["data", "mimeType"],
            "additionalProperties": False,
        },
    ]
}

ENCODING_PROPERTIES = {
    "format": {"type": "string", "enum": list(FORMATS), "default": "png"},
    "quality": _integer(1, 100, 85),
    "stripMetadata": {"type": "boolean", "default": True},
}

OUTPUT_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"mode": {"const": "inline"}, **ENCODING_PROPERTIES},
            "required": ["mode"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "mode": {"const": "artifact"},
                "name": {"type": "string", "pattern": OUTPUT_NAME_PATTERN},
                "retentionDays": _integer(1, 90, 7),
                **ENCODING_PROPERTIES,
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    ]
}

EDIT_SCHEMAS = [
    {
        "type": "object",
        "properties": {"operation": {"const": "auto_orient"}},
        "required": ["operation"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "operation": {"const": "resize"},
            "width": _integer(1, MAX_DIMENSION),
            "height": _integer(1, MAX_DIMENSION),
            "mode": {"type": "string", "enum": ["fit", "fill", "stretch"], "default": "fit"},
            "background": {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$", "default": "#00000000"},
        },
        "required": ["operation", "width", "height"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "operation": {"const": "crop"},
            "x": _integer(0, MAX_DIMENSION - 1),
            "y": _integer(0, MAX_DIMENSION - 1),
            "width": _integer(1, MAX_DIMENSION),
            "height": _integer(1, MAX_DIMENSION),
        },
        "required": ["operation", "x", "y", "width", "height"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "operation": {"const": "rotate"},
            "degrees": {"type": "number", "minimum": -360, "maximum": 360},
            "background": {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$", "default": "#00000000"},
        },
        "required": ["operation", "degrees"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "flip"}, "axis": {"type": "string", "enum": ["horizontal", "vertical"]}},
        "required": ["operation", "axis"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "trim"}, "fuzzPercent": {"type": "number", "minimum": 0, "maximum": 20, "default": 0}},
        "required": ["operation"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "operation": {"const": "pad"},
            "width": _integer(1, MAX_DIMENSION),
            "height": _integer(1, MAX_DIMENSION),
            "background": {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$", "default": "#00000000"},
            "gravity": {"type": "string", "enum": ["center", "north", "south", "east", "west"], "default": "center"},
        },
        "required": ["operation", "width", "height"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "grayscale"}},
        "required": ["operation"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "invert"}},
        "required": ["operation"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "gamma"}, "gamma": {"type": "number", "minimum": 0.1, "maximum": 10}},
        "required": ["operation", "gamma"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "auto_contrast"}, "cutoffPercent": {"type": "number", "minimum": 0, "maximum": 20, "default": 0}},
        "required": ["operation"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "equalize"}},
        "required": ["operation"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "threshold"}, "thresholdPercent": {"type": "number", "minimum": 0, "maximum": 100}},
        "required": ["operation", "thresholdPercent"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "posterize"}, "levels": _integer(2, 256)},
        "required": ["operation", "levels"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "solarize"}, "thresholdPercent": {"type": "number", "minimum": 0, "maximum": 100}},
        "required": ["operation", "thresholdPercent"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "hue"}, "degrees": {"type": "number", "minimum": -180, "maximum": 180}},
        "required": ["operation", "degrees"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "operation": {"const": "colorize"},
            "color": {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}$"},
            "amount": {"type": "number", "minimum": 0, "maximum": 100},
        },
        "required": ["operation", "color", "amount"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "opacity"}, "amount": {"type": "number", "minimum": 0, "maximum": 100}},
        "required": ["operation", "amount"],
        "additionalProperties": False,
    },
    *[
        {
            "type": "object",
            "properties": {"operation": {"const": name}, "amount": {"type": "number", "minimum": -100, "maximum": 100}},
            "required": ["operation", "amount"],
            "additionalProperties": False,
        }
        for name in ("brightness", "contrast", "saturation")
    ],
    {
        "type": "object",
        "properties": {"operation": {"const": "blur"}, "radius": {"type": "number", "minimum": 0.1, "maximum": 20}},
        "required": ["operation", "radius"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"operation": {"const": "sharpen"}, "amount": {"type": "number", "minimum": 0.1, "maximum": 5}},
        "required": ["operation", "amount"],
        "additionalProperties": False,
    },
]

TOOLS: dict[str, dict[str, Any]] = {
    "image_environment": {
        "description": "Report the installed image engines, supported safe raster formats, shared-artifact availability, operations, and effective resource limits before choosing a workflow.",
        "inputSchema": {
            "type": "object",
            "properties": {"refresh": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
    },
    "image_inspect": {
        "description": "Inspect one PNG, JPEG, WebP, or GIF from inline base64 or shared artifact storage and return bounded dimensions, format, orientation, sanitized metadata, histogram, and dominant-color information.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": SOURCE_SCHEMA,
                "includeMetadata": {"type": "boolean", "default": True},
                "histogramBins": _integer(0, 64, 0),
                "dominantColors": _integer(0, 16, 0),
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    },
    "image_edit": {
        "description": "Apply up to eight ordered, bounded edits to one safe raster image and encode it once as inline base64 or an immutable shared artifact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": SOURCE_SCHEMA,
                "edits": {"type": "array", "minItems": 1, "maxItems": MAX_EDITS, "items": {"oneOf": EDIT_SCHEMAS}},
                "output": OUTPUT_SCHEMA,
            },
            "required": ["source", "edits", "output"],
            "additionalProperties": False,
        },
    },
    "image_compose": {
        "description": "Overlay an image, add a bounded text watermark, or arrange up to eight safe raster images as a montage, returning inline base64 or a shared artifact.",
        "inputSchema": {
            "type": "object",
            "oneOf": [
                {
                    "properties": {
                        "operation": {"const": "overlay"},
                        "source": SOURCE_SCHEMA,
                        "overlay": SOURCE_SCHEMA,
                        "position": {"type": "string", "enum": ["center", "northwest", "northeast", "southwest", "southeast"], "default": "southeast"},
                        "opacity": _integer(1, 100, 100),
                        "output": OUTPUT_SCHEMA,
                    },
                    "required": ["operation", "source", "overlay", "output"],
                    "additionalProperties": False,
                },
                {
                    "properties": {
                        "operation": {"const": "watermark"},
                        "source": SOURCE_SCHEMA,
                        "text": {"type": "string", "minLength": 1, "maxLength": 256},
                        "position": {"type": "string", "enum": ["center", "northwest", "northeast", "southwest", "southeast"], "default": "southeast"},
                        "fontSize": _integer(8, 256, 32),
                        "color": {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$", "default": "#FFFFFFFF"},
                        "output": OUTPUT_SCHEMA,
                    },
                    "required": ["operation", "source", "text", "output"],
                    "additionalProperties": False,
                },
                {
                    "properties": {
                        "operation": {"const": "montage"},
                        "sources": {"type": "array", "minItems": 1, "maxItems": MAX_SOURCES, "items": SOURCE_SCHEMA},
                        "columns": _integer(1, MAX_SOURCES, 2),
                        "cellWidth": _integer(1, 2048, 512),
                        "cellHeight": _integer(1, 2048, 512),
                        "background": {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$", "default": "#000000FF"},
                        "output": OUTPUT_SCHEMA,
                    },
                    "required": ["operation", "sources", "output"],
                    "additionalProperties": False,
                },
            ],
        },
    },
    "image_compare": {
        "description": "Compare two safe raster images with bounded normalized pixel metrics and optionally return a visual difference image inline or through shared artifact storage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": SOURCE_SCHEMA,
                "reference": SOURCE_SCHEMA,
                "normalize": {"type": "string", "enum": ["none", "fit"], "default": "none"},
                "diffOutput": {"anyOf": [OUTPUT_SCHEMA, {"type": "null"}]},
            },
            "required": ["source", "reference"],
            "additionalProperties": False,
        },
    },
}


def _expect_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown fields for {name}: {', '.join(unknown)}")


def validate_source(value: Any, name: str = "source") -> dict[str, Any]:
    source = _expect_object(value, name)
    if set(source) == {"artifactId"}:
        artifact_id = source["artifactId"]
        if not isinstance(artifact_id, str) or re.fullmatch(ARTIFACT_ID_PATTERN, artifact_id) is None:
            raise ValueError(f"{name}.artifactId is invalid")
        return {"artifactId": artifact_id}
    _reject_unknown(source, {"data", "mimeType"}, name)
    data = source.get("data")
    mime_type = source.get("mimeType")
    if not isinstance(data, str) or mime_type not in MIME_TYPES:
        raise ValueError(f"{name} must contain base64 data and a supported mimeType")
    try:
        payload = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"{name}.data must be valid base64") from error
    if len(payload) > MAX_INLINE_BYTES:
        raise ValueError(f"{name}.data exceeds {MAX_INLINE_BYTES} decoded bytes")
    return {"data": data, "mimeType": mime_type}


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int, default: int) -> int:
    value = default if value is None else value
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _bounded_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be a number from {minimum} to {maximum}")
    return float(value)


def validate_output(value: Any, name: str = "output") -> dict[str, Any]:
    output = _expect_object(value, name)
    mode = output.get("mode")
    allowed = {"mode", "format", "quality", "stripMetadata"}
    if mode == "artifact":
        allowed |= {"name", "retentionDays"}
    elif mode != "inline":
        raise ValueError(f"{name}.mode must be inline or artifact")
    _reject_unknown(output, allowed, name)
    image_format = output.get("format", "png")
    if image_format not in FORMATS:
        raise ValueError(f"{name}.format must be one of: {', '.join(FORMATS)}")
    normalized = {
        "mode": mode,
        "format": image_format,
        "quality": _bounded_integer(output.get("quality"), f"{name}.quality", 1, 100, 85),
        "stripMetadata": output.get("stripMetadata", True),
    }
    if not isinstance(normalized["stripMetadata"], bool):
        raise ValueError(f"{name}.stripMetadata must be a boolean")
    if mode == "artifact":
        artifact_name = output.get("name", f"image.{image_format}")
        if not isinstance(artifact_name, str) or re.fullmatch(OUTPUT_NAME_PATTERN, artifact_name) is None:
            raise ValueError(f"{name}.name is invalid")
        normalized["name"] = artifact_name
        normalized["retentionDays"] = _bounded_integer(output.get("retentionDays"), f"{name}.retentionDays", 1, 90, 7)
    return normalized


def validate_edit(value: Any, index: int) -> dict[str, Any]:
    edit = _expect_object(value, f"edits[{index}]")
    operation = edit.get("operation")
    allowed: dict[str, set[str]] = {
        "auto_orient": {"operation"},
        "resize": {"operation", "width", "height", "mode", "background"},
        "crop": {"operation", "x", "y", "width", "height"},
        "rotate": {"operation", "degrees", "background"},
        "flip": {"operation", "axis"},
        "trim": {"operation", "fuzzPercent"},
        "pad": {"operation", "width", "height", "background", "gravity"},
        "grayscale": {"operation"},
        "invert": {"operation"},
        "gamma": {"operation", "gamma"},
        "auto_contrast": {"operation", "cutoffPercent"},
        "equalize": {"operation"},
        "threshold": {"operation", "thresholdPercent"},
        "posterize": {"operation", "levels"},
        "solarize": {"operation", "thresholdPercent"},
        "hue": {"operation", "degrees"},
        "colorize": {"operation", "color", "amount"},
        "opacity": {"operation", "amount"},
        "brightness": {"operation", "amount"},
        "contrast": {"operation", "amount"},
        "saturation": {"operation", "amount"},
        "blur": {"operation", "radius"},
        "sharpen": {"operation", "amount"},
    }
    if operation not in allowed:
        raise ValueError(f"unknown image edit operation: {operation}")
    _reject_unknown(edit, allowed[operation], f"edits[{index}]")
    required = {
        "resize": ("width", "height"),
        "crop": ("x", "y", "width", "height"),
        "rotate": ("degrees",),
        "flip": ("axis",),
        "pad": ("width", "height"),
        "gamma": ("gamma",),
        "threshold": ("thresholdPercent",),
        "posterize": ("levels",),
        "solarize": ("thresholdPercent",),
        "hue": ("degrees",),
        "colorize": ("color", "amount"),
        "opacity": ("amount",),
        "brightness": ("amount",),
        "contrast": ("amount",),
        "saturation": ("amount",),
        "blur": ("radius",),
        "sharpen": ("amount",),
    }.get(operation, ())
    missing = [field for field in required if field not in edit]
    if missing:
        raise ValueError(f"edits[{index}] is missing: {', '.join(missing)}")
    if operation == "gamma":
        _bounded_number(edit["gamma"], f"edits[{index}].gamma", 0.1, 10)
    elif operation == "auto_contrast" and "cutoffPercent" in edit:
        _bounded_number(edit["cutoffPercent"], f"edits[{index}].cutoffPercent", 0, 20)
    elif operation in ("threshold", "solarize"):
        _bounded_number(edit["thresholdPercent"], f"edits[{index}].thresholdPercent", 0, 100)
    elif operation == "posterize":
        _bounded_integer(edit["levels"], f"edits[{index}].levels", 2, 256, 2)
    elif operation == "hue":
        _bounded_number(edit["degrees"], f"edits[{index}].degrees", -180, 180)
    elif operation in ("colorize", "opacity"):
        _bounded_number(edit["amount"], f"edits[{index}].amount", 0, 100)
    if operation == "colorize" and (not isinstance(edit["color"], str) or re.fullmatch(r"#[0-9A-Fa-f]{6}", edit["color"]) is None):
        raise ValueError(f"edits[{index}].color must be a six-digit hex color")
    return dict(edit)


def validate(tool: str, arguments: Any) -> dict[str, Any]:
    request = _expect_object(arguments, "arguments")
    if tool == "image_environment":
        _reject_unknown(request, {"refresh"}, tool)
        refresh = request.get("refresh", False)
        if not isinstance(refresh, bool):
            raise ValueError("refresh must be a boolean")
        return {"refresh": refresh}
    if tool == "image_inspect":
        _reject_unknown(request, {"source", "includeMetadata", "histogramBins", "dominantColors"}, tool)
        if "source" not in request:
            raise ValueError("source is required")
        include_metadata = request.get("includeMetadata", True)
        if not isinstance(include_metadata, bool):
            raise ValueError("includeMetadata must be a boolean")
        return {
            "source": validate_source(request["source"]),
            "includeMetadata": include_metadata,
            "histogramBins": _bounded_integer(request.get("histogramBins"), "histogramBins", 0, 64, 0),
            "dominantColors": _bounded_integer(request.get("dominantColors"), "dominantColors", 0, 16, 0),
        }
    if tool == "image_edit":
        _reject_unknown(request, {"source", "edits", "output"}, tool)
        edits = request.get("edits")
        if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
            raise ValueError(f"edits must contain 1 to {MAX_EDITS} entries")
        return {
            "source": validate_source(request.get("source")),
            "edits": [validate_edit(edit, index) for index, edit in enumerate(edits)],
            "output": validate_output(request.get("output")),
        }
    if tool == "image_compose":
        operation = request.get("operation")
        common = {"operation", "output"}
        if operation == "overlay":
            _reject_unknown(request, common | {"source", "overlay", "position", "opacity"}, tool)
            return {
                "operation": operation,
                "source": validate_source(request.get("source")),
                "overlay": validate_source(request.get("overlay"), "overlay"),
                "position": _choice(request.get("position", "southeast"), "position", {"center", "northwest", "northeast", "southwest", "southeast"}),
                "opacity": _bounded_integer(request.get("opacity"), "opacity", 1, 100, 100),
                "output": validate_output(request.get("output")),
            }
        if operation == "watermark":
            _reject_unknown(request, common | {"source", "text", "position", "fontSize", "color"}, tool)
            text = request.get("text")
            if not isinstance(text, str) or not 1 <= len(text) <= 256:
                raise ValueError("text must contain 1 to 256 characters")
            return {
                "operation": operation,
                "source": validate_source(request.get("source")),
                "text": text,
                "position": _choice(request.get("position", "southeast"), "position", {"center", "northwest", "northeast", "southwest", "southeast"}),
                "fontSize": _bounded_integer(request.get("fontSize"), "fontSize", 8, 256, 32),
                "color": _color(request.get("color", "#FFFFFFFF"), "color"),
                "output": validate_output(request.get("output")),
            }
        if operation == "montage":
            _reject_unknown(request, common | {"sources", "columns", "cellWidth", "cellHeight", "background"}, tool)
            sources = request.get("sources")
            if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
                raise ValueError(f"sources must contain 1 to {MAX_SOURCES} entries")
            return {
                "operation": operation,
                "sources": [validate_source(source, f"sources[{index}]") for index, source in enumerate(sources)],
                "columns": _bounded_integer(request.get("columns"), "columns", 1, MAX_SOURCES, 2),
                "cellWidth": _bounded_integer(request.get("cellWidth"), "cellWidth", 1, 2048, 512),
                "cellHeight": _bounded_integer(request.get("cellHeight"), "cellHeight", 1, 2048, 512),
                "background": _color(request.get("background", "#000000FF"), "background"),
                "output": validate_output(request.get("output")),
            }
        raise ValueError("image_compose.operation must be overlay, watermark, or montage")
    if tool == "image_compare":
        _reject_unknown(request, {"source", "reference", "normalize", "diffOutput"}, tool)
        diff_output = request.get("diffOutput")
        return {
            "source": validate_source(request.get("source")),
            "reference": validate_source(request.get("reference"), "reference"),
            "normalize": _choice(request.get("normalize", "none"), "normalize", {"none", "fit"}),
            "diffOutput": None if diff_output is None else validate_output(diff_output, "diffOutput"),
        }
    raise ValueError(f"unknown image-processing tool: {tool}")


def _choice(value: Any, name: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return value


def _color(value: Any, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", value) is None:
        raise ValueError(f"{name} must be #RRGGBB or #RRGGBBAA")
    return value


def self_test() -> None:
    assert list(TOOLS) == ["image_environment", "image_inspect", "image_edit", "image_compose", "image_compare"]
    payload = base64.b64encode(b"tiny").decode("ascii")
    source = {"data": payload, "mimeType": "image/png"}
    assert validate("image_environment", {}) == {"refresh": False}
    assert validate("image_inspect", {"source": source})["source"] == source
    edited = validate("image_edit", {
        "source": source,
        "edits": [{"operation": "resize", "width": 32, "height": 32}],
        "output": {"mode": "inline", "format": "webp"},
    })
    assert edited["output"]["quality"] == 85 and edited["edits"][0]["operation"] == "resize"
    compared = validate("image_compare", {"source": source, "reference": source})
    assert compared["normalize"] == "none" and compared["diffOutput"] is None
    try:
        validate("image_inspect", {"source": source, "path": "/etc/passwd"})
    except ValueError as error:
        assert "unknown fields" in str(error)
    else:
        raise AssertionError("an undeclared field was accepted")
    try:
        validate_source({"data": base64.b64encode(b"x" * (MAX_INLINE_BYTES + 1)).decode("ascii"), "mimeType": "image/png"})
    except ValueError as error:
        assert "decoded bytes" in str(error)
    else:
        raise AssertionError("an oversized inline image was accepted")
    assert len(json.dumps(TOOLS)) > 10_000


if __name__ == "__main__":
    self_test()
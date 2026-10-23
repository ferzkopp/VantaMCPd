#!/usr/bin/env python3
"""Convert a pinned glTF 2.0 model into a POV-Ray mesh2 include. Standard library only; runs at installation.

Positions are baked into world space, mirrored from glTF's right-handed axes to POV-Ray's left-handed ones
(z negated, so the model's front faces the default -z camera), and shifted so the model stands on y=0,
centred on x and z. Each primitive becomes one uv-mapped mesh2 textured with its base colour map; normal,
metallic-roughness and occlusion maps are not used because POV-Ray 3.7 cannot apply them to a mesh.
"""
import json
import math
import os
import re
import struct
from typing import Any, Callable

MAX_TRIANGLES = 60_000
COMPONENTS = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
NORMALIZED_MAX = {"b": 127.0, "B": 255.0, "h": 32767.0, "H": 65535.0}
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
IMAGE_TYPES = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png"}
IDENTITY = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


def _multiply(a: list[float], b: list[float]) -> list[float]:
    """Column-major 4x4 product a*b, as glTF stores matrices."""
    return [sum(a[k * 4 + row] * b[column * 4 + k] for k in range(4)) for column in range(4) for row in range(4)]


def _local_matrix(node: dict[str, Any]) -> list[float]:
    if "matrix" in node:
        return [float(value) for value in node["matrix"]]
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    rotation = [
        1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w),
        2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w),
        2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y),
    ]
    return [
        rotation[0] * sx, rotation[1] * sx, rotation[2] * sx, 0.0,
        rotation[3] * sy, rotation[4] * sy, rotation[5] * sy, 0.0,
        rotation[6] * sz, rotation[7] * sz, rotation[8] * sz, 0.0,
        float(tx), float(ty), float(tz), 1.0,
    ]


def _normal_matrix(m: list[float]) -> list[float]:
    """Inverse transpose of the upper 3x3, column-major, for transforming normals."""
    a, b, c = m[0], m[4], m[8]
    d, e, f = m[1], m[5], m[9]
    g, h, i = m[2], m[6], m[10]
    cofactors = [e * i - f * h, -(d * i - f * g), d * h - e * g,
                 -(b * i - c * h), a * i - c * g, -(a * h - b * g),
                 b * f - c * e, -(a * f - c * d), a * e - b * d]
    determinant = a * cofactors[0] + b * cofactors[1] + c * cofactors[2]
    if abs(determinant) < 1e-20:
        raise ValueError("a node transform is singular")
    # The cofactor matrix divided by the determinant is the inverse transpose; return it column-major.
    return [cofactors[0] / determinant, cofactors[1] / determinant, cofactors[2] / determinant,
            cofactors[3] / determinant, cofactors[4] / determinant, cofactors[5] / determinant,
            cofactors[6] / determinant, cofactors[7] / determinant, cofactors[8] / determinant]


class Model:
    def __init__(self, path: str, resolve: Callable[[str], str]):
        with open(path, "r", encoding="utf-8") as handle:
            self.gltf = json.load(handle)
        if not str(self.gltf.get("asset", {}).get("version", "")).startswith("2"):
            raise ValueError("only glTF 2.0 is supported")
        self.resolve = resolve
        self.buffers: list[bytes] = []
        for buffer in self.gltf.get("buffers", []):
            uri = buffer.get("uri", "")
            if not uri or uri.startswith("data:") or ".." in uri.replace("\\", "/").split("/"):
                raise ValueError("buffers must be external files")
            with open(resolve(uri), "rb") as handle:
                payload = handle.read()
            if len(payload) < int(buffer["byteLength"]):
                raise ValueError(f"buffer {uri} is shorter than declared")
            self.buffers.append(payload)

    def accessor(self, index: int) -> list[tuple[float, ...]]:
        accessor = self.gltf["accessors"][index]
        view = self.gltf["bufferViews"][accessor["bufferView"]]
        code = COMPONENTS[accessor["componentType"]]
        width = WIDTHS[accessor["type"]]
        element = struct.calcsize("<" + code * width)
        stride = int(view.get("byteStride") or element)
        start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
        count = int(accessor["count"])
        data = self.buffers[view["buffer"]]
        if start + stride * (count - 1) + element > len(data):
            raise ValueError(f"accessor {index} reads past its buffer")
        unpack = struct.Struct("<" + code * width).unpack_from
        values = [unpack(data, start + stride * item) for item in range(count)]
        if accessor.get("normalized") and code in NORMALIZED_MAX:
            scale = NORMALIZED_MAX[code]
            values = [tuple(max(component / scale, -1.0) for component in value) for value in values]
        return values

    def image(self, texture_index: int | None) -> str | None:
        if texture_index is None:
            return None
        source = self.gltf["textures"][texture_index].get("source")
        uri = self.gltf["images"][source].get("uri", "")
        name = uri.replace("\\", "/").rsplit("/", 1)[-1]
        if not name or uri.startswith("data:") or os.path.splitext(name)[1].lower() not in IMAGE_TYPES:
            raise ValueError(f"unsupported image {uri[:80]}")
        return name

    def instances(self) -> list[tuple[dict[str, Any], list[float]]]:
        nodes = self.gltf.get("nodes", [])
        scene = self.gltf.get("scenes", [{"nodes": list(range(len(nodes)))}])[self.gltf.get("scene", 0)]
        found: list[tuple[dict[str, Any], list[float]]] = []
        stack = [(index, IDENTITY) for index in scene.get("nodes", [])]
        visited = 0
        while stack:
            index, parent = stack.pop()
            visited += 1
            if visited > 10_000:
                raise ValueError("the node graph is too large or cyclic")
            node = nodes[index]
            world = _multiply(parent, _local_matrix(node))
            if "mesh" in node:
                found.append((self.gltf["meshes"][node["mesh"]], world))
            stack += [(child, world) for child in node.get("children", [])]
        return found


def _material(model: Model, index: int | None, metallic_default: float) -> dict[str, Any]:
    material = model.gltf.get("materials", [])[index] if index is not None else {}
    pbr = material.get("pbrMetallicRoughness", {})
    textured = "metallicRoughnessTexture" in pbr
    # Factors default to 1, which with an ignored metallic-roughness map would turn every prop into chrome.
    return {
        "name": material.get("name", ""),
        "image": model.image(pbr.get("baseColorTexture", {}).get("index")),
        "color": [float(value) for value in pbr.get("baseColorFactor", (1, 1, 1, 1))],
        "metallic": metallic_default if textured and "metallicFactor" not in pbr else float(pbr.get("metallicFactor", 0.0 if textured else 1.0)),
        "roughness": (0.35 if metallic_default else 0.6) if textured and "roughnessFactor" not in pbr else float(pbr.get("roughnessFactor", 1.0)),
        "blend": material.get("alphaMode") == "BLEND",
        "emissive": [float(value) for value in material.get("emissiveFactor", (0, 0, 0))],
        "emissiveImage": model.image(material.get("emissiveTexture", {}).get("index")),
    }


def _number(value: float) -> str:
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _texture(material: dict[str, Any]) -> str:
    def image(name: str, extra: str = "") -> str:
        kind = IMAGE_TYPES[os.path.splitext(name)[1].lower()]
        return f'image_map {{ {kind} "{name}" interpolate 2{extra} }}'

    red, green, blue, alpha = material["color"]
    if max(material["emissive"]) > 0:
        name = material["emissiveImage"] or material["image"]
        if name:
            kind = IMAGE_TYPES[os.path.splitext(name)[1].lower()]
            # Dark texels of a blended emitter become fully transparent instead of a black card.
            pigment = f'pigment {{ image_pattern {{ {kind} "{name}" interpolate 2 }} pigment_map {{ [0 rgbt <0, 0, 0, 1>] [1 {image(name)}] }} }}'
        else:
            pigment = f"pigment {{ rgb <{', '.join(_number(value) for value in material['emissive'])}> }}"
        return f"texture {{ uv_mapping {pigment} finish {{ emission 1 diffuse 0 specular 0 }} }}"
    roughness = min(max(material["roughness"], 0.0), 1.0)
    metallic = min(max(material["metallic"], 0.0), 1.0)
    finish = f"diffuse 0.85 specular {_number(0.02 + 0.6 * (1 - roughness) ** 2)} roughness {_number(max(0.001, 0.05 * roughness * roughness))}"
    if metallic > 0.5:
        finish += f" metallic reflection {{ {_number(0.15 * (1 - roughness))} metallic }}"
    if material["blend"]:
        # Blended glass in these scans has a dark base map; transmit keeps it clear instead of tinting it black.
        finish = "diffuse 0.3 specular 0.8 roughness 0.002 reflection 0.12"
    if material["image"]:
        extra = f" transmit all {_number(min(0.95, max(0.6, 1 - alpha)) if alpha < 1 else 0.9)}" if material["blend"] else ""
        pigment = f"pigment {{ {image(material['image'], extra)} }}"
    else:
        transparency = f", {_number(1 - alpha)}" if material["blend"] or alpha < 1 else ""
        pigment = f"pigment {{ rgb{'f' if transparency else ''} <{_number(red)}, {_number(green)}, {_number(blue)}{transparency}> }}"
    return f"texture {{ uv_mapping {pigment} finish {{ {finish} }} }}"


def convert(path: str, resolve: Callable[[str], str], name: str, title: str, source: str, metallic_default: float = 0.0) -> tuple[str, dict[str, Any]]:
    """Return the include text declaring object `name`, and statistics (triangles, bounds, images)."""
    if IDENTIFIER.fullmatch(name) is None or len(name) > 60:
        raise ValueError("model identifier is invalid")
    model = Model(path, resolve)
    parts: list[dict[str, Any]] = []
    triangles = 0
    for mesh, world in model.instances():
        normals_matrix = _normal_matrix(world)
        for primitive in mesh["primitives"]:
            if primitive.get("mode", 4) != 4:
                raise ValueError("only triangle primitives are supported")
            attributes = primitive["attributes"]
            positions = []
            for x, y, z in model.accessor(attributes["POSITION"]):
                wx = world[0] * x + world[4] * y + world[8] * z + world[12]
                wy = world[1] * x + world[5] * y + world[9] * z + world[13]
                wz = world[2] * x + world[6] * y + world[10] * z + world[14]
                positions.append((wx, wy, -wz))
            normals = []
            for x, y, z in model.accessor(attributes["NORMAL"]) if "NORMAL" in attributes else []:
                nx = normals_matrix[0] * x + normals_matrix[3] * y + normals_matrix[6] * z
                ny = normals_matrix[1] * x + normals_matrix[4] * y + normals_matrix[7] * z
                nz = normals_matrix[2] * x + normals_matrix[5] * y + normals_matrix[8] * z
                length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                normals.append((nx / length, ny / length, -nz / length))
            uvs = [(u, 1.0 - v) for u, v in model.accessor(attributes["TEXCOORD_0"])] if "TEXCOORD_0" in attributes else []
            flat = [int(value[0]) for value in model.accessor(primitive["indices"])] if "indices" in primitive else list(range(len(positions)))
            faces = []
            for first in range(0, len(flat) - 2, 3):
                a, b, c = flat[first : first + 3]
                if max(a, b, c) >= len(positions):
                    raise ValueError("a face index is out of range")
                if a != b and b != c and a != c:
                    faces.append((a, b, c))
            triangles += len(faces)
            if triangles > MAX_TRIANGLES:
                raise ValueError(f"the model has more than {MAX_TRIANGLES} triangles")
            parts.append({"positions": positions, "normals": normals, "uvs": uvs, "faces": faces, "material": _material(model, primitive.get("material"), metallic_default)})
    if not parts:
        raise ValueError("the model has no meshes")
    every = [point for part in parts for point in part["positions"]]
    low = [min(point[axis] for point in every) for axis in range(3)]
    high = [max(point[axis] for point in every) for axis in range(3)]
    shift = (-(low[0] + high[0]) / 2, -low[1], -(low[2] + high[2]) / 2)
    minimum = [low[axis] + shift[axis] for axis in range(3)]
    maximum = [high[axis] + shift[axis] for axis in range(3)]

    def vector(values: tuple[float, ...]) -> str:
        return f"<{', '.join(_number(value) for value in values)}>"

    meshes = []
    images: list[str] = []
    for part in parts:
        material = part["material"]
        for image in (material["image"], material["emissiveImage"]):
            if image and image not in images:
                images.append(image)
        lines = ["  mesh2 {", f"    vertex_vectors {{ {len(part['positions'])},"]
        lines += [f"      {vector((x + shift[0], y + shift[1], z + shift[2]))}," for x, y, z in part["positions"]]
        lines[-1] = lines[-1].rstrip(",")
        lines.append("    }")
        if len(part["normals"]) == len(part["positions"]):
            lines.append(f"    normal_vectors {{ {len(part['normals'])},")
            lines += [f"      {vector(normal)}," for normal in part["normals"]]
            lines[-1] = lines[-1].rstrip(",")
            lines.append("    }")
        if len(part["uvs"]) == len(part["positions"]):
            lines.append(f"    uv_vectors {{ {len(part['uvs'])},")
            lines += [f"      {vector(uv)}," for uv in part["uvs"]]
            lines[-1] = lines[-1].rstrip(",")
            lines.append("    }")
        lines.append(f"    face_indices {{ {len(part['faces'])},")
        lines += [f"      <{a}, {b}, {c}>," for a, b, c in part["faces"]]
        lines[-1] = lines[-1].rstrip(",")
        lines.append("    }")
        if material["emissive"] and max(material["emissive"]) > 0:
            lines.append("    no_shadow")
        lines.append(f"    {_texture(material)}")
        lines.append("  }")
        meshes.append("\n".join(lines))
    body = meshes[0].replace("  mesh2 {", "mesh2 {", 1) if len(meshes) == 1 else "union {\n" + "\n".join(meshes) + "\n}"
    text = "\n".join([
        f"// VantaMCPd image-render model: {title}. Poly Haven {source}, CC0 1.0. Generated at installation.",
        f"// {triangles} triangles in metres, standing on y=0 and centred on x and z; front faces -z.",
        f"#ifndef ({name}_Inc_Temp)",
        f"#declare {name}_Inc_Temp = version;",
        "#version 3.7;",
        f"#declare {name}_Min = {vector(tuple(minimum))}; // Lower bounding corner",
        f"#declare {name}_Max = {vector(tuple(maximum))}; // Upper bounding corner",
        f"// {title}",
        f"#declare {name} = {body}",
        f"#version {name}_Inc_Temp;",
        "#end",
        "",
    ])
    return text, {"triangles": triangles, "min": minimum, "max": maximum, "images": images}


def self_test() -> None:
    import tempfile

    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    normals = struct.pack("<9f", 0, 0, 1, 0, 0, 1, 0, 0, 1)
    uvs = struct.pack("<6f", 0, 0, 1, 0, 0, 1)
    indices = struct.pack("<3H", 0, 1, 2) + b"\x00\x00"
    payload = positions + normals + uvs + indices
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"children": [1], "translation": [5, 2, 1]}, {"mesh": 0, "scale": [2, 2, 2]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}, "indices": 3, "material": 0}]}],
        "materials": [{"name": "m", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicRoughnessTexture": {"index": 0}}}],
        "textures": [{"source": 0}],
        "images": [{"uri": "textures/thing_diff_1k.jpg"}],
        "buffers": [{"uri": "thing.bin", "byteLength": len(payload)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 36},
            {"buffer": 0, "byteOffset": 36, "byteLength": 36},
            {"buffer": 0, "byteOffset": 72, "byteLength": 24},
            {"buffer": 0, "byteOffset": 96, "byteLength": 6},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": 3, "type": "VEC2"},
            {"bufferView": 3, "componentType": 5123, "count": 3, "type": "SCALAR"},
        ],
    }
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "thing.bin"), "wb") as handle:
            handle.write(payload)

        def run(document: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            with open(os.path.join(root, "thing.gltf"), "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            return convert(os.path.join(root, "thing.gltf"), lambda uri: os.path.join(root, uri.rsplit("/", 1)[-1]), "Vanta_Model_Thing", "Test thing", "thing")

        text, stats = run(gltf)
        assert stats["triangles"] == 1 and stats["images"] == ["thing_diff_1k.jpg"], stats
        assert stats["min"] == [-1.0, 0.0, 0.0] and stats["max"] == [1.0, 2.0, 0.0], stats
        assert "#declare Vanta_Model_Thing = mesh2 {" in text and "<-1, 0, 0>," in text and "<1, 2, 0>" in text
        assert "<0, 0, -1>" in text, "normals must be mirrored with z"
        assert "<1, 1>" in text and "<0, 0>" in text, "v must be flipped"
        assert 'image_map { jpeg "thing_diff_1k.jpg" interpolate 2 }' in text and "metallic" not in text
        assert "face_indices { 1," in text and "#ifndef (Vanta_Model_Thing_Inc_Temp)" in text
        gltf["materials"][0] = {"alphaMode": "BLEND", "emissiveFactor": [1, 1, 1], "emissiveTexture": {"index": 0}, "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}
        text, _ = run(gltf)
        assert "emission 1" in text and "rgbt <0, 0, 0, 1>" in text and "no_shadow" in text
        gltf["accessors"][3]["count"] = 400
        try:
            run(gltf)
        except ValueError:
            pass
        else:
            raise AssertionError("an accessor past its buffer was accepted")


if __name__ == "__main__":
    self_test()

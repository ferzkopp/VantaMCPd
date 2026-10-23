#!/usr/bin/env python3
"""Download the pinned CC0 asset packs, convert the model pack and generate vanta_assets.inc. Runs once at installation."""
import hashlib
import json
import os
import shutil
import sys
import urllib.request
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gltf_mesh2

USER_AGENT = "VantaMCPd-image-render/0.3.1 (asset installer)"
TIMEOUT_SECONDS = 60
PACKS = ("hdri", "textures", "models")
BUMP_SIZE = 1.0


def parse_selection(raw: str) -> tuple[str, ...]:
    """The assets install option: a JSON list of packs or ["none"]; a legacy single word is still accepted."""
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    if isinstance(value, str):
        value = list(PACKS) if value == "all" else [value]
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ValueError("assets must be a non-empty list of pack names")
    if len(set(value)) != len(value) or any(item not in (*PACKS, "none") for item in value):
        raise ValueError(f"assets may contain {', '.join(PACKS)} or none, each at most once")
    if "none" in value:
        if len(value) > 1:
            raise ValueError("assets none cannot be combined with other packs")
        return ()
    return tuple(pack for pack in PACKS if pack in value)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: str, expected_bytes: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temporary = target + ".part"
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response, open(temporary, "wb") as handle:
        received = 0
        for chunk in iter(lambda: response.read(256 * 1024), b""):
            received += len(chunk)
            if received > expected_bytes:
                raise ValueError("download is larger than the pinned size")
            handle.write(chunk)
    os.replace(temporary, target)


def _fetch_file(item: dict[str, Any], target: str, cache: str | None) -> None:
    """Copy a hash-matching file from the previous installation, or download it, and verify it."""
    cached = os.path.join(cache, item["name"]) if cache else None
    try:
        if cached and os.path.isfile(cached) and not os.path.islink(cached) and _sha256(cached) == item["sha256"]:
            shutil.copyfile(cached, target)
        else:
            _download(item["url"], target, item["bytes"])
        if _sha256(target) != item["sha256"]:
            raise ValueError("SHA-256 mismatch")
    except Exception:
        for path in (target, target + ".part"):
            if os.path.exists(path):
                os.unlink(path)
        raise
    os.chmod(target, 0o644)


def _fetch_map(item: dict[str, Any], directory: str, cache: str | None, pack: str, skip: Callable[[str, Exception], None]) -> dict[str, Any]:
    _fetch_file(item, os.path.join(directory, item["name"]), cache)
    entry: dict[str, Any] = {"path": f"assets/{pack}/{item['name']}", "bytes": item["bytes"]}
    detail = {}
    for extra in item.get("detail", []):
        try:
            _fetch_file(extra, os.path.join(directory, extra["name"]), cache)
        except Exception as error:  # The base map still installs; its texture falls back to the simple form.
            skip(extra["name"], error)
            continue
        detail[extra["kind"]] = extra["name"]
    if detail:
        entry["detail"] = detail
    return entry


def _fetch_model(item: dict[str, Any], directory: str, cache: str | None, pack: str, skip: Callable[[str, Exception], None]) -> dict[str, Any]:
    staging = os.path.join(directory, f".src-{item['source']}")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    include = os.path.join(directory, f"vanta_model_{item['identifier'].lower()}.inc")
    try:
        for part in item["files"]:
            _fetch_file(part, os.path.join(staging, part["name"]), cache)
        gltf = next(part["name"] for part in item["files"] if part["name"].endswith(".gltf"))
        text, stats = gltf_mesh2.convert(
            os.path.join(staging, gltf),
            lambda uri: os.path.join(staging, uri.replace("\\", "/").rsplit("/", 1)[-1]),
            f"Vanta_Model_{item['identifier']}",
            item["title"],
            item["source"],
        )
        for name in stats["images"]:
            os.replace(os.path.join(staging, name), os.path.join(directory, name))
        with open(include, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(include, 0o644)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "path": f"assets/{pack}/{os.path.basename(include)}",
        "bytes": os.path.getsize(include),
        "triangles": stats["triangles"],
        "sizeMetres": [round(stats["max"][axis] - stats["min"][axis], 3) for axis in range(3)],
    }


def fetch(catalog: dict[str, Any], packs: tuple[str, ...], destination: str, previous: str | None, report: bool = True) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []

    def skip(name: str, error: Exception) -> None:
        missing.append({"name": name, "error": str(error)[:200]})
        if report:
            print(f"asset {name} skipped: {error}", file=sys.stderr)

    for pack in packs:
        definition = catalog["packs"][pack]
        directory = os.path.join(destination, pack)
        cache = os.path.join(previous, pack) if previous else None
        os.makedirs(directory, exist_ok=True)
        installed = 0
        for item in definition["files"]:
            try:
                entry = (_fetch_model if pack == "models" else _fetch_map)(item, directory, cache, pack, skip)
            except Exception as error:  # Assets are optional; a failed file or model is reported, never installed.
                skip(item.get("name") or item["identifier"], error)
                continue
            entry.update({"pack": pack, "identifier": item["identifier"], "title": item["title"], "license": catalog["license"], "page": item["page"]})
            files.append(entry)
            installed += 1
        summary[pack] = {"description": definition["description"], "files": installed, "expected": len(definition["files"])}
    include = generate_include(catalog, files)
    with open(os.path.join(destination, "vanta_assets.inc"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(include)
    manifest = {"selection": list(packs) or ["none"], "license": catalog["license"], "attribution": catalog["attribution"], "packs": summary, "files": files, "missing": missing}
    with open(os.path.join(destination, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1)
    return manifest


def _image(name: str) -> str:
    return f'{"png" if name.lower().endswith(".png") else "jpeg"} "{name}"'


def texture_declarations(prefix: str, identifier: str, title: str, diffuse: str, detail: dict[str, str]) -> list[str]:
    """P_, N_, T_ and T_..._Simple declarations for one photographic material on the unit square."""
    name = f"{prefix}_{identifier}"
    lines = [f"#declare P_{name} = pigment {{ image_map {{ {_image(diffuse)} interpolate 2 }} }} // {title}"]
    normal = ""
    if "disp" in detail:
        lines.append(f"#declare N_{name} = normal {{ bump_map {{ {_image(detail['disp'])} interpolate 2 bump_size {BUMP_SIZE} }} }} // {title} relief")
        normal = f" normal {{ N_{name} }}"
    lines.append(f"#declare T_{name}_Simple = texture {{ pigment {{ P_{name} }} finish {{ diffuse 0.9 specular 0.05 roughness 0.05 }} }} // {title}, colour only")
    if "rough" in detail:
        lines += [
            f"#declare T_{name} = texture {{ // {title}",
            f"  image_pattern {{ {_image(detail['rough'])} interpolate 2 }}",
            "  texture_map {",
            f"    [0 pigment {{ P_{name} }}{normal} finish {{ diffuse 0.85 specular 0.6 roughness 0.004 }}]",
            f"    [1 pigment {{ P_{name} }}{normal} finish {{ diffuse 0.9 specular 0.02 roughness 0.1 }}]",
            "  }",
            "}",
        ]
    else:
        lines.append(f"#declare T_{name} = texture {{ pigment {{ P_{name} }}{normal} finish {{ diffuse 0.9 specular 0.05 roughness 0.05 }} }} // {title}")
    return lines


def sky_macros(prefix: str) -> list[str]:
    """<prefix>_HDRI_Sky(Name, Intensity) and its rotated form, returning an equirectangular sky_sphere."""
    return [
        f"#macro {prefix}_HDRI_Sky_Rotated(Name, Intensity, Degrees)",
        "  sky_sphere {",
        "    pigment { image_map { hdr Name gamma 1.0 map_type 1 interpolate 2 } }",
        "    scale <-1, 1, 1>",
        "    rotate y*Degrees",
        "    emission Intensity",
        "  }",
        "#end",
        f"#macro {prefix}_HDRI_Sky(Name, Intensity)",
        f"  {prefix}_HDRI_Sky_Rotated(Name, Intensity, 0)",
        "#end",
    ]


def generate_include(catalog: dict[str, Any], files: list[dict[str, Any]]) -> str:
    textures = [entry for entry in files if entry["pack"] == "textures"]
    hdris = [entry for entry in files if entry["pack"] == "hdri"]
    models = [entry for entry in files if entry["pack"] == "models"]
    names = {entry["identifier"]: entry["path"].rsplit("/", 1)[-1] for entry in files}
    lines = [
        "// VantaMCPd image-render asset library, generated at installation.",
        f"// {catalog['attribution']}",
        "#ifndef (Vanta_Assets_Inc_Temp)",
        "#declare Vanta_Assets_Inc_Temp = version;",
        "#version 3.7;",
        "",
        "// Photographic maps cover the unit square <0,0>-<1,1> in the x-y plane and repeat.",
        "// For a floor: plane { y, 0 texture { T_Vanta_Wood_Table rotate x*90 scale 2 } }",
        "// T_Vanta_<Name> adds displacement bump relief and roughness-driven gloss; T_Vanta_<Name>_Simple is colour only and renders faster.",
    ]
    for entry in textures:
        lines += texture_declarations("Vanta", entry["identifier"], entry["title"], names[entry["identifier"]], entry.get("detail", {}))
    if hdris:
        lines += [
            "",
            "// Equirectangular HDR environments. Vanta_HDRI_Sky(Name, Intensity) returns a sky_sphere; with",
            "// radiosity enabled it also lights the scene. Rotate it with Vanta_HDRI_Sky_Rotated(Name, Intensity, Degrees).",
        ]
        for entry in hdris:
            lines.append(f'#declare Vanta_HDRI_{entry["identifier"]} = "{names[entry["identifier"]]}"; // {entry["title"]}')
        lines += sky_macros("Vanta")
    if models:
        lines += ["", "// Models live in their own include files, so a scene parses only the meshes it uses:"]
        for entry in models:
            size = " x ".join(f"{value:g}" for value in entry["sizeMetres"])
            lines.append(f'//   #include "{names[entry["identifier"]]}" object {{ Vanta_Model_{entry["identifier"]} }} // {entry["title"]}, {size} m')
    lines += ["", "#version Vanta_Assets_Inc_Temp;", "#end", ""]
    return "\n".join(lines)


def self_test() -> None:
    import struct
    import tempfile

    assert parse_selection('["models", "hdri"]') == ("hdri", "models") and parse_selection("all") == PACKS
    assert parse_selection('["none"]') == () and parse_selection("textures") == ("textures",)
    for bad in ('["none", "hdri"]', "[]", '["hdri", "hdri"]', '["meshes"]', "7"):
        try:
            parse_selection(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted asset selection {bad}")
    with tempfile.TemporaryDirectory() as root:
        previous = os.path.join(root, "previous")
        for pack in ("textures", "models"):
            os.makedirs(os.path.join(previous, pack))

        def cached(pack: str, name: str, payload: bytes) -> dict[str, Any]:
            with open(os.path.join(previous, pack, name), "wb") as handle:
                handle.write(payload)
            return {"name": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "url": "http://127.0.0.1:9/unused"}

        wood = cached("textures", "wood.jpg", b"\xff\xd8\xffjpeg")
        disp = cached("textures", "wood_disp.png", b"\x89PNGdisp")
        rough = {"name": "wood_rough.jpg", "bytes": 4, "sha256": "0" * 64, "url": "http://127.0.0.1:9/unreachable"}
        buffer = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
        gltf = json.dumps({
            "asset": {"version": "2.0"}, "nodes": [{"mesh": 0}], "scenes": [{"nodes": [0]}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "buffers": [{"uri": "box.bin", "byteLength": len(buffer)}],
            "bufferViews": [{"buffer": 0, "byteLength": len(buffer)}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
        }).encode()
        model_files = [cached("models", "box.gltf", gltf), cached("models", "box.bin", buffer)]
        catalog = {
            "license": "CC0-1.0",
            "attribution": "Test assets",
            "packs": {
                "textures": {"description": "maps", "files": [
                    {**wood, "identifier": "Wood", "page": "p", "title": "Wood", "detail": [{**disp, "kind": "disp"}, {**rough, "kind": "rough"}]},
                    {"identifier": "Broken", "name": "broken.jpg", "bytes": 4, "sha256": "0" * 64, "url": "http://127.0.0.1:9/unreachable", "page": "p", "title": "Broken"},
                ]},
                "hdri": {"description": "env", "files": []},
                "models": {"description": "props", "files": [
                    {"identifier": "Box", "source": "box", "title": "Box", "page": "p", "files": model_files},
                ]},
            },
        }
        destination = os.path.join(root, "assets")
        manifest = fetch(catalog, PACKS, destination, previous, report=False)
        assert manifest["packs"]["textures"] == {"description": "maps", "files": 1, "expected": 2}
        assert [entry["name"] for entry in manifest["missing"]] == ["wood_rough.jpg", "broken.jpg"]
        assert not os.path.exists(os.path.join(destination, "textures", "broken.jpg"))
        assert sorted(os.listdir(os.path.join(destination, "models"))) == ["vanta_model_box.inc"], "model sources must be removed"
        model = next(entry for entry in manifest["files"] if entry["pack"] == "models")
        assert model["triangles"] == 1 and model["sizeMetres"] == [1.0, 1.0, 0.0], model
        with open(os.path.join(destination, "vanta_assets.inc"), "r", encoding="utf-8") as handle:
            include = handle.read()
        assert 'jpeg "wood.jpg"' in include and 'bump_map { png "wood_disp.png"' in include and "Broken" not in include
        assert "#declare T_Vanta_Wood_Simple" in include and "normal { N_Vanta_Wood }" in include and "image_pattern" not in include
        assert '#include "vanta_model_box.inc" object { Vanta_Model_Box }' in include
        assert "Vanta_HDRI_Sky" not in include
    assert "Vanta_HDRI_Sky(Name, Intensity)" in generate_include({"attribution": "x"}, [{"pack": "hdri", "identifier": "Sky", "path": "assets/hdri/sky.hdr", "title": "Sky"}])
    rough_include = generate_include({"attribution": "x"}, [{"pack": "textures", "identifier": "Oak", "path": "assets/textures/oak.jpg", "title": "Oak", "detail": {"rough": "oak_rough.jpg"}}])
    assert 'image_pattern { jpeg "oak_rough.jpg" interpolate 2 }' in rough_include and "N_Vanta_Oak" not in rough_include


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--normalize":
        print(json.dumps(list(parse_selection(sys.argv[2])) or ["none"]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "--fetch":
        packs, destination = parse_selection(sys.argv[2]), sys.argv[3]
        previous = sys.argv[4] if len(sys.argv) > 4 and os.path.isdir(sys.argv[4]) else None
        with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets.json"), "r", encoding="utf-8") as handle:
            catalog = json.load(handle)
        os.makedirs(destination, exist_ok=True)
        manifest = fetch(catalog, packs, destination, previous)
        print(json.dumps({"packs": manifest["packs"], "missing": len(manifest["missing"])}))
    else:
        self_test()


if __name__ == "__main__":
    main()

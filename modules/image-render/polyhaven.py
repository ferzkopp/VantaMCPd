#!/usr/bin/env python3
"""On-demand Poly Haven assets for Image Render.

The broker has no network. At installation, --index stores a trimmed copy of the public Poly Haven asset
list. At runtime an agent searches it, downloads an asset's file listing and the planned files with
browser-retrieval's web_download, and imports them: every file is checked against the listing's size and
MD5, then turned into a POV-Ray include (textures and HDRIs) or converted to mesh2 (models) and published
as an artifact. Poly Haven's API terms ask integrations to credit Poly Haven, so every result carries it.
"""
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gltf_mesh2
from assets import sky_macros, texture_declarations

API_ASSETS = "https://api.polyhaven.com/assets?type=all"
API_FILES = "https://api.polyhaven.com/files/{}"
DOWNLOAD_HOST = "dl.polyhaven.org"
USER_AGENT = "VantaMCPd-image-render/0.3.1 (asset index)"
CREDIT = "Powered by Poly Haven (https://polyhaven.com). Assets are CC0 1.0."
INDEX_FILE = "polyhaven.json"
INDEX_VERSION = 1
KINDS = {0: "hdri", 1: "texture", 2: "model"}
RESOLUTIONS = ("1k", "2k")
ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
FILE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
TOKEN = re.compile(r"[a-z0-9]+")
MAX_LISTING_BYTES = 2_000_000
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ASSET_BYTES = 160 * 1024 * 1024
MAX_MODEL_POLYGONS = 60_000
METALS = {"metal", "brass", "bronze", "copper", "steel", "iron", "aluminium", "aluminum", "gold", "silver", "chrome", "tin", "pewter"}
# Model maps POV-Ray cannot apply to a mesh; base colour and emission maps are kept.
UNUSED_MODEL_MAPS = re.compile(r"_(nor_gl|nor_dx|arm|ao|rough|roughness|metal|metallic|disp|displacement|spec|specular|bump|opacity|alpha)_\d+k\.", re.IGNORECASE)


def _token(asset_id: str) -> str:
    """A POV-Ray identifier fragment short enough that T_PH_<token>_Simple stays within 40 characters."""
    clean = re.sub(r"[^A-Za-z0-9_]", "_", asset_id)
    if len(clean) <= 26:
        return clean
    return f"{clean[:21]}_{hashlib.sha256(asset_id.encode()).hexdigest()[:4]}"


def trim_index(raw: dict[str, Any]) -> dict[str, Any]:
    assets = {}
    for asset_id, entry in raw.items():
        if not isinstance(entry, dict) or ASSET_ID.fullmatch(asset_id) is None or entry.get("type") not in KINDS:
            continue
        item: dict[str, Any] = {
            "kind": KINDS[entry["type"]],
            "name": str(entry.get("name", asset_id))[:120],
            "category": str(entry.get("category") or "/".join(entry.get("categories", [])))[:160],
            "tags": [str(tag).strip()[:40] for tag in entry.get("tags", [])][:16],
            "description": str(entry.get("description", ""))[:300],
            "downloads": int(entry.get("download_count", 0) or 0),
        }
        if isinstance(entry.get("polycount"), (int, float)):
            item["polycount"] = int(entry["polycount"])
        materials = (entry.get("attributes") or {}).get("material") if isinstance(entry.get("attributes"), dict) else None
        if isinstance(materials, list) and materials:
            item["materials"] = [str(material).lower()[:30] for material in materials][:8]
        if item["kind"] == "model" and isinstance(entry.get("dimensions"), list) and len(entry["dimensions"]) == 3:
            # Poly Haven reports Blender's Z-up millimetres; report x, height, depth like the converted mesh.
            width, depth, height = (round(float(value) / 1000, 3) for value in entry["dimensions"])
            item["sizeMetres"] = [width, height, depth]
        assets[asset_id] = item
    return {"version": INDEX_VERSION, "source": API_ASSETS, "credit": CREDIT, "assets": assets}


def fetch_index(library_dir: str) -> dict[str, Any]:
    request = urllib.request.Request(API_ASSETS, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = json.loads(response.read(20 * 1024 * 1024 + 1))
    index = trim_index(raw)
    target = os.path.join(library_dir, INDEX_FILE)
    with open(target + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(target + ".tmp", target)
    return {"assets": len(index["assets"]), "kinds": {kind: sum(1 for item in index["assets"].values() if item["kind"] == kind) for kind in KINDS.values()}}


class Catalog:
    def __init__(self, library_dir: str):
        path = os.path.join(library_dir, INDEX_FILE)
        if not os.path.isfile(path):
            raise ValueError("the Poly Haven index is not installed; reinstall image-render while api.polyhaven.com is reachable")
        with open(path, "r", encoding="utf-8") as handle:
            index = json.load(handle)
        if index.get("version") != INDEX_VERSION:
            raise ValueError("the Poly Haven index has an unsupported version")
        self.assets: dict[str, dict[str, Any]] = index["assets"]

    def summary(self) -> dict[str, Any]:
        return {"assets": len(self.assets), "kinds": {kind: sum(1 for item in self.assets.values() if item["kind"] == kind) for kind in KINDS.values()}, "credit": CREDIT}

    def get(self, asset_id: str) -> dict[str, Any]:
        if ASSET_ID.fullmatch(asset_id) is None or asset_id not in self.assets:
            raise ValueError(f"unknown Poly Haven asset {asset_id[:80]}; use operation search")
        return self.assets[asset_id]

    def search(self, query: str | None, kind: str | None, limit: int) -> dict[str, Any]:
        terms = TOKEN.findall((query or "").lower())
        scored = []
        for asset_id, item in self.assets.items():
            if kind and item["kind"] != kind:
                continue
            score = 0.0
            matched = 0
            if terms:
                name = item["name"].lower()
                tags = " ".join(item["tags"]).lower()
                category = item["category"].lower()
                description = item["description"].lower()
                for term in terms:
                    hit = 0.0
                    if term in asset_id.lower() or term in name:
                        hit += 4
                    if term in tags:
                        hit += 3
                    if term in category:
                        hit += 2
                    if term in description:
                        hit += 1
                    matched += hit > 0
                    score += hit
                if not matched:
                    continue
            scored.append((matched, score, item["downloads"], asset_id))
        scored.sort(key=lambda entry: (-entry[0], -entry[1], -entry[2], entry[3]))
        results = []
        for _, _, _, asset_id in scored[:limit]:
            item = self.assets[asset_id]
            result = {"assetId": asset_id, **{key: item[key] for key in ("kind", "name", "category") if item.get(key)}, "tags": item["tags"][:8]}
            for key in ("polycount", "sizeMetres", "materials"):
                if key in item:
                    result[key] = item[key]
            result["filesUrl"] = API_FILES.format(asset_id)
            results.append(result)
        return {
            "query": query,
            "kind": kind,
            "total": len(scored),
            "results": results,
            "next": "Download filesUrl with browser-retrieval web_download (name '<assetId>.files.json'), then call operation plan with its artifactId.",
            "credit": CREDIT,
        }


def _file(entry: Any, role: str, required: bool) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError(f"the file listing has no {role} file at this resolution")
    url = str(entry.get("url", ""))
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != DOWNLOAD_HOST:
        raise ValueError(f"the {role} file is not served from https://{DOWNLOAD_HOST}")
    name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
    size = entry.get("size")
    md5 = str(entry.get("md5", "")).lower()
    if FILE_NAME.fullmatch(name) is None or not isinstance(size, int) or not 0 < size <= MAX_FILE_BYTES or re.fullmatch(r"[0-9a-f]{32}", md5) is None:
        raise ValueError(f"the {role} file entry is invalid or larger than {MAX_FILE_BYTES} bytes")
    return {"name": name, "url": url, "bytes": size, "md5": md5, "role": role, "required": required}


def plan(item: dict[str, Any], listing: dict[str, Any], resolution: str) -> list[dict[str, Any]]:
    """The exact files to download for one asset at one resolution."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    kind = item["kind"]
    if kind == "hdri":
        files = [_file(listing.get("hdri", {}).get(resolution, {}).get("hdr"), "hdr", True)]
    elif kind == "texture":
        diffuse = listing.get("Diffuse", {}).get(resolution, {})
        files = [_file(diffuse.get("jpg") or diffuse.get("png"), "diffuse", True)]
        for key, role, extension in (("Displacement", "disp", "png"), ("Rough", "rough", "jpg")):
            entry = listing.get(key, {}).get(resolution, {}).get(extension)
            if entry:
                files.append(_file(entry, role, False))
    else:
        if int(item.get("polycount", 0)) > MAX_MODEL_POLYGONS:
            raise ValueError(f"the model has more than {MAX_MODEL_POLYGONS} polygons; choose a lighter model")
        gltf = listing.get("gltf", {}).get(resolution, {}).get("gltf")
        files = [_file(gltf, "gltf", True)]
        for path, entry in sorted((gltf or {}).get("include", {}).items()):
            if not UNUSED_MODEL_MAPS.search(path):
                files.append(_file(entry, "buffer" if path.endswith(".bin") else "image", True))
    names = [entry["name"] for entry in files]
    if len(set(name.lower() for name in names)) != len(names):
        raise ValueError("the file listing contains duplicate file names")
    if sum(entry["bytes"] for entry in files) > MAX_ASSET_BYTES:
        raise ValueError(f"the asset needs more than {MAX_ASSET_BYTES} bytes at {resolution}; try 1k")
    return files


def _md5(path: str) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(files: list[dict[str, Any]], staged: dict[str, str]) -> None:
    for entry in files:
        path = staged.get(entry["name"])
        if path is None:
            if entry["required"]:
                raise ValueError(f"missing {entry['role']} file {entry['name']}; download it with web_download")
            continue
        if os.path.getsize(path) != entry["bytes"] or _md5(path) != entry["md5"]:
            raise ValueError(f"{entry['name']} does not match the Poly Haven listing (size or MD5); download it again")


def build_include(asset_id: str, item: dict[str, Any], resolution: str, files: list[dict[str, Any]], staged: dict[str, str]) -> tuple[str, list[str], dict[str, Any]]:
    """Include text, the staged file names the scene must receive beside it, and details for the result."""
    token = _token(asset_id)
    title = item["name"]
    header = [
        f"// Poly Haven {item['kind']} {asset_id} at {resolution}: {title}. CC0 1.0. {CREDIT}",
        f"#ifndef (PH_{token}_Inc_Temp)",
        f"#declare PH_{token}_Inc_Temp = version;",
        "#version 3.7;",
    ]
    footer = [f"#version PH_{token}_Inc_Temp;", "#end", ""]
    present = {entry["role"]: entry["name"] for entry in files if entry["name"] in staged}
    if item["kind"] == "hdri":
        body = [
            f'#declare PH_HDRI_{token} = "{present["hdr"]}"; // {title}',
            "#ifndef (PH_HDRI_Sky_Macros)",
            "#declare PH_HDRI_Sky_Macros = 1;",
            *sky_macros("PH"),
            "#end",
        ]
        usage = f"PH_HDRI_Sky(PH_HDRI_{token}, 1.0) with radiosity enabled for image-based lighting"
        return "\n".join(header + body + footer), [present["hdr"]], {"declarations": [f"PH_HDRI_{token}", "PH_HDRI_Sky", "PH_HDRI_Sky_Rotated"], "usage": usage}
    if item["kind"] == "texture":
        detail = {role: name for role, name in present.items() if role in ("disp", "rough")}
        body = texture_declarations("PH", token, title, present["diffuse"], detail)
        names = [f"P_PH_{token}", *([f"N_PH_{token}"] if "disp" in detail else []), f"T_PH_{token}", f"T_PH_{token}_Simple"]
        usage = f"plane {{ y, 0 texture {{ T_PH_{token} rotate x*90 scale 2 }} }}; the map spans the unit square in the x-y plane"
        return "\n".join(header + body + footer), list(present.values()), {"declarations": names, "usage": usage}
    gltf = next(entry["name"] for entry in files if entry["role"] == "gltf")
    # The metal map cannot be applied, so an all-metal model gets a metallic finish instead of looking painted.
    metallic = 1.0 if item.get("materials") and set(item["materials"]) <= METALS else 0.0
    text, stats = gltf_mesh2.convert(staged[gltf], lambda uri: staged.get(uri.replace("\\", "/").rsplit("/", 1)[-1], "/nonexistent"), f"PH_Model_{token}", title, asset_id, metallic)
    needed = list(stats["images"])
    size = [round(stats["max"][axis] - stats["min"][axis], 3) for axis in range(3)]
    details = {
        "declarations": [f"PH_Model_{token}", f"PH_Model_{token}_Min", f"PH_Model_{token}_Max"],
        "usage": f"object {{ PH_Model_{token} }}; metres, standing on y=0, centred on x and z, front facing -z",
        "triangles": stats["triangles"],
        "sizeMetres": size,
    }
    return text, needed, details


def include_name(asset_id: str, resolution: str) -> str:
    return f"ph_{_token(asset_id).lower()}_{resolution}.inc"


def self_test() -> None:
    import tempfile

    raw = {
        "red_brick_03": {"type": 1, "name": "Red Brick 03", "category": "Brick/Walls", "tags": ["brick", "wall"], "download_count": 50, "description": "Red brick wall"},
        "venice_sunset": {"type": 0, "name": "Venice Sunset", "categories": ["outdoor", "sunset"], "tags": ["sun"], "download_count": 90},
        "tiny_chair": {"type": 2, "name": "Tiny Chair", "category": "Furniture/Seating", "tags": ["chair"], "polycount": 12, "dimensions": [400, 900, 450], "attributes": {"material": ["Brass"]}},
        "bad id!": {"type": 1, "name": "Bad"},
        "unknown": {"type": 7, "name": "Other"},
    }
    index = trim_index(raw)
    assert set(index["assets"]) == {"red_brick_03", "venice_sunset", "tiny_chair"}
    assert index["assets"]["tiny_chair"]["sizeMetres"] == [0.4, 0.45, 0.9] and index["assets"]["tiny_chair"]["materials"] == ["brass"]
    assert len(_token("kloofendal_48d_partly_cloudy_puresky")) == 26 and _token("red_brick_03") == "red_brick_03"
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, INDEX_FILE), "w", encoding="utf-8") as handle:
            json.dump(index, handle)
        catalog = Catalog(root)
        found = catalog.search("brick wall", None, 5)
        assert [entry["assetId"] for entry in found["results"]] == ["red_brick_03"] and found["results"][0]["filesUrl"].endswith("/files/red_brick_03")
        assert catalog.search("brick mossy", None, 5)["results"][0]["assetId"] == "red_brick_03", "partial matches still rank"
        assert [entry["assetId"] for entry in catalog.search(None, "hdri", 5)["results"]] == ["venice_sunset"]
        assert catalog.search("chair", "texture", 5)["results"] == [] and "Poly Haven" in found["credit"]

        def entry(name: str, payload: bytes) -> dict[str, Any]:
            return {"url": f"https://{DOWNLOAD_HOST}/file/ph-assets/x/{name}", "size": len(payload), "md5": hashlib.md5(payload).hexdigest()}

        diffuse, rough = b"\xff\xd8\xffdiff", b"\xff\xd8\xffrough"
        listing = {
            "Diffuse": {"1k": {"jpg": entry("red_brick_03_diff_1k.jpg", diffuse)}},
            "Rough": {"1k": {"jpg": entry("red_brick_03_rough_1k.jpg", rough)}},
            "Displacement": {"1k": {"png": entry("red_brick_03_disp_1k.png", b"\x89PNGd")}},
        }
        files = plan(catalog.get("red_brick_03"), listing, "1k")
        assert [(item["role"], item["required"]) for item in files] == [("diffuse", True), ("disp", False), ("rough", False)]
        staged = {}
        for name, payload in (("red_brick_03_diff_1k.jpg", diffuse), ("red_brick_03_rough_1k.jpg", rough)):
            staged[name] = os.path.join(root, name)
            with open(staged[name], "wb") as handle:
                handle.write(payload)
        verify(files, staged)
        text, needed, details = build_include("red_brick_03", catalog.get("red_brick_03"), "1k", files, staged)
        assert "#declare T_PH_red_brick_03 = texture" in text and "image_pattern" in text and "N_PH_red_brick_03" not in text
        assert sorted(needed) == ["red_brick_03_diff_1k.jpg", "red_brick_03_rough_1k.jpg"] and "T_PH_red_brick_03" in details["declarations"]
        with open(staged["red_brick_03_rough_1k.jpg"], "ab") as handle:
            handle.write(b"x")
        try:
            verify(files, staged)
        except ValueError as error:
            assert "MD5" in str(error)
        else:
            raise AssertionError("a tampered file passed verification")
        hdr = {"hdri": {"2k": {"hdr": entry("venice_sunset_2k.hdr", b"#?RADIANCE")}}}
        hdr_files = plan(catalog.get("venice_sunset"), hdr, "2k")
        text, needed, details = build_include("venice_sunset", catalog.get("venice_sunset"), "2k", hdr_files, {"venice_sunset_2k.hdr": "/x"})
        assert '#declare PH_HDRI_venice_sunset = "venice_sunset_2k.hdr";' in text and "#macro PH_HDRI_Sky(Name, Intensity)" in text
        for bad in (
            {"hdri": {"1k": {"hdr": {"url": "https://evil.example/x.hdr", "size": 4, "md5": "0" * 32}}}},
            {"hdri": {"1k": {"hdr": {"url": f"https://{DOWNLOAD_HOST}/x.hdr", "size": MAX_FILE_BYTES + 1, "md5": "0" * 32}}}},
            {"hdri": {}},
        ):
            try:
                plan(catalog.get("venice_sunset"), bad, "1k")
            except ValueError:
                continue
            raise AssertionError(f"accepted listing {bad}")
        model_listing = {"gltf": {"1k": {"gltf": {**entry("tiny_chair_1k.gltf", b"{}"), "include": {
            "tiny_chair.bin": entry("tiny_chair.bin", b"bin"),
            "textures/tiny_chair_diff_1k.jpg": entry("tiny_chair_diff_1k.jpg", b"d"),
            "textures/tiny_chair_nor_gl_1k.jpg": entry("tiny_chair_nor_gl_1k.jpg", b"n"),
            "textures/tiny_chair_arm_1k.jpg": entry("tiny_chair_arm_1k.jpg", b"a"),
        }}}}}
        assert [item["name"] for item in plan(catalog.get("tiny_chair"), model_listing, "1k")] == ["tiny_chair_1k.gltf", "tiny_chair_diff_1k.jpg", "tiny_chair.bin"]
    assert include_name("red_brick_03", "2k") == "ph_red_brick_03_2k.inc"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--index":
        try:
            print(json.dumps(fetch_index(sys.argv[2])))
        except Exception as error:  # The index is optional; the starter set still works without it.
            print(f"Poly Haven index unavailable: {error}", file=sys.stderr)
    else:
        self_test()

#!/usr/bin/env python3
import json
import math
import os
import re
import shutil
import subprocess
import warnings
from typing import Any

from schemas import FORMATS, MAX_DIMENSION, MAX_METADATA_ITEMS, MAX_PIXELS, MIME_TYPES

FORMAT_MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
MIME_FORMAT = {value: key for key, value in FORMAT_MIME.items()}
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def detect_magic(path: str) -> str:
    with open(path, "rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    raise ValueError("input is not a supported PNG, JPEG, WebP, or GIF image")


def verify_source(path: str, mime_type: str | None = None) -> str:
    image_format = detect_magic(path)
    if mime_type is not None and mime_type not in MIME_TYPES:
        raise ValueError("source MIME type is not supported")
    if mime_type is not None and MIME_FORMAT[mime_type] != image_format:
        raise ValueError(f"source MIME type says {mime_type} but content is {FORMAT_MIME[image_format]}")
    return image_format


def imagemagick() -> dict[str, Any] | None:
    magick = shutil.which("magick")
    if magick:
        return {"version": 7, "command": os.path.realpath(magick)}
    convert = shutil.which("convert")
    identify = shutil.which("identify")
    if convert and identify:
        return {"version": 6, "command": os.path.realpath(convert), "identify": os.path.realpath(identify)}
    return None


def pillow() -> dict[str, Any] | None:
    try:
        import PIL
        from PIL import features
    except ImportError:
        return None
    supported = ["png", "jpeg", "gif"]
    if features.check("webp"):
        supported.append("webp")
    return {"version": PIL.__version__, "formats": supported}


def environment(preference: str, artifact_available: bool, limits: dict[str, Any]) -> dict[str, Any]:
    magick = imagemagick()
    pil = pillow()
    available = []
    if magick:
        available.append({"name": "imagemagick", "version": magick["version"], "command": magick["command"]})
    if pil:
        available.append({"name": "pillow", **pil})
    selected = select_backend(preference, "edit") if available else None
    return {
        "preferredBackend": preference,
        "selectedBackend": selected,
        "backends": available,
        "formats": list(FORMATS),
        "gifBehavior": "first-frame",
        "artifactStorage": {"available": artifact_available, "read": artifact_available, "write": artifact_available},
        "operations": {
            "inspect": ["identify", "metadata", "histogram", "dominant-colors"],
            "edit": ["auto_orient", "resize", "crop", "rotate", "flip", "trim", "pad", "grayscale", "invert", "gamma", "auto_contrast", "equalize", "threshold", "posterize", "solarize", "hue", "colorize", "opacity", "brightness", "contrast", "saturation", "blur", "sharpen"],
            "compose": ["overlay", "watermark", "montage"],
            "compare": ["mae", "rmse", "similarity", "differing-pixels", "visual-diff"],
        },
        "limits": limits,
        "isolation": {"network": "none", "filesystem": "read-only system plus per-call workspace"},
    }


def select_backend(preference: str, operation: str) -> str:
    magick_available = imagemagick() is not None
    pillow_available = pillow() is not None
    if operation in ("inspect", "compare") and pillow_available and preference != "imagemagick":
        return "pillow"
    if preference in ("auto", "imagemagick") and magick_available:
        return "imagemagick"
    if preference in ("auto", "pillow") and pillow_available:
        return "pillow"
    requested = preference if preference != "auto" else "ImageMagick or Pillow"
    raise ValueError(f"{requested} is not installed on this node")


def magick_argv(tool: str, arguments: list[str]) -> list[str]:
    engine = imagemagick()
    if engine is None:
        raise ValueError("ImageMagick is not installed on this node")
    if engine["version"] == 7:
        return [engine["command"], tool, *arguments] if tool != "convert" else [engine["command"], *arguments]
    command = engine["command"] if tool == "convert" else engine.get(tool) or shutil.which(tool)
    if not command:
        raise ValueError(f"ImageMagick {tool} is not installed on this node")
    return [command, *arguments]


def _run(argv: list[str], timeout_seconds: float, allow_metric_error: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/work",
            "TMPDIR": "/tmp",
            "MAGICK_TMPDIR": "/tmp",
            "MAGICK_THREAD_LIMIT": "1",
            "MAGICK_CONFIGURE_PATH": "/vanta-policy",
            "LC_ALL": "C.UTF-8",
        },
    )
    if result.returncode != 0 and not allow_metric_error:
        detail = (result.stderr or result.stdout).strip()[:2_000]
        raise ValueError(f"ImageMagick rejected the operation: {detail or f'exit {result.returncode}'}")
    return result


def _check_dimensions(width: int, height: int) -> None:
    if width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise ValueError(f"image dimensions exceed {MAX_DIMENSION} per side or {MAX_PIXELS} total pixels")


def _open_pillow(path: str):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    image = Image.open(path, formats=["PNG", "JPEG", "WEBP", "GIF"])
    image.seek(0)
    _check_dimensions(image.width, image.height)
    image.load()
    return image


def _safe_metadata(image: Any) -> dict[str, str]:
    metadata: dict[str, str] = {}
    try:
        from PIL import ExifTags
        for key, value in image.getexif().items():
            name = str(ExifTags.TAGS.get(key, key))
            rendered = str(value).replace("\x00", "")[:512]
            if name and rendered:
                metadata[name[:128]] = rendered
            if len(metadata) >= MAX_METADATA_ITEMS:
                break
    except (AttributeError, TypeError, ValueError):
        pass
    return metadata


def inspect_pillow(path: str, request: dict[str, Any]) -> dict[str, Any]:
    image = _open_pillow(path)
    result: dict[str, Any] = {
        "backend": "pillow",
        "format": str(image.format or detect_magic(path)).lower(),
        "mimeType": FORMAT_MIME[detect_magic(path)],
        "width": image.width,
        "height": image.height,
        "pixels": image.width * image.height,
        "mode": image.mode,
        "hasAlpha": "A" in image.getbands() or "transparency" in image.info,
        "frames": min(int(getattr(image, "n_frames", 1)), 65_535),
        "orientation": int(image.getexif().get(274, 1)) if hasattr(image, "getexif") else 1,
    }
    if request["includeMetadata"]:
        result["metadata"] = _safe_metadata(image)
    rgb = image.convert("RGB")
    if request["histogramBins"]:
        bins = request["histogramBins"]
        raw = rgb.histogram()
        channels = []
        for offset in (0, 256, 512):
            values = raw[offset : offset + 256]
            channels.append([sum(values[index * 256 // bins : (index + 1) * 256 // bins]) for index in range(bins)])
        result["histogram"] = {"bins": bins, "red": channels[0], "green": channels[1], "blue": channels[2]}
    if request["dominantColors"]:
        sample = rgb.copy()
        sample.thumbnail((256, 256))
        quantized = sample.quantize(colors=request["dominantColors"])
        palette = quantized.getpalette() or []
        colors = sorted(quantized.getcolors() or [], reverse=True)
        total = max(1, sample.width * sample.height)
        result["dominantColors"] = [
            {"hex": "#%02X%02X%02X" % tuple(palette[index * 3 : index * 3 + 3]), "fraction": round(count / total, 6)}
            for count, index in colors[: request["dominantColors"]]
        ]
    return result


def inspect_imagemagick(path: str, request: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    template = "%m\n%w\n%h\n%z\n%[colorspace]\n%[orientation]\n%[channels]\n%n"
    result = _run(magick_argv("identify", ["-ping", "-format", template, f"{path}[0]"]), timeout_seconds)
    lines = result.stdout.splitlines()
    if len(lines) < 8:
        raise ValueError("ImageMagick returned incomplete image metadata")
    width, height = int(lines[1]), int(lines[2])
    _check_dimensions(width, height)
    output: dict[str, Any] = {
        "backend": "imagemagick",
        "format": lines[0].lower().replace("jpg", "jpeg"),
        "mimeType": FORMAT_MIME[detect_magic(path)],
        "width": width,
        "height": height,
        "pixels": width * height,
        "depth": int(lines[3]),
        "colorspace": lines[4],
        "orientation": lines[5],
        "channels": lines[6],
        "frames": int(lines[7]),
    }
    if request["includeMetadata"]:
        metadata_result = _run(magick_argv("identify", ["-format", "%[EXIF:*]", f"{path}[0]"]), timeout_seconds)
        metadata = {}
        for line in metadata_result.stdout.splitlines()[:MAX_METADATA_ITEMS]:
            if "=" in line:
                key, value = line.split("=", 1)
                metadata[key[:128]] = value.replace("\x00", "")[:512]
        output["metadata"] = metadata
    if request["histogramBins"] or request["dominantColors"]:
        if pillow() is None:
            raise ValueError("histogram and dominant colors require python3-pil, which is not installed on this node")
        analytics = inspect_pillow(path, request)
        output.update({key: analytics[key] for key in ("histogram", "dominantColors") if key in analytics})
        output["backend"] = "imagemagick+pillow"
    return output


def inspect_image(path: str, request: dict[str, Any], preference: str, timeout_seconds: float) -> dict[str, Any]:
    verify_source(path, request.get("mimeType"))
    backend = select_backend(preference, "inspect")
    return inspect_imagemagick(path, request, timeout_seconds) if backend == "imagemagick" else inspect_pillow(path, request)


def _gravity(value: str) -> str:
    return {"center": "Center", "north": "North", "south": "South", "east": "East", "west": "West", "northwest": "NorthWest", "northeast": "NorthEast", "southwest": "SouthWest", "southeast": "SouthEast"}[value]


def edit_imagemagick(source: str, edits: list[dict[str, Any]], output: str, encoding: dict[str, Any], timeout_seconds: float) -> None:
    argv = [f"{source}[0]"]
    for edit in edits:
        operation = edit["operation"]
        if operation == "auto_orient":
            argv += ["-auto-orient"]
        elif operation == "resize":
            geometry = f"{edit['width']}x{edit['height']}"
            mode = edit.get("mode", "fit")
            if mode == "fit":
                argv += ["-resize", geometry + ">"]
            elif mode == "fill":
                argv += ["-resize", geometry + "^", "-gravity", "Center", "-extent", geometry]
            else:
                argv += ["-resize", geometry + "!"]
        elif operation == "crop":
            argv += ["-crop", f"{edit['width']}x{edit['height']}+{edit['x']}+{edit['y']}", "+repage"]
        elif operation == "rotate":
            argv += ["-background", edit.get("background", "#00000000"), "-rotate", str(edit["degrees"])]
        elif operation == "flip":
            argv += ["-flop" if edit["axis"] == "horizontal" else "-flip"]
        elif operation == "trim":
            argv += ["-fuzz", f"{edit.get('fuzzPercent', 0)}%", "-trim", "+repage"]
        elif operation == "pad":
            argv += ["-gravity", _gravity(edit.get("gravity", "center")), "-background", edit.get("background", "#00000000"), "-extent", f"{edit['width']}x{edit['height']}"]
        elif operation == "grayscale":
            argv += ["-colorspace", "Gray"]
        elif operation == "invert":
            argv += ["-channel", "RGB", "-negate", "+channel"]
        elif operation == "gamma":
            argv += ["-channel", "RGB", "-gamma", str(edit["gamma"]), "+channel"]
        elif operation == "auto_contrast":
            cutoff = edit.get("cutoffPercent", 0)
            argv += ["-channel", "RGB", "-contrast-stretch", f"{cutoff}%x{cutoff}%", "+channel"]
        elif operation == "equalize":
            argv += ["-channel", "RGB", "-equalize", "+channel"]
        elif operation == "threshold":
            argv += ["-colorspace", "Gray", "-channel", "Gray", "-threshold", f"{edit['thresholdPercent']}%", "+channel"]
        elif operation == "posterize":
            argv += ["+dither", "-posterize", str(edit["levels"])]
        elif operation == "solarize":
            argv += ["-channel", "RGB", "-solarize", f"{edit['thresholdPercent']}%", "+channel"]
        elif operation == "hue":
            argv += ["-modulate", f"100,100,{100 + edit['degrees'] / 1.8}"]
        elif operation == "colorize":
            argv += ["-channel", "RGB", "-fill", edit["color"], "-colorize", f"{edit['amount']}%", "+channel"]
        elif operation == "opacity":
            argv += ["-alpha", "set", "-channel", "A", "-evaluate", "multiply", str(edit["amount"] / 100), "+channel"]
        elif operation in ("brightness", "contrast"):
            value = edit["amount"]
            argv += ["-brightness-contrast", f"{value if operation == 'brightness' else 0}x{value if operation == 'contrast' else 0}"]
        elif operation == "saturation":
            argv += ["-modulate", f"100,{100 + edit['amount']},100"]
        elif operation == "blur":
            argv += ["-blur", f"0x{edit['radius']}"]
        elif operation == "sharpen":
            argv += ["-unsharp", f"0x1+{edit['amount']}+0"]
    if encoding["stripMetadata"]:
        argv += ["-strip"]
    argv += ["-quality", str(encoding["quality"]), f"{encoding['format']}:{output}"]
    _run(magick_argv("convert", argv), timeout_seconds)


def _trim_pillow(image: Any, fuzz_percent: float):
    from PIL import Image, ImageChops
    background = Image.new(image.mode, image.size, image.getpixel((0, 0)))
    difference = ImageChops.difference(image, background)
    if fuzz_percent:
        threshold = round(255 * fuzz_percent / 100)
        difference = difference.point(lambda value: 0 if value <= threshold else 255)
    box = difference.getbbox()
    return image.crop(box) if box else image


def _apply_rgb_pillow(image: Any, transform: Any):
    alpha = image.convert("RGBA").getchannel("A") if "A" in image.getbands() or "transparency" in image.info else None
    result = transform(image.convert("RGB"))
    if alpha is not None:
        result.putalpha(alpha)
    return result


def _apply_luma_pillow(image: Any, transform: Any):
    from PIL import ImageOps
    alpha = image.convert("RGBA").getchannel("A") if "A" in image.getbands() or "transparency" in image.info else None
    result = transform(ImageOps.grayscale(image))
    if alpha is not None:
        result.putalpha(alpha)
    return result


def _posterize_pillow(image: Any, levels: int):
    scale = levels - 1
    lookup = [round(round(value * scale / 255) * 255 / scale) for value in range(256)]
    return _apply_rgb_pillow(image, lambda colors: colors.point(lookup * 3))


def _shift_hue_pillow(image: Any, degrees: float):
    from PIL import Image
    def shift(colors: Any):
        hue, saturation, value = colors.convert("HSV").split()
        offset = round(degrees * 255 / 360)
        hue = hue.point(lambda channel: (channel + offset) % 256)
        return Image.merge("HSV", (hue, saturation, value)).convert("RGB")
    return _apply_rgb_pillow(image, shift)


def _save_pillow(image: Any, output: str, encoding: dict[str, Any], source_info: dict[str, Any] | None = None) -> None:
    image_format = encoding["format"]
    options: dict[str, Any] = {}
    if image_format in ("jpeg", "webp"):
        options["quality"] = encoding["quality"]
    if image_format == "png":
        options["compress_level"] = max(0, min(9, round((100 - encoding["quality"]) * 9 / 99)))
    if not encoding["stripMetadata"] and source_info:
        for key in ("exif", "icc_profile"):
            if key in source_info:
                options[key] = source_info[key]
    if image_format == "jpeg" and image.mode not in ("RGB", "L"):
        background = __import__("PIL.Image", fromlist=["Image"]).new("RGB", image.size, "white")
        if "A" in image.getbands():
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image)
        image = background
    image.save(output, format=image_format.upper(), **options)


def edit_pillow(source: str, edits: list[dict[str, Any]], output: str, encoding: dict[str, Any]) -> None:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    image = _open_pillow(source)
    source_info = dict(image.info)
    for edit in edits:
        operation = edit["operation"]
        if operation == "auto_orient":
            image = ImageOps.exif_transpose(image)
        elif operation == "resize":
            size = (edit["width"], edit["height"])
            mode = edit.get("mode", "fit")
            if mode == "fill":
                image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
            elif mode == "stretch":
                image = image.resize(size, Image.Resampling.LANCZOS)
            else:
                image = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
        elif operation == "crop":
            box = (edit["x"], edit["y"], edit["x"] + edit["width"], edit["y"] + edit["height"])
            if box[2] > image.width or box[3] > image.height:
                raise ValueError("crop rectangle extends outside the image")
            image = image.crop(box)
        elif operation == "rotate":
            image = image.rotate(-edit["degrees"], resample=Image.Resampling.BICUBIC, expand=True, fillcolor=edit.get("background", "#00000000"))
        elif operation == "flip":
            image = ImageOps.mirror(image) if edit["axis"] == "horizontal" else ImageOps.flip(image)
        elif operation == "trim":
            image = _trim_pillow(image, edit.get("fuzzPercent", 0))
        elif operation == "pad":
            if edit["width"] < image.width or edit["height"] < image.height:
                raise ValueError("pad dimensions cannot be smaller than the image")
            image = ImageOps.pad(image, (edit["width"], edit["height"]), color=edit.get("background", "#00000000"), centering=(0.5, 0.5))
        elif operation == "grayscale":
            image = _apply_luma_pillow(image, lambda grayscale: grayscale)
        elif operation == "invert":
            image = _apply_rgb_pillow(image, ImageOps.invert)
        elif operation == "gamma":
            gamma = edit["gamma"]
            lookup = [round(255 * ((value / 255) ** (1 / gamma))) for value in range(256)]
            image = _apply_rgb_pillow(image, lambda colors: colors.point(lookup * 3))
        elif operation == "auto_contrast":
            image = _apply_rgb_pillow(image, lambda colors: ImageOps.autocontrast(colors, cutoff=edit.get("cutoffPercent", 0)))
        elif operation == "equalize":
            image = _apply_rgb_pillow(image, ImageOps.equalize)
        elif operation == "threshold":
            threshold = round(edit["thresholdPercent"] * 255 / 100)
            image = _apply_luma_pillow(image, lambda grayscale: grayscale.point(lambda value: 255 if value > threshold else 0))
        elif operation == "posterize":
            image = _posterize_pillow(image, edit["levels"])
        elif operation == "solarize":
            threshold = round(edit["thresholdPercent"] * 255 / 100)
            image = _apply_rgb_pillow(image, lambda colors: ImageOps.solarize(colors, threshold=threshold))
        elif operation == "hue":
            image = _shift_hue_pillow(image, edit["degrees"])
        elif operation == "colorize":
            amount = edit["amount"] / 100
            image = _apply_rgb_pillow(image, lambda colors: Image.blend(colors, Image.new("RGB", colors.size, edit["color"]), amount))
        elif operation == "opacity":
            image = image.convert("RGBA")
            image.putalpha(image.getchannel("A").point(lambda value: round(value * edit["amount"] / 100)))
        elif operation == "brightness":
            image = ImageEnhance.Brightness(image).enhance((100 + edit["amount"]) / 100)
        elif operation == "contrast":
            image = ImageEnhance.Contrast(image).enhance((100 + edit["amount"]) / 100)
        elif operation == "saturation":
            image = ImageEnhance.Color(image).enhance((100 + edit["amount"]) / 100)
        elif operation == "blur":
            image = image.filter(ImageFilter.GaussianBlur(edit["radius"]))
        elif operation == "sharpen":
            image = image.filter(ImageFilter.UnsharpMask(radius=1, percent=round(edit["amount"] * 100), threshold=0))
        _check_dimensions(image.width, image.height)
    _save_pillow(image, output, encoding, source_info)


def edit_image(source: str, edits: list[dict[str, Any]], output: str, encoding: dict[str, Any], preference: str, timeout_seconds: float) -> str:
    verify_source(source)
    backend = select_backend(preference, "edit")
    if backend == "imagemagick":
        edit_imagemagick(source, edits, output, encoding, timeout_seconds)
    else:
        edit_pillow(source, edits, output, encoding)
    verify_source(output, FORMAT_MIME[encoding["format"]])
    return backend


def compose_pillow(request: dict[str, Any], sources: list[str], output: str) -> None:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    operation = request["operation"]
    if operation == "overlay":
        base = _open_pillow(sources[0]).convert("RGBA")
        layer = _open_pillow(sources[1]).convert("RGBA")
        if request["opacity"] < 100:
            layer.putalpha(layer.getchannel("A").point(lambda value: round(value * request["opacity"] / 100)))
        position = _position(base.size, layer.size, request["position"], 16)
        base.alpha_composite(layer, position)
        result = base
    elif operation == "watermark":
        result = _open_pillow(sources[0]).convert("RGBA")
        font = ImageFont.truetype(FONT_PATH, request["fontSize"])
        draw = ImageDraw.Draw(result)
        box = draw.textbbox((0, 0), request["text"], font=font)
        size = (box[2] - box[0], box[3] - box[1])
        draw.text(_position(result.size, size, request["position"], 16), request["text"], fill=request["color"], font=font)
    else:
        cells = [ImageOps.pad(_open_pillow(path).convert("RGBA"), (request["cellWidth"], request["cellHeight"]), color=request["background"]) for path in sources]
        columns = min(request["columns"], len(cells))
        rows = math.ceil(len(cells) / columns)
        result = Image.new("RGBA", (columns * request["cellWidth"], rows * request["cellHeight"]), request["background"])
        for index, cell in enumerate(cells):
            result.alpha_composite(cell, ((index % columns) * request["cellWidth"], (index // columns) * request["cellHeight"]))
    _check_dimensions(result.width, result.height)
    _save_pillow(result, output, request["output"])


def _position(base: tuple[int, int], item: tuple[int, int], position: str, margin: int) -> tuple[int, int]:
    horizontal = {"northwest": margin, "southwest": margin, "center": (base[0] - item[0]) // 2, "northeast": base[0] - item[0] - margin, "southeast": base[0] - item[0] - margin}
    vertical = {"northwest": margin, "northeast": margin, "center": (base[1] - item[1]) // 2, "southwest": base[1] - item[1] - margin, "southeast": base[1] - item[1] - margin}
    return max(0, horizontal[position]), max(0, vertical[position])


def compose_imagemagick(request: dict[str, Any], sources: list[str], output: str, timeout_seconds: float) -> None:
    encoding = request["output"]
    operation = request["operation"]
    if operation == "overlay":
        overlay_args = [f"{sources[0]}[0]", "(", f"{sources[1]}[0]"]
        if request["opacity"] < 100:
            overlay_args += ["-channel", "A", "-evaluate", "multiply", str(request["opacity"] / 100), "+channel"]
        overlay_args += [")", "-gravity", _gravity(request["position"]), "-geometry", "+16+16", "-composite"]
        argv = overlay_args
        tool = "convert"
    elif operation == "watermark":
        argv = [f"{sources[0]}[0]", "-gravity", _gravity(request["position"]), "-font", FONT_PATH, "-pointsize", str(request["fontSize"]), "-fill", request["color"], "-annotate", "+16+16", request["text"]]
        tool = "convert"
    else:
        width, height = request["cellWidth"], request["cellHeight"]
        columns = min(request["columns"], len(sources))
        rows = math.ceil(len(sources) / columns)
        argv = []
        for row_start in range(0, len(sources), columns):
            argv.append("(")
            for path in sources[row_start:row_start + columns]:
                argv += ["(", f"{path}[0]", "-thumbnail", f"{width}x{height}", "-gravity", "center", "-background", request["background"], "-extent", f"{width}x{height}", ")"]
            argv += ["+append", ")"]
        argv += ["-append", "-gravity", "northwest", "-background", request["background"], "-extent", f"{columns * width}x{rows * height}"]
        tool = "convert"
    if encoding["stripMetadata"]:
        argv += ["-strip"]
    argv += ["-quality", str(encoding["quality"]), f"{encoding['format']}:{output}"]
    _run(magick_argv(tool, argv), timeout_seconds)


def compose_image(request: dict[str, Any], sources: list[str], output: str, preference: str, timeout_seconds: float) -> str:
    for source in sources:
        verify_source(source)
    backend = "pillow" if request["operation"] == "montage" and pillow() is not None else select_backend(preference, "compose")
    if backend == "imagemagick":
        compose_imagemagick(request, sources, output, timeout_seconds)
    else:
        compose_pillow(request, sources, output)
    verify_source(output, FORMAT_MIME[request["output"]["format"]])
    return backend


def compare_images(source: str, reference: str, request: dict[str, Any], output: str | None) -> dict[str, Any]:
    from PIL import Image, ImageChops, ImageStat
    first = _open_pillow(source).convert("RGBA")
    second = _open_pillow(reference).convert("RGBA")
    if first.size != second.size:
        if request["normalize"] != "fit":
            raise ValueError("images have different dimensions; set normalize to fit to compare them")
        second = second.resize(first.size, Image.Resampling.LANCZOS)
    difference = ImageChops.difference(first, second)
    histogram = difference.histogram()
    channel_pixels = first.width * first.height * 4
    absolute = sum((index % 256) * count for index, count in enumerate(histogram))
    squared = sum(((index % 256) ** 2) * count for index, count in enumerate(histogram))
    extrema = difference.getextrema()
    differing = sum(1 for pixel in difference.getdata() if any(pixel))
    mae = absolute / max(1, channel_pixels * 255)
    rmse = math.sqrt(squared / max(1, channel_pixels)) / 255
    result: dict[str, Any] = {
        "backend": "pillow",
        "width": first.width,
        "height": first.height,
        "differingPixels": differing,
        "different": any(high > 0 for _low, high in extrema),
        "mae": round(mae, 8),
        "rmse": round(rmse, 8),
        "similarity": round(max(0.0, 1.0 - rmse), 8),
    }
    if output:
        enhanced = difference.convert("RGB").point(lambda value: min(255, value * 4))
        _save_pillow(enhanced, output, request["diffOutput"])
    return result


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        png_path = os.path.join(directory, "input.png")
        svg_path = os.path.join(directory, "input.svg")
        with open(png_path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\nrest")
        with open(svg_path, "wb") as handle:
            handle.write(b"<svg/>")
        assert detect_magic(png_path) == "png"
        try:
            detect_magic(svg_path)
        except ValueError:
            pass
        else:
            raise AssertionError("SVG input was accepted")
    original = imagemagick
    original_run = _run
    try:
        globals()["imagemagick"] = lambda: {"version": 7, "command": "/usr/bin/magick"}
        assert magick_argv("convert", ["in", "out"]) == ["/usr/bin/magick", "in", "out"]
        assert magick_argv("identify", ["in"]) == ["/usr/bin/magick", "identify", "in"]
        globals()["imagemagick"] = lambda: {"version": 6, "command": "/usr/bin/convert", "identify": "/usr/bin/identify"}
        assert magick_argv("convert", ["in", "out"])[0] == "/usr/bin/convert"
        assert magick_argv("identify", ["in"])[0] == "/usr/bin/identify"
        captured: list[str] = []
        globals()["_run"] = lambda argv, _timeout: captured.extend(argv)
        edit_imagemagick("in.png", [{"operation": "invert"}], "out.png", {"format": "png", "quality": 85, "stripMetadata": True}, 10)
        assert captured[2:6] == ["-channel", "RGB", "-negate", "+channel"]
    finally:
        globals()["imagemagick"] = original
        globals()["_run"] = original_run
    assert _position((100, 100), (20, 10), "southeast", 5) == (75, 85)


if __name__ == "__main__":
    self_test()
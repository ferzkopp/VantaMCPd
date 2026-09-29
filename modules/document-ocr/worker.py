#!/usr/bin/env python3
import json
import sys

MAX_PIXELS = 16_000_000
MAX_PAGES = 20
MAX_ERROR = 500


def page_images(source, requested):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with open(source, "rb") as handle:
        header = handle.read(16)
    if header.startswith(b"%PDF-"):
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(source)
        try:
            if len(document) > MAX_PAGES:
                raise ValueError("PDF exceeds the page limit")
            pages = requested or list(range(1, len(document) + 1))
            if not pages or any(page > len(document) for page in pages):
                raise ValueError("requested page is outside the PDF")
            for number in pages:
                page = document[number - 1]
                try:
                    width, height = page.get_size()
                    scale = 150 / 72
                    if width * height * scale * scale > MAX_PIXELS:
                        raise ValueError("rendered PDF page exceeds the pixel limit")
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        yield number, image
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        finally:
            document.close()
    else:
        if requested and requested != [1]:
            raise ValueError("images have only page 1")
        with Image.open(source) as original:
            if original.format not in ("PNG", "JPEG", "WEBP") or original.width * original.height > MAX_PIXELS:
                raise ValueError("unsupported or oversized image")
            image = ImageOps.exif_transpose(original).convert("RGB")
        yield 1, image


def create_engine(language="auto"):
    import paddle
    from paddleocr import PaddleOCR

    if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
        raise RuntimeError("CUDA inference is unavailable")
    return PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="en_PP-OCRv4_mobile_rec" if language == "en" else "PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="gpu:0",
    )
def extract(source, requested, language="auto"):
    import numpy as np

    engine = create_engine(language)

    pages = []
    for number, image in page_images(source, requested):
        try:
            prediction = list(engine.predict(np.asarray(image)))
            if len(prediction) != 1:
                raise ValueError("OCR returned an unexpected number of pages")
            result = prediction[0].json["res"]
            lines = []
            for text, score, polygon in zip(result["rec_texts"], result["rec_scores"], result["rec_polys"]):
                if text:
                    lines.append({"text": str(text), "confidence": round(float(score), 4),
                                  "polygon": [[int(point[0]), int(point[1])] for point in polygon]})
            pages.append({"page": number, "width": image.width, "height": image.height,
                          "text": "\n".join(line["text"] for line in lines), "lines": lines})
        finally:
            image.close()
    return {"pages": pages, "pageCount": len(pages),
            "model": "PP-OCRv4-English-mobile" if language == "en" else "PP-OCRv5-mobile"}


def main():
    with open("/work/request.json", encoding="utf-8") as handle:
        request = json.load(handle)
    try:
        result = extract("/inputs/document", request["pages"], request["language"])
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else f"{type(error).__name__}: {error}"
        with open("/work/error.json", "x", encoding="utf-8") as handle:
            json.dump({"error": message[:MAX_ERROR]}, handle, ensure_ascii=False)
        raise
    with open("/work/result.json", "x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        assert MAX_PIXELS > 0 and MAX_PAGES > 0
    elif sys.argv[1:] == ["--warmup"]:
        import numpy as np
        from PIL import Image, ImageDraw

        sample = Image.new("RGB", (640, 120), "white")
        ImageDraw.Draw(sample).text((20, 30), "DOCUMENT OCR", fill="black", stroke_width=1)
        for language in ("auto", "en"):
            assert len(list(create_engine(language).predict(np.asarray(sample)))) == 1
    else:
        main()
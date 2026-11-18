"""
Decode uploaded receipts in memory (never written to disk).

Images: JPEG/PNG/WebP/GIF/TIFF via Pillow, HEIC/HEIF via pillow-heif.
PDFs: the embedded text layer is used when present (exact, no OCR); the
first page is also rendered for OCR / vision models.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

MAX_PIXELS = 40_000_000          # decompression-bomb guard (~ 8000 × 5000)
PDF_RENDER_SCALE = 3.0           # 216 dpi: good OCR accuracy without huge bitmaps
MIN_PDF_TEXT_CHARS = 40

SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "application/pdf": (b"%PDF-",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/tiff": (b"II*\x00", b"MM\x00*"),
}
ALLOWED_TYPES = frozenset({*SIGNATURES, "image/webp", "image/heic", "image/heif"})


class DocumentError(ValueError):
    pass


@dataclass
class Document:
    image: Any | None          # PIL.Image (RGB) or None
    text: str | None           # PDF text layer, if any
    pages: int = 1


def check_signature(content: bytes, mime: str) -> None:
    if mime not in ALLOWED_TYPES:
        raise DocumentError(f"Unsupported type {mime}")
    if mime == "image/webp":
        ok = content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    elif mime in ("image/heic", "image/heif"):
        ok = content[4:8] == b"ftyp"
    else:
        ok = any(content.startswith(sig) for sig in SIGNATURES[mime])
    if not ok:
        raise DocumentError(f"Body is not a valid {mime} file")


def load_document(content: bytes, mime: str) -> Document:
    check_signature(content, mime)
    if mime == "application/pdf":
        return _load_pdf(content)
    return Document(image=_load_image(content, mime), text=None)


def _load_image(content: bytes, mime: str) -> Any:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    if mime in ("image/heic", "image/heif"):
        try:
            from pillow_heif import register_heif_opener
        except ImportError as exc:  # pragma: no cover - installed in the Docker image
            raise DocumentError("HEIC support is not installed on this server") from exc
        register_heif_opener()
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except Image.DecompressionBombError as exc:
        raise DocumentError("Image is too large") from exc
    except Exception as exc:
        raise DocumentError("Couldn't decode the image") from exc
    img = ImageOps.exif_transpose(img)            # phone photos are often stored sideways
    return img.convert("RGB")


def _load_pdf(content: bytes) -> Document:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - installed in the Docker image
        raise DocumentError("PDF support is not installed on this server") from exc
    try:
        pdf = pdfium.PdfDocument(content)
    except Exception as exc:
        raise DocumentError("Couldn't open the PDF") from exc
    try:
        if len(pdf) == 0:
            raise DocumentError("PDF has no pages")
        page = pdf[0]
        text = page.get_textpage().get_text_bounded() or ""
        image = page.render(scale=PDF_RENDER_SCALE).to_pil().convert("RGB")
        return Document(image=image, text=text if len(text.strip()) >= MIN_PDF_TEXT_CHARS else None, pages=len(pdf))
    finally:
        pdf.close()


def prepare_for_ocr(image: Any) -> Any:
    """Grayscale, upscale small photos, stretch contrast: cheap wins for Tesseract."""
    from PIL import ImageOps

    gray = ImageOps.grayscale(image)
    if max(gray.size) < 1600:
        gray = gray.resize((gray.width * 2, gray.height * 2))
    return ImageOps.autocontrast(gray, cutoff=1)


def prepare_for_vlm(image: Any, max_side: int = 1280) -> Any:
    """Vision models' cost grows with pixels; receipts stay legible at ~1280 px."""
    if max(image.size) <= max_side:
        return image
    scale = max_side / max(image.size)
    return image.resize((round(image.width * scale), round(image.height * scale)))

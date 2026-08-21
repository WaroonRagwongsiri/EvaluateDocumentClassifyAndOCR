"""PDF/image loading, encoding, rotation, and bbox-drawing helpers."""
import base64
import io
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw

SAMPLE_EXTENSIONS = ("*.pdf", "*.png", "*.jpg", "*.jpeg")


def list_sample_files(sample_dir: str = "sample_data") -> list[Path]:
    """List PDFs/images sitting in sample_dir, sorted by name."""
    base = Path(sample_dir)
    if not base.is_dir():
        return []
    files: list[Path] = []
    for pattern in SAMPLE_EXTENSIONS:
        files.extend(base.glob(pattern))
    return sorted(files, key=lambda p: p.name.lower())


def load_pages(file_bytes: bytes, filename: str) -> list[Image.Image]:
    """Return a list of PIL images: one per PDF page, or a single image."""
    if filename.lower().endswith(".pdf"):
        pages = []
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(img)
        doc.close()
        return pages
    return [Image.open(io.BytesIO(file_bytes)).convert("RGB")]


def make_thumbnail(img: Image.Image, max_dim: int = 220) -> Image.Image:
    """Downscaled copy for grid previews, so full-res pages aren't shipped to the browser."""
    thumb = img.copy()
    thumb.thumbnail((max_dim, max_dim))
    return thumb


def image_to_b64(img: Image.Image) -> str:
    """PNG-encode and base64-encode an image (no data-URI prefix — Ollama wants raw base64)."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def rotate_image(img: Image.Image, degrees: int) -> Image.Image:
    """Rotate so content becomes upright, given the clockwise correction the model reported."""
    if degrees % 360 == 0:
        return img.copy()
    return img.rotate(-degrees, expand=True)


def draw_bboxes(img: Image.Image, boxes: list[dict]) -> Image.Image:
    """Draw 0-1000-normalized bbox_2d boxes (scaled to actual pixel size) with labels."""
    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)
    w, h = annotated.size
    for box in boxes:
        coords = box.get("bbox_2d")
        if not coords or len(coords) != 4:
            continue
        x1, y1, x2, y2 = coords
        px1, py1 = x1 / 1000 * w, y1 / 1000 * h
        px2, py2 = x2 / 1000 * w, y2 / 1000 * h
        draw.rectangle([px1, py1, px2, py2], outline="red", width=3)
        label = box.get("label", "signature")
        draw.text((px1, max(py1 - 14, 0)), label, fill="red")
    return annotated

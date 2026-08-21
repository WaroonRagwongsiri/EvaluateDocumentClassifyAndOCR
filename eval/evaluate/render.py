"""On-demand page-PNG rendering + disk cache.

Pages are rendered @150dpi (matching the worker's OCR resolution) and cached at
<CACHE_DIR>/<sha256>/page_<n>.png. The path is derivable, so it is never stored in
the DB. `render_page` is idempotent: it returns the existing PNG if present,
otherwise renders and writes it.

For a multi-page PDF we render only the requested page (cheap), not the whole
document. For a single image, the page PNG is just the image re-saved as PNG.
"""
from __future__ import annotations

import logging
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from . import config
from .db import connect

log = logging.getLogger("eval.render")


def _cache_path(sha256: str, page_no: int) -> Path:
    return config.CACHE_DIR / sha256 / f"page_{page_no}.png"


def _lookup_file(sha256: str) -> tuple[str, str] | None:
    """Return (local_path, content_kind) for sha256, or None if the file is gone."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT local_path, content_kind FROM files WHERE sha256 = %s", (sha256,))
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _render_pdf_page(local_path: str, page_no: int, out: Path) -> None:
    """Render one page (1-based) of a PDF to `out` as PNG @150dpi."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(local_path) as doc:
        page = doc.load_page(page_no - 1)  # 0-based
        pix = page.get_pixmap(dpi=150)
        pix.save(str(out))


def _render_image_page(local_path: str, out: Path) -> None:
    """Normalize a single image to a PNG page (page_no is always 1 for images)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.open(local_path).convert("RGB").save(str(out), format="PNG")


def render_page(sha256: str, page_no: int) -> Path:
    """Ensure the cached page PNG exists and return its path.

    Raises FileNotFoundError if the sha256 is not in `files`. Raises ValueError
    if the file is content_kind='other' (not renderable). A corrupt/missing
    on-disk source raises its underlying OSError — the server's page route maps
    those to a 404/500.
    """
    out = _cache_path(sha256, page_no)
    if out.exists():
        return out

    info = _lookup_file(sha256)
    if info is None:
        raise FileNotFoundError(f"no files row for sha256={sha256}")
    local_path, content_kind = info
    if content_kind == "pdf":
        _render_pdf_page(local_path, page_no, out)
    elif content_kind == "image":
        _render_image_page(local_path, out)
    else:
        raise ValueError(f"sha256={sha256} is content_kind={content_kind!r}, not renderable")
    return out

"""Turning scans into pages: previews, splitting a stack, merging a document."""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any


def normalise_rotation(degrees: Any) -> int:
    try:
        return int(degrees) % 360 // 90 * 90
    except (TypeError, ValueError):
        return 0


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def page_count(path: Path) -> int:
    """How many pages a scan holds. Images are always one.

    A file that cannot be opened counts as one page rather than raising: the
    scan itself succeeded, and losing it over an unreadable page count would be
    the worse outcome.
    """
    if not is_pdf(path):
        return 1
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(str(path))
        try:
            return len(document)
        finally:
            document.close()
    except Exception:
        return 1


def split_pages(path: Path, target_dir: Path) -> list[Path]:
    """One file per page, so a stack from the feeder can be handled sheet by sheet.

    Many scanners answer a whole feeder run with a single multi-page PDF. Left
    as it is, five sheets would show up as one tile that cannot be reordered,
    rotated or dropped individually — so the document is taken apart here.
    Single-page scans are handed back untouched, without a needless rewrite.
    """
    if not is_pdf(path) or page_count(path) <= 1:
        return [path]

    import pypdfium2

    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        source = pypdfium2.PdfDocument(str(path))
        try:
            for index in range(len(source)):
                single = pypdfium2.PdfDocument.new()
                try:
                    single.import_pages(source, [index])
                    target = target_dir / f"{path.stem}_seite-{index + 1:02d}.pdf"
                    single.save(str(target))
                    written.append(target)
                finally:
                    single.close()
        finally:
            source.close()
    except Exception:
        # Rather one tile that works than a lost scan: hand back the original
        # and let the user split it by hand.
        for partial in written:
            partial.unlink(missing_ok=True)
        return [path]
    path.unlink(missing_ok=True)
    return written


def as_pdf_document(path: Path, rotation: int = 0) -> Any:
    """Every page becomes a PDF so the merge only has to deal with one format."""
    import pypdfium2
    from PIL import Image

    rotation = normalise_rotation(rotation)
    if is_pdf(path):
        document = pypdfium2.PdfDocument(str(path))
        if rotation:
            for index in range(len(document)):
                page = document[index]
                page.set_rotation((page.get_rotation() + rotation) % 360)
        return document

    image = Image.open(path).convert("RGB")
    if rotation:
        # PIL turns counter-clockwise, the UI promises clockwise.
        image = image.rotate(-rotation, expand=True)
    buffer = io.BytesIO()
    image.save(buffer, "PDF", resolution=200.0)
    return pypdfium2.PdfDocument(buffer.getvalue())


def merge_pages(pages: list[dict[str, Any]], target: Path) -> int:
    """Combine the collected pages into a single PDF. Returns the page count."""
    import pypdfium2

    merged = pypdfium2.PdfDocument.new()
    sources = []
    try:
        for page in pages:
            document = as_pdf_document(Path(page["path"]), page.get("rotation", 0))
            sources.append(document)
            merged.import_pages(document)
        target.parent.mkdir(parents=True, exist_ok=True)
        merged.save(str(target))
        return len(merged)
    finally:
        for document in sources:
            document.close()
        merged.close()


def merge_files(paths: list[Path], target: Path) -> int:
    """Merge whole files, used when the feeder answers sheet by sheet."""
    count = merge_pages([{"path": str(path), "rotation": 0} for path in paths], target)
    for path in paths:
        if path != target:
            path.unlink(missing_ok=True)
    return count


class PreviewCache:
    """The most recently rendered preview, so reopening the viewer is instant.

    One slot is enough — the interface only ever shows one page at a time — but
    it is read and written by several request threads, so key and payload have
    to be swapped together or a second viewer can be served someone else's page.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[str, int] | None = None
        self._payload: tuple[bytes, str] | None = None

    def get(self, key: tuple[str, int]) -> tuple[bytes, str] | None:
        with self._lock:
            return self._payload if self._key == key else None

    def put(self, key: tuple[str, int], payload: tuple[bytes, str]) -> None:
        with self._lock:
            self._key, self._payload = key, payload

    def clear(self) -> None:
        with self._lock:
            self._key, self._payload = None, None


preview_cache = PreviewCache()


def render_preview(file_path: Path, rotation: int = 0) -> tuple[bytes, str]:
    """Return displayable image bytes for a scan (first page for PDFs)."""
    rotation = normalise_rotation(rotation)
    cache_key = (str(file_path), rotation)
    cached = preview_cache.get(cache_key)
    if cached:
        return cached

    if not is_pdf(file_path):
        if not rotation:
            result = (file_path.read_bytes(), "image/jpeg")
        else:
            from PIL import Image

            buffer = io.BytesIO()
            Image.open(file_path).convert("RGB").rotate(-rotation, expand=True).save(buffer, "JPEG", quality=88)
            result = (buffer.getvalue(), "image/jpeg")
        preview_cache.put(cache_key, result)
        return result

    import pypdfium2  # optional: only needed to rasterise PDF previews

    pdf = pypdfium2.PdfDocument(str(file_path))
    try:
        page = pdf[0]
        if rotation:
            page.set_rotation((page.get_rotation() + rotation) % 360)
        bitmap = page.render(scale=1.4)
        buffer = io.BytesIO()
        bitmap.to_pil().save(buffer, format="PNG", optimize=True)
    finally:
        pdf.close()
    result = (buffer.getvalue(), "image/png")
    preview_cache.put(cache_key, result)
    return result


def batch_filename(suffix: str) -> str:
    """A name that keeps the pages of one feeder run in scanning order."""
    return f"page_{time.time_ns() // 1000}{suffix.lower()}"

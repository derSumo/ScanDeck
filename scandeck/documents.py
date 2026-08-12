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


# --------------------------------------------------------------------------- #
# Looking at what was actually scanned
# --------------------------------------------------------------------------- #

# A scan is never pure white: dust, paper texture and the lid's shadow all leave
# marks. So "blank" means "almost nothing but background", not "nothing".
BLANK_INK_SHARE = 0.004  # 0.4 % of the inspected pixels
BLANK_DARK_LEVEL = 205   # below this counts as ink, on a 0-255 grey scale
EDGE_MARGIN = 0.04       # ignore the outer 4 %: scanner edges are rarely clean


def _grey_preview(path: Path, rotation: int = 0, width: int = 700) -> Any:
    """A small greyscale copy — enough to judge a page, cheap to work with."""
    from PIL import Image

    if is_pdf(path):
        import pypdfium2

        pdf = pypdfium2.PdfDocument(str(path))
        try:
            page = pdf[0]
            if rotation:
                page.set_rotation((page.get_rotation() + normalise_rotation(rotation)) % 360)
            scale = min(2.0, max(0.3, width / max(1.0, page.get_size()[0])))
            image = page.render(scale=scale, grayscale=True).to_pil()
        finally:
            pdf.close()
    else:
        image = Image.open(path)
        if rotation:
            image = image.rotate(-normalise_rotation(rotation), expand=True)
    return image.convert("L")


def ink_share(path: Path, rotation: int = 0) -> float:
    """How much of the page carries ink, between 0 and 1."""
    image = _grey_preview(path, rotation)
    width, height = image.size
    margin_x, margin_y = int(width * EDGE_MARGIN), int(height * EDGE_MARGIN)
    inner = image.crop((margin_x, margin_y, width - margin_x, height - margin_y))
    histogram = inner.histogram()
    total = sum(histogram) or 1
    return sum(histogram[:BLANK_DARK_LEVEL]) / total


def looks_blank(path: Path, rotation: int = 0) -> bool:
    """Is this the back of a sheet nobody printed on?

    Duplex and feeder runs produce these by the dozen; keeping them means a
    ten page document where five pages say nothing.
    """
    try:
        return ink_share(path, rotation) < BLANK_INK_SHARE
    except Exception:
        return False  # unreadable is not the same as empty


def find_skew(path: Path, limit: float = 6.0, step: float = 0.25) -> float:
    """The angle this page is rotated by, in degrees, or 0.0 if it looks straight.

    Uses the classic projection profile: rotate a small copy through a range of
    angles and keep the one where the rows differ most from one another. Text
    lines line up exactly at that angle, which makes the row sums spiky.
    """
    try:
        image = _grey_preview(path, width=800)
    except Exception:
        return 0.0
    from PIL import Image

    # Work on ink-as-white so empty rows really are zero.
    binary = image.point(lambda value: 255 if value < BLANK_DARK_LEVEL else 0)
    if not sum(binary.histogram()[1:]):
        return 0.0  # nothing on the page to align

    def sharpness(angle: float) -> float:
        turned = binary if angle == 0 else binary.rotate(angle, resample=Image.BILINEAR, fillcolor=0)
        # Squeezing the page to a single column averages every row in one go —
        # summing row by row in Python would mean millions of pixel reads.
        rows = turned.resize((1, turned.height), Image.BILINEAR).tobytes()
        if not rows:
            return 0.0
        mean = sum(rows) / len(rows)
        return sum((value - mean) ** 2 for value in rows)

    # Coarse pass first, then refine around the winner: the fine pass alone
    # would mean hundreds of rotations of the whole image.
    coarse = max((a / 2 for a in range(int(-limit * 2), int(limit * 2) + 1)), key=sharpness)
    fine_range = [coarse + offset * step for offset in range(-2, 3)]
    best = max(fine_range, key=sharpness)
    return 0.0 if abs(best) < step else round(best, 2)


# Below this the correction is not worth re-encoding the page for.
MIN_SKEW = 0.4


def straighten(path: Path, dpi: int = 300) -> float:
    """Rotate a crooked page upright in place. Returns the angle applied.

    A PDF page has to be rasterised and rebuilt for this — its content cannot be
    turned by a fraction of a degree otherwise. That is acceptable here because
    these pages are photographs of paper anyway, with no text layer to lose;
    Paperless does the text recognition afterwards, and it reads straight lines
    considerably better.
    """
    angle = find_skew(path)
    if abs(angle) < MIN_SKEW:
        return 0.0

    from PIL import Image

    try:
        if is_pdf(path):
            import pypdfium2

            pdf = pypdfium2.PdfDocument(str(path))
            try:
                scale = max(1.0, min(6.0, dpi / 72))
                pages = [pdf[index].render(scale=scale).to_pil().convert("RGB")
                         for index in range(len(pdf))]
            finally:
                pdf.close()
            turned = [page.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
                      for page in pages]
            buffer = io.BytesIO()
            turned[0].save(buffer, "PDF", save_all=True, append_images=turned[1:], resolution=float(dpi))
            path.write_bytes(buffer.getvalue())
        else:
            image = Image.open(path).convert("RGB")
            image.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255)).save(
                path, "JPEG", quality=92
            )
    except Exception:
        return 0.0  # a page that cannot be rebuilt stays as it was
    return angle


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

"""Judging a scanned page: is it blank, and is it straight?"""

from __future__ import annotations

import pytest

pypdfium2 = pytest.importorskip("pypdfium2")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")


def page(tmp_path, name, lines=0, angle=0.0, speckles=0, fmt="PDF"):
    """A sheet of paper with a given number of text lines, optionally crooked."""
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    for index in range(lines):
        top = 200 + index * 70
        draw.rectangle([180, top, 1000, top + 22], fill=(20, 20, 20))
    for index in range(speckles):  # scanner dust and lid shadow
        draw.point(((index * 3) % 1240, (index * 7) % 1754), fill=(205, 205, 205))
    if angle:
        image = image.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    path = tmp_path / name
    if fmt == "PDF":
        image.save(path, "PDF", resolution=150.0)
    else:
        image.save(path, "JPEG", quality=92)
    return path


# --- blank pages ----------------------------------------------------------- #

def test_an_empty_sheet_is_recognised(deck, tmp_path):
    from scandeck.documents import looks_blank

    assert looks_blank(page(tmp_path, "leer.pdf")) is True


def test_dust_and_shadows_do_not_make_a_page_look_printed(deck, tmp_path):
    """A scan is never pure white; the threshold has to survive that."""
    from scandeck.documents import looks_blank

    assert looks_blank(page(tmp_path, "staub.pdf", speckles=600)) is True


def test_even_a_single_line_counts_as_printed(deck, tmp_path):
    """Losing a page that only carries one sentence would be unforgivable."""
    from scandeck.documents import looks_blank

    assert looks_blank(page(tmp_path, "wenig.pdf", lines=1)) is False


def test_a_full_page_is_never_blank(deck, tmp_path):
    from scandeck.documents import ink_share, looks_blank

    path = page(tmp_path, "voll.pdf", lines=18)
    assert looks_blank(path) is False
    assert ink_share(path) > 0.05


def test_images_are_judged_like_pdfs(deck, tmp_path):
    from scandeck.documents import looks_blank

    assert looks_blank(page(tmp_path, "leer.jpg", fmt="JPEG")) is True
    assert looks_blank(page(tmp_path, "voll.jpg", lines=12, fmt="JPEG")) is False


def test_an_unreadable_file_is_never_called_blank(deck, tmp_path):
    """Unreadable is not the same as empty — that page must not be thrown away."""
    from scandeck.documents import looks_blank

    broken = tmp_path / "kaputt.pdf"
    broken.write_bytes(b"%PDF-1.4 kaputt")
    assert looks_blank(broken) is False


# --- crooked pages --------------------------------------------------------- #

@pytest.mark.parametrize("angle", [1.5, -2.5, 4.0])
def test_the_skew_angle_is_found(deck, tmp_path, angle):
    from scandeck.documents import find_skew

    found = find_skew(page(tmp_path, f"skew{angle}.pdf", lines=14, angle=angle))
    # The correction turns the other way, so only the magnitude is compared.
    assert abs(abs(found) - abs(angle)) < 0.6


def test_a_straight_page_is_left_alone(deck, tmp_path):
    from scandeck.documents import find_skew, straighten

    path = page(tmp_path, "gerade.pdf", lines=14)
    before = path.read_bytes()
    assert abs(find_skew(path)) < 0.5
    assert straighten(path) == 0.0
    assert path.read_bytes() == before  # not re-encoded for nothing


def test_an_empty_page_has_no_angle(deck, tmp_path):
    """Nothing to line up means nothing to correct."""
    from scandeck.documents import find_skew

    assert find_skew(page(tmp_path, "leer.pdf")) == 0.0


def test_straightening_actually_straightens(deck, tmp_path):
    from scandeck.documents import find_skew, straighten

    path = page(tmp_path, "schief.pdf", lines=14, angle=3.0)
    applied = straighten(path, dpi=150)
    assert abs(applied) > 2.0
    assert abs(find_skew(path)) < 0.5  # upright afterwards
    assert path.stat().st_size > 1000


def test_straightening_works_on_images_too(deck, tmp_path):
    from scandeck.documents import find_skew, straighten

    path = page(tmp_path, "schief.jpg", lines=14, angle=2.5, fmt="JPEG")
    assert abs(straighten(path)) > 1.5
    assert abs(find_skew(path)) < 0.6


def test_a_broken_file_survives_the_attempt(deck, tmp_path):
    from scandeck.documents import straighten

    broken = tmp_path / "kaputt.pdf"
    broken.write_bytes(b"%PDF-1.4 kaputt")
    assert straighten(broken) == 0.0
    assert broken.exists()


# --- the scan flow uses both ----------------------------------------------- #

def test_blank_sheets_are_dropped_from_a_run(deck, tmp_path):
    sheets = [page(tmp_path, "a.pdf", lines=10),
              page(tmp_path, "b.pdf"),
              page(tmp_path, "c.pdf", lines=6)]
    config = {**deck.store.get(), "skip_blank_pages": True}

    kept = deck.tidy_sheets(sheets, config)
    assert [path.name for path in kept] == ["a.pdf", "c.pdf"]
    assert not sheets[1].exists()  # and the file is gone


def test_nothing_is_touched_when_both_options_are_off(deck, tmp_path):
    sheets = [page(tmp_path, "a.pdf"), page(tmp_path, "b.pdf", lines=4)]
    before = [path.read_bytes() for path in sheets]

    kept = deck.tidy_sheets(sheets, deck.store.get())
    assert kept == sheets
    assert [path.read_bytes() for path in sheets] == before


def test_a_run_of_nothing_but_blanks_is_reported(deck, tmp_path):
    """Writing an empty document would be worse than saying so."""
    sheets = [page(tmp_path, "a.pdf"), page(tmp_path, "b.pdf")]
    config = {**deck.store.get(), "skip_blank_pages": True}

    with pytest.raises(RuntimeError, match="leer"):
        deck.tidy_sheets(sheets, config)

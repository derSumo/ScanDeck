"""Batch scanning: collect sheets, reorder them, merge them into one PDF."""

from __future__ import annotations

import pytest

pypdfium2 = pytest.importorskip("pypdfium2")
Image = pytest.importorskip("PIL.Image")


def page_file(tmp_path, name, colour=(255, 255, 255)):
    path = tmp_path / name
    Image.new("RGB", (120, 160), colour).save(path, "JPEG")
    return path


def multipage_pdf(tmp_path, name, pages=5):
    """What a feeder run looks like when the scanner answers with one document."""
    path = tmp_path / name
    images = [Image.new("RGB", (120, 160), (index * 20, 255, 255)) for index in range(pages)]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:], resolution=200.0)
    return path


def collect(deck, tmp_path, count=3):
    for index in range(count):
        deck.batch.add([page_file(tmp_path, f"seite_{index}.jpg")])


def pdf_pages(path):
    document = pypdfium2.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


# --- collecting ------------------------------------------------------------ #

def test_starting_a_batch_discards_the_previous_one(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 2)
    leftovers = [page["path"] for page in deck.batch.pages()]

    body = client.post("/api/batch/start").get_json()
    assert body["active"] is True and body["pages"] == []
    assert not any(deck.Path(path).exists() for path in leftovers)


def test_pages_are_appended_and_counted(client, deck, tmp_path):
    client.post("/api/batch/start")
    assert deck.batch.add([page_file(tmp_path, "a.jpg")]) == (0, 1, False, 1)
    assert deck.batch.add([page_file(tmp_path, "b.jpg")]) == (1, 2, False, 1)
    assert deck.batch.count() == 2
    assert deck.batch.active() is True


def test_a_feeder_run_of_separate_sheets_becomes_separate_tiles(client, deck, tmp_path):
    """Five sheets have to be five tiles, or none of them can be reordered."""
    client.post("/api/batch/start")
    sheets = [page_file(tmp_path, f"blatt_{index}.jpg") for index in range(5)]

    index, total, replaced, added = deck.batch.add(sheets)
    assert (index, total, replaced, added) == (0, 5, False, 5)
    assert len(client.get("/api/batch").get_json()["pages"]) == 5


def test_a_feeder_run_delivered_as_one_pdf_is_split_into_tiles(client, deck, tmp_path):
    """The case that looked like a single page: one PDF holding the whole stack."""
    client.post("/api/batch/start")
    stack = multipage_pdf(tmp_path, "einzug.pdf", pages=5)

    index, total, replaced, added = deck.batch.add([stack])
    assert (index, total, replaced, added) == (0, 5, False, 5)

    body = client.get("/api/batch").get_json()
    assert len(body["pages"]) == 5
    assert [page["index"] for page in body["pages"]] == [0, 1, 2, 3, 4]
    # Every tile is its own single-page file, so each can be turned or dropped.
    for page in deck.batch.pages():
        assert pdf_pages(deck.Path(page["path"])) == 1
    assert not stack.exists()


def test_split_pages_keeps_the_scanning_order(deck, tmp_path):
    from scandeck.documents import split_pages

    parts = split_pages(multipage_pdf(tmp_path, "stapel.pdf", pages=4), tmp_path / "out")
    assert len(parts) == 4
    assert [path.name for path in parts] == sorted(path.name for path in parts)


def test_split_pages_leaves_a_single_page_alone(deck, tmp_path):
    from scandeck.documents import split_pages

    single = multipage_pdf(tmp_path, "eine.pdf", pages=1)
    assert split_pages(single, tmp_path / "out") == [single]
    assert single.exists()  # not rewritten for nothing


def test_a_second_feeder_run_appends_behind_the_first(client, deck, tmp_path):
    client.post("/api/batch/start")
    deck.batch.add([multipage_pdf(tmp_path, "erst.pdf", pages=3)])
    index, total, replaced, added = deck.batch.add([multipage_pdf(tmp_path, "dann.pdf", pages=2)])
    assert (index, total, replaced, added) == (3, 5, False, 2)


def test_an_armed_page_is_replaced_not_appended(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 3)
    replaced_path = deck.batch.pages()[1]["path"]

    assert client.post("/api/batch/replace", json={"index": 1}).status_code == 200
    index, total, replaced, added = deck.batch.add([page_file(tmp_path, "neu.jpg")])
    assert (index, total, replaced, added) == (1, 3, True, 1)
    assert not deck.Path(replaced_path).exists()  # the bad page is gone
    # And the arming is spent, so the next page appends again.
    assert deck.batch.add([page_file(tmp_path, "vier.jpg")]) == (3, 4, False, 1)


def test_rescanning_an_armed_page_with_the_feeder_inserts_in_place(client, deck, tmp_path):
    """Two sheets replacing one land where the old page was, not at the end."""
    client.post("/api/batch/start")
    collect(deck, tmp_path, 3)
    keep_last = deck.batch.pages()[2]["name"]
    client.post("/api/batch/replace", json={"index": 1})

    index, total, replaced, added = deck.batch.add([multipage_pdf(tmp_path, "zwei.pdf", pages=2)])
    assert (index, total, replaced, added) == (1, 4, True, 2)
    assert deck.batch.pages()[3]["name"] == keep_last  # the tail kept its order


def test_replace_rejects_impossible_pages(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 2)
    assert client.post("/api/batch/replace", json={"index": 9}).status_code == 400
    assert client.post("/api/batch/replace", json={"index": True}).status_code == 400
    assert client.post("/api/batch/replace", json={"index": "1"}).status_code == 400
    assert client.post("/api/batch/replace", json={"index": None}).status_code == 200


# --- editing --------------------------------------------------------------- #

def test_reordering_moves_pages_and_follows_the_armed_one(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 3)
    names = [page["name"] for page in deck.batch.pages()]
    client.post("/api/batch/replace", json={"index": 0})

    body = client.post("/api/batch/order", json={"order": [2, 0, 1]}).get_json()
    assert [page["name"] for page in body["pages"]] == [names[2], names[0], names[1]]
    assert body["replace_index"] == 1  # still the same sheet, new slot


def test_reordering_rejects_a_list_that_is_not_a_permutation(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 3)
    for bad in ([0, 1], [0, 1, 1], ["a", 1, 2], [0, 1, 5], "keine liste"):
        assert client.post("/api/batch/order", json={"order": bad}).status_code == 400


def test_rotation_is_remembered_per_page(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 2)
    client.post("/api/batch/page/1/rotate", json={"degrees": 90})
    body = client.post("/api/batch/page/1/rotate", json={"degrees": 270}).get_json()
    assert body["pages"][0]["rotation"] == 0
    assert body["pages"][1]["rotation"] == 0  # 90 + 270 is a full turn
    assert client.post("/api/batch/page/9/rotate", json={}).status_code == 404


def test_normalise_rotation_snaps_to_quarter_turns(deck):
    assert deck.normalise_rotation(90) == 90
    assert deck.normalise_rotation(-90) == 270
    assert deck.normalise_rotation(450) == 90
    assert deck.normalise_rotation("krumm") == 0
    assert deck.normalise_rotation(None) == 0


def test_deleting_a_page_removes_its_file(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 3)
    gone = deck.Path(deck.batch.pages()[1]["path"])

    body = client.delete("/api/batch/page/1").get_json()
    assert len(body["pages"]) == 2
    assert not gone.exists()
    assert client.delete("/api/batch/page/7").status_code == 404


def test_cancelling_removes_every_collected_file(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 3)
    paths = [deck.Path(page["path"]) for page in deck.batch.pages()]

    body = client.post("/api/batch/cancel").get_json()
    assert body == {"active": False, "pages": [], "replace_index": None}
    assert not any(path.exists() for path in paths)


# --- merging --------------------------------------------------------------- #

def test_merging_produces_one_pdf_with_every_page(deck, tmp_path):
    from scandeck.documents import merge_pages

    for index in range(4):
        deck.batch.add([page_file(tmp_path, f"m_{index}.jpg")])

    target = tmp_path / "zusammen.pdf"
    assert merge_pages(deck.batch.pages(), target) == 4
    assert pdf_pages(target) == 4


def test_merging_applies_the_rotation(deck, tmp_path):
    from scandeck.documents import merge_pages

    deck.batch.add([page_file(tmp_path, "quer.jpg")])
    pages = deck.batch.pages()
    pages[0]["rotation"] = 90

    target = tmp_path / "gedreht.pdf"
    merge_pages(pages, target)
    document = pypdfium2.PdfDocument(str(target))
    try:
        width, height = document[0].get_size()
        assert width > height  # the portrait page now lies on its side
    finally:
        document.close()


def test_finishing_writes_the_document_and_records_it(deck, tmp_path, monkeypatch):
    monkeypatch.setattr(deck, "queue_upload",
                        lambda path, config, pages, tags: {"id": "x", "pages": pages})
    deck.batch.begin()
    for index in range(2):
        deck.batch.add([page_file(tmp_path, f"f_{index}.jpg")])

    config = deck.validate_config(deck.store.get())
    target = deck.finish_batch(config, ["Rechnung"])

    assert target.exists() and target.suffix == ".pdf"
    assert "2-seiten" in target.name
    assert deck.scan_state["last_name"] == target.name
    # The batch is empty again and its scratch files are gone.
    assert deck.batch.count() == 0
    assert deck.batch.active() is False


def test_finishing_a_feeder_stack_keeps_every_page(deck, tmp_path, monkeypatch):
    recorded = {}
    monkeypatch.setattr(deck, "queue_upload",
                        lambda path, config, pages, tags: recorded.update(pages=pages))
    deck.batch.begin()
    deck.batch.add([multipage_pdf(tmp_path, "einzug.pdf", pages=5)])

    target = deck.finish_batch(deck.validate_config(deck.store.get()), [])
    assert pdf_pages(target) == 5
    assert recorded["pages"] == 5
    assert "5-seiten" in target.name


def test_finishing_an_empty_batch_is_refused(client, deck):
    client.post("/api/batch/start")
    assert client.post("/api/batch/finish", json={}).status_code == 400
    assert deck.scan_lock.acquire(blocking=False) is True  # lock was not taken
    deck.scan_lock.release()


def test_finishing_is_refused_while_a_scan_runs(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 1)
    deck.scan_lock.acquire()
    try:
        assert client.post("/api/batch/finish", json={}).status_code == 409
    finally:
        deck.scan_lock.release()


# --- whole-stack corrections ----------------------------------------------- #

def test_rotating_the_whole_stack_turns_every_page(client, deck, tmp_path):
    """A feeder stack is upside down as a whole, not page by page."""
    client.post("/api/batch/start")
    collect(deck, tmp_path, 3)
    deck.batch.rotate(1, 90)  # one page already corrected by hand

    body = client.post("/api/batch/rotate-all", json={"degrees": 180}).get_json()
    assert [page["rotation"] for page in body["pages"]] == [180, 270, 180]


def test_rotating_the_whole_stack_defaults_to_a_half_turn(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 2)
    body = client.post("/api/batch/rotate-all", json={}).get_json()
    assert [page["rotation"] for page in body["pages"]] == [180, 180]


def test_reversing_flips_the_order(client, deck, tmp_path):
    """Many feeders deliver the last sheet first."""
    client.post("/api/batch/start")
    collect(deck, tmp_path, 4)
    names = [page["name"] for page in deck.batch.pages()]

    body = client.post("/api/batch/reverse", json={}).get_json()
    assert [page["name"] for page in body["pages"]] == list(reversed(names))


def test_reversing_keeps_the_armed_page_armed(client, deck, tmp_path):
    client.post("/api/batch/start")
    collect(deck, tmp_path, 4)
    armed_name = deck.batch.pages()[0]["name"]
    client.post("/api/batch/replace", json={"index": 0})

    body = client.post("/api/batch/reverse", json={}).get_json()
    assert body["replace_index"] == 3
    assert body["pages"][3]["name"] == armed_name


def test_bulk_actions_on_an_empty_stack_do_nothing(client, deck):
    client.post("/api/batch/start")
    assert client.post("/api/batch/rotate-all", json={}).get_json()["pages"] == []
    assert client.post("/api/batch/reverse", json={}).get_json()["pages"] == []

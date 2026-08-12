"""End to end: a feeder run, from the device answer to what the interface shows."""

from __future__ import annotations

import pytest

from tests.test_escl import FakeScanner, one_page_pdf

pypdfium2 = pytest.importorskip("pypdfium2")
Image = pytest.importorskip("PIL.Image")


class PdfScanner(FakeScanner):
    """A device that answers the whole feeder run with one multi-page PDF."""

    def __init__(self, pages=5):
        super().__init__(sheets=1, content_type="application/pdf")
        self.pdf_pages = pages

    def sheet_bytes(self, number):
        return one_page_pdf(self.pdf_pages)


def pdf_page_count(path):
    document = pypdfium2.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


@pytest.fixture()
def run_scan(deck, monkeypatch):
    """Drive the real scan worker against a fake device, synchronously."""
    def run(device, **overrides):
        from scandeck import escl

        monkeypatch.setattr(escl, "scanner_session", device)
        settings = {"scanner_url": "https://10.0.0.31:443", "source": "Feeder", **overrides}
        deck.store.patch(**settings)
        deck.scan_lock.acquire()
        deck.scan_worker([], "ui", {})  # releases the lock itself
        return deck.scan_state

    return run


# --- what the batch strip ends up showing ---------------------------------- #

def test_five_sheets_become_five_tiles_in_the_batch(client, deck, run_scan):
    """The reported problem: one tile for a whole feeder run."""
    client.post("/api/batch/start")
    run_scan(FakeScanner(sheets=5))

    pages = client.get("/api/batch").get_json()["pages"]
    assert len(pages) == 5
    assert [page["index"] for page in pages] == [0, 1, 2, 3, 4]
    # Each tile can be moved on its own, which is the whole point.
    assert client.post("/api/batch/order", json={"order": [4, 3, 2, 1, 0]}).status_code == 200


def test_a_single_pdf_holding_the_stack_is_split_into_tiles(client, deck, run_scan):
    """Same run, but the scanner answered with one five-page document."""
    client.post("/api/batch/start")
    run_scan(PdfScanner(pages=5))

    pages = client.get("/api/batch").get_json()["pages"]
    assert len(pages) == 5
    for page in deck.batch.pages():
        assert pdf_page_count(deck.Path(page["path"])) == 1


def test_the_batch_still_grows_across_several_feeder_runs(client, deck, run_scan):
    client.post("/api/batch/start")
    run_scan(FakeScanner(sheets=3))
    run_scan(FakeScanner(sheets=2))
    assert len(client.get("/api/batch").get_json()["pages"]) == 5


def test_finishing_the_batch_yields_one_document_with_every_sheet(client, deck, run_scan):
    client.post("/api/batch/start")
    run_scan(PdfScanner(pages=4))

    config = deck.validate_config(deck.store.get())
    target = deck.finish_batch(config, [])
    assert pdf_page_count(target) == 4
    assert deck.batch.count() == 0


# --- without a batch ------------------------------------------------------- #

def test_a_feeder_run_without_a_batch_becomes_one_document(client, deck, run_scan):
    """Five sheets are one document, not five files in the history."""
    run_scan(FakeScanner(sheets=5))

    entries = client.get("/api/history").get_json()["jobs"]
    assert len(entries) == 1
    assert entries[0]["pages"] == 5
    assert "5-seiten" in entries[0]["name"]
    assert pdf_page_count(deck.Path(deck.scan_state["last_file"])) == 5


def test_the_flatbed_still_produces_a_plain_single_scan(client, deck, run_scan):
    run_scan(FakeScanner(sheets=1), source="Platen")

    entries = client.get("/api/history").get_json()["jobs"]
    assert len(entries) == 1
    assert entries[0]["pages"] == 1
    assert "seiten" not in entries[0]["name"]


def test_a_multipage_pdf_is_counted_correctly_in_the_history(client, deck, run_scan):
    """One document, several pages: the history must not claim it is one page."""
    run_scan(PdfScanner(pages=3), source="Platen")
    assert client.get("/api/history").get_json()["jobs"][0]["pages"] == 3


def test_the_scan_lock_is_free_again_afterwards(deck, run_scan):
    run_scan(FakeScanner(sheets=2))
    assert deck.scan_lock.acquire(blocking=False) is True
    deck.scan_lock.release()
    assert deck.scan_state["running"] is False
    assert deck.scan_state["last_error"] is None


def test_a_failing_scan_reports_and_releases(deck, run_scan):
    state = run_scan(FakeScanner(sheets=0))
    assert state["last_error"]
    assert state["running"] is False
    assert deck.scan_lock.acquire(blocking=False) is True
    deck.scan_lock.release()


# --- robustness ------------------------------------------------------------ #

class BrokenPdfScanner(FakeScanner):
    """Firmware that labels its answer a PDF but sends something unreadable."""

    def sheet_bytes(self, number):
        return b"%PDF-1.4 kaputt"


def test_an_unreadable_document_is_kept_not_lost(client, deck, run_scan):
    """The scan happened; refusing to count its pages must not throw it away."""
    state = run_scan(BrokenPdfScanner(sheets=1), source="Platen")
    assert state["last_error"] is None

    entries = client.get("/api/history").get_json()["jobs"]
    assert len(entries) == 1 and entries[0]["pages"] == 1
    assert deck.Path(state["last_file"]).exists()


def test_an_unreadable_document_still_becomes_one_batch_tile(client, deck, run_scan):
    client.post("/api/batch/start")
    state = run_scan(BrokenPdfScanner(sheets=1), source="Platen")
    assert state["last_error"] is None
    assert len(client.get("/api/batch").get_json()["pages"]) == 1


# --- cancelling ------------------------------------------------------------ #

def test_cancelling_a_run_adds_nothing_to_the_batch(client, deck, monkeypatch):
    """Half a feeder run must not turn into pages nobody asked for."""
    from scandeck import escl

    client.post("/api/batch/start")
    device = FakeScanner(sheets=6)
    monkeypatch.setattr(escl, "scanner_session", device)
    deck.store.patch(scanner_url="https://10.0.0.31:443", source="Feeder")

    original = device.get

    def stop_after_two(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith("NextDocument") and device.delivered == 2:
            deck.cancel_active_scan()
        return response

    device.get = stop_after_two
    deck.scan_lock.acquire()
    deck.scan_worker([], "ui", {})

    assert client.get("/api/batch").get_json()["pages"] == []
    assert deck.scan_state["last_error"] is None  # cancelling is not a failure
    assert deck.scan_state["stage"] == "cancelled"
    # And the scanner is free for the next attempt straight away.
    assert deck.scan_lock.acquire(blocking=False) is True
    deck.scan_lock.release()


def test_cancelling_records_no_history_entry(client, deck, monkeypatch):
    from scandeck import escl

    device = FakeScanner(sheets=4)
    monkeypatch.setattr(escl, "scanner_session", device)
    deck.store.patch(scanner_url="https://10.0.0.31:443", source="Feeder")
    original = device.get

    def stop_immediately(url, **kwargs):
        if url.endswith("NextDocument"):
            deck.cancel_active_scan()
        return original(url, **kwargs)

    device.get = stop_immediately
    deck.scan_lock.acquire()
    deck.scan_worker([], "ui", {})

    assert client.get("/api/history").get_json()["jobs"] == []


def test_the_cancel_endpoint_refuses_when_nothing_runs(client):
    assert client.post("/api/scan/cancel").status_code == 409


def test_the_cancel_endpoint_stops_a_running_scan(client, deck):
    class Stoppable:
        def __init__(self):
            self.aborted = False

        def abort(self):
            self.aborted = True

    stoppable = Stoppable()
    deck.set_active_scan(stoppable)
    deck.scan_state["running"] = True
    try:
        assert client.post("/api/scan/cancel").status_code == 202
        assert stoppable.aborted
    finally:
        deck.scan_state["running"] = False
        deck.set_active_scan(None)

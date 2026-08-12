"""Talking eSCL: the scan settings, and fetching every sheet of a feeder run."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
import requests

CAPABILITIES = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"
    xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03">
  <pwg:Version>2.63</pwg:Version>
  <pwg:MakeAndModel>HP DeskJet 4100</pwg:MakeAndModel>
  <scan:PlatenInputCaps><scan:MinWidth>1</scan:MinWidth></scan:PlatenInputCaps>
  <scan:AdfSimplexInputCaps><scan:MinWidth>1</scan:MinWidth></scan:AdfSimplexInputCaps>
  <scan:AdfDuplexInputCaps><scan:MinWidth>1</scan:MinWidth></scan:AdfDuplexInputCaps>
</scan:ScannerCapabilities>"""

STATUS_IDLE = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"
    xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03">
  <pwg:State>Idle</pwg:State>
</scan:ScannerStatus>"""


def one_page_pdf(pages=1):
    """A minimal but valid PDF, built once and reused."""
    import io

    from PIL import Image

    images = [Image.new("RGB", (120, 160), (40 * index, 200, 255)) for index in range(pages)]
    buffer = io.BytesIO()
    images[0].save(buffer, "PDF", save_all=True, append_images=images[1:], resolution=200.0)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, status_code=200, content=b"", text="", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text or (content.decode("latin-1") if content else "")
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeScanner:
    """A scanner that hands out a fixed number of sheets, then reports empty."""

    def __init__(self, sheets=1, content_type="application/pdf", end_status=404):
        self.sheets = sheets
        self.content_type = content_type
        self.end_status = end_status
        self.delivered = 0
        self.settings_xml = ""
        self.deleted = False
        self.next_document_calls = 0

    def sheet_bytes(self, number):
        """A real one-page PDF, so everything downstream can actually read it."""
        return one_page_pdf()

    def get(self, url, **kwargs):
        if url.endswith("ScannerStatus"):
            return FakeResponse(text=STATUS_IDLE)
        if url.endswith("ScannerCapabilities"):
            return FakeResponse(text=CAPABILITIES)
        if url.endswith("NextDocument"):
            self.next_document_calls += 1
            if self.delivered >= self.sheets:
                return FakeResponse(status_code=self.end_status)
            self.delivered += 1
            return FakeResponse(
                content=self.sheet_bytes(self.delivered),
                headers={"Content-Type": self.content_type},
            )
        raise AssertionError(f"unerwarteter GET: {url}")

    def post(self, url, data=None, **kwargs):
        assert url.endswith("/eSCL/ScanJobs")
        self.settings_xml = data.decode("utf-8")
        return FakeResponse(status_code=201, headers={"Location": "/eSCL/ScanJobs/4711"})

    def delete(self, url, **kwargs):
        self.deleted = True
        return FakeResponse(status_code=200)


@pytest.fixture()
def scanner(deck, monkeypatch):
    """Build a ScannerClient wired to a fake device."""
    from scandeck import escl

    def build(sheets=1, source="Platen", end_status=404, **overrides):
        device = FakeScanner(sheets=sheets, end_status=end_status)
        monkeypatch.setattr(escl, "scanner_session", device)
        config = deck.validate_config({
            **deck.store.get(),
            "scanner_url": "https://10.0.0.31:443",
            "source": source,
            **overrides,
        })
        return device, escl.ScannerClient(config, deck.logs, deck.timings)

    return build


# --- the multi-page loop --------------------------------------------------- #

def test_the_flatbed_returns_exactly_one_page(scanner):
    device, client = scanner(sheets=1, source="Platen")
    pages = client.scan()
    assert len(pages) == 1
    assert pages[0].exists()
    # The flatbed holds one sheet; asking again would only cost a round trip.
    assert device.next_document_calls == 1


def test_the_feeder_returns_every_sheet(scanner):
    """This is what fetching NextDocument once used to throw away."""
    device, client = scanner(sheets=5, source="Feeder")
    pages = client.scan()
    assert len(pages) == 5
    assert all(path.exists() for path in pages)
    assert [path.name for path in pages] == sorted(path.name for path in pages)
    assert device.next_document_calls == 6  # five sheets, then the empty answer


def test_an_empty_feeder_is_a_readable_error_not_a_crash(scanner):
    device, client = scanner(sheets=0, source="Feeder")
    with pytest.raises(RuntimeError, match="keine Seite"):
        client.scan()


def test_the_job_is_released_even_when_the_scan_fails(scanner):
    device, client = scanner(sheets=0, source="Feeder")
    with pytest.raises(RuntimeError):
        client.scan()
    assert device.deleted  # otherwise the next scan is refused as busy


def test_the_job_is_released_after_a_good_run(scanner):
    device, client = scanner(sheets=2, source="Feeder")
    client.scan()
    assert device.deleted


@pytest.mark.parametrize("end_status", [404, 410, 204])
def test_every_way_of_saying_empty_ends_the_run(scanner, end_status):
    """Firmware disagrees on how to say "no more paper"; all of it must work."""
    device, client = scanner(sheets=3, source="Feeder", end_status=end_status)
    assert len(client.scan()) == 3
    assert device.next_document_calls == 4


def test_a_feeder_run_is_measured_per_sheet(scanner, deck):
    """One average for a five-sheet run would make the progress bar lie."""
    device, client = scanner(sheets=4, source="Feeder")
    client.scan()
    from scandeck.jobs import TimingStore

    expected = deck.timings.expected(TimingStore.key(client.config))
    assert expected is not None and expected < 60


# --- scan settings --------------------------------------------------------- #

def settings_of(device):
    return ET.fromstring(device.settings_xml)


def find(root, tag):
    for element in root.iter():
        if element.tag.endswith("}" + tag):
            return element.text
    return None


def test_the_paper_size_reaches_the_scanner(scanner):
    from scandeck.config import PAPER_SIZES

    device, client = scanner(sheets=1, paper_size="Legal")
    client.scan()
    width, height = PAPER_SIZES["Legal"]
    root = settings_of(device)
    assert find(root, "Width") == str(width)
    assert find(root, "Height") == str(height)


def test_a4_stays_the_default(scanner):
    device, client = scanner(sheets=1)
    client.scan()
    assert find(settings_of(device), "Width") == "2480"


def test_duplex_is_requested_only_for_the_feeder(scanner):
    device, client = scanner(sheets=1, source="Feeder", duplex=True)
    client.scan()
    assert find(settings_of(device), "Duplex") == "true"

    device, client = scanner(sheets=1, source="Platen", duplex=True)
    client.scan()
    assert find(settings_of(device), "Duplex") is None


def test_the_profile_reaches_the_scanner(scanner):
    device, client = scanner(sheets=1, resolution=600, color_mode="Grayscale8")
    client.scan()
    root = settings_of(device)
    assert find(root, "XResolution") == "600"
    assert find(root, "ColorMode") == "Grayscale8"


# --- capabilities ---------------------------------------------------------- #

def test_capabilities_report_sources_and_duplex(deck):
    from scandeck.escl import parse_capabilities

    result = parse_capabilities(CAPABILITIES)
    assert result["sources"] == ["Platen", "Feeder"]
    assert result["duplex"] is True
    assert result["model"] == "HP DeskJet 4100"
    assert result["version"] == "2.63"


def test_capabilities_without_a_feeder(deck):
    from scandeck.escl import parse_capabilities

    stripped = CAPABILITIES.replace("Adf", "Unused")
    result = parse_capabilities(stripped)
    assert result["sources"] == ["Platen"]
    assert result["duplex"] is False

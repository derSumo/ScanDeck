"""Talking eSCL: the scan settings, and fetching every sheet of a feeder run."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

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

def status_xml(state="Idle", adf="ScannerAdfLoaded"):
    """ScannerStatus as the device sends it; adf="" means no feeder is reported."""
    tray = f"\n  <scan:AdfState>{adf}</scan:AdfState>" if adf else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"
    xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03">
  <pwg:State>{state}</pwg:State>{tray}
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

    def __init__(self, sheets=1, content_type="application/pdf", end_status=404,
                 adf_state="ScannerAdfLoaded"):
        self.adf_state = adf_state
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
            return FakeResponse(text=status_xml(adf=self.adf_state))
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

    def build(sheets=1, source="Platen", end_status=404, adf_state="ScannerAdfLoaded", **overrides):
        device = FakeScanner(sheets=sheets, end_status=end_status, adf_state=adf_state)
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


# --- fitting the request to the device ------------------------------------- #

# Values taken from a real HP DeskJet 4100: the flatbed stops at A4 height,
# the feeder reaches Legal but only scans up to 300 dpi, and it cannot duplex.
def _steps(*values):
    inner = "".join(
        f"<scan:DiscreteResolution><scan:XResolution>{dpi}</scan:XResolution>"
        f"<scan:YResolution>{dpi}</scan:YResolution></scan:DiscreteResolution>"
        for dpi in values
    )
    return f"<scan:SupportedResolutions><scan:DiscreteResolutions>{inner}</scan:DiscreteResolutions></scan:SupportedResolutions>"


_MODES = ("<scan:ColorMode>BlackAndWhite1</scan:ColorMode><scan:ColorMode>Grayscale8</scan:ColorMode>"
          "<scan:ColorMode>RGB24</scan:ColorMode>"
          "<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>"
          "<pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>")

DESKJET = CAPABILITIES.replace(
    "<scan:PlatenInputCaps><scan:MinWidth>1</scan:MinWidth></scan:PlatenInputCaps>",
    "<scan:PlatenInputCaps><scan:MaxWidth>2550</scan:MaxWidth>"
    "<scan:MaxHeight>3508</scan:MaxHeight>"
    "<scan:MaxOpticalXResolution>1200</scan:MaxOpticalXResolution>"
    + _steps(75, 100, 200, 300, 600, 1200) + _MODES + "</scan:PlatenInputCaps>",
).replace(
    "<scan:AdfSimplexInputCaps><scan:MinWidth>1</scan:MinWidth></scan:AdfSimplexInputCaps>",
    "<scan:AdfSimplexInputCaps><scan:MaxWidth>2550</scan:MaxWidth>"
    "<scan:MaxHeight>4200</scan:MaxHeight>"
    "<scan:MaxOpticalXResolution>300</scan:MaxOpticalXResolution>"
    + _steps(75, 100, 200, 300) + _MODES + "</scan:AdfSimplexInputCaps>",
).replace(
    "<scan:AdfDuplexInputCaps><scan:MinWidth>1</scan:MinWidth></scan:AdfDuplexInputCaps>", "",
)


class DeskJet(FakeScanner):
    """Answers ScannerCapabilities like the real device does."""

    def __init__(self, sheets=1, adf_state="ScannerAdfLoaded"):
        super().__init__(sheets=sheets, adf_state=adf_state)
        self.capability_calls = 0

    def get(self, url, **kwargs):
        if url.endswith("ScannerCapabilities"):
            self.capability_calls += 1
            return FakeResponse(text=DESKJET)
        return super().get(url, **kwargs)


@pytest.fixture()
def deskjet(deck, monkeypatch):
    from scandeck import escl

    escl.capability_cache.clear()

    def build(**overrides):
        device = DeskJet(sheets=1)
        monkeypatch.setattr(escl, "scanner_session", device)
        config = deck.validate_config({
            **deck.store.get(), "scanner_url": "https://10.0.0.31:443", **overrides,
        })
        return device, escl.ScannerClient(config, deck.logs, deck.timings)

    return build


def test_limits_are_read_per_source(deck):
    from scandeck.escl import parse_capabilities

    limits = parse_capabilities(DESKJET)["limits"]
    assert limits["Platen"]["max_height"] == 3508
    assert limits["Feeder"]["max_height"] == 4200
    assert limits["Feeder"]["max_resolution"] == 300


def test_legal_is_not_offered_for_a_flatbed_that_stops_at_a4(deck):
    from scandeck.escl import paper_sizes_for, parse_capabilities

    limits = parse_capabilities(DESKJET)["limits"]
    assert "Legal" not in paper_sizes_for(limits["Platen"])
    assert set(paper_sizes_for(limits["Platen"])) == {"A4", "Letter", "A5"}
    # The feeder is taller, so there Legal is fine.
    assert "Legal" in paper_sizes_for(limits["Feeder"])


def test_legal_on_the_flatbed_is_trimmed_instead_of_refused(deskjet):
    """This is the HTTP 409 the device answered with before."""
    device, client = deskjet(source="Platen", paper_size="Legal")
    client.scan()
    root = settings_of(device)
    assert find(root, "Height") == "3508"  # clamped to what the glass can do
    assert find(root, "Width") == "2550"


def test_a_size_that_fits_is_sent_unchanged(deskjet):
    device, client = deskjet(source="Platen", paper_size="Letter")
    client.scan()
    assert (find(settings_of(device), "Width"), find(settings_of(device), "Height")) == ("2550", "3300")


def test_the_feeder_resolution_is_capped(deskjet):
    device, client = deskjet(source="Feeder", resolution=600)
    client.scan()
    assert find(settings_of(device), "XResolution") == "300"


def test_the_flatbed_keeps_its_high_resolution(deskjet):
    device, client = deskjet(source="Platen", resolution=600)
    client.scan()
    assert find(settings_of(device), "XResolution") == "600"


def test_duplex_is_dropped_on_a_device_that_cannot_do_it(deskjet):
    device, client = deskjet(source="Feeder", duplex=True)
    client.scan()
    assert find(settings_of(device), "Duplex") == "false"


def test_an_unreadable_device_gets_the_settings_unchanged(deck, monkeypatch):
    """Not knowing the limits must never stand in the way of a scan."""
    from scandeck import escl

    escl.capability_cache.clear()

    class NoCapabilities(FakeScanner):
        def get(self, url, **kwargs):
            if url.endswith("ScannerCapabilities"):
                return FakeResponse(status_code=500)
            return super().get(url, **kwargs)

    device = NoCapabilities(sheets=1)
    monkeypatch.setattr(escl, "scanner_session", device)
    config = deck.validate_config({**deck.store.get(), "scanner_url": "https://x:443",
                                   "source": "Platen", "paper_size": "Legal"})
    escl.ScannerClient(config, deck.logs, deck.timings).scan()
    assert find(settings_of(device), "Height") == "4200"


def test_capabilities_are_fetched_once_and_then_cached(deskjet):
    """A scan must not pay for the same answer over and over."""
    device, client = deskjet(source="Platen")
    device.sheets = 3
    client.scan()
    assert device.capability_calls == 1

    client.known_capabilities()
    client.fit_to_device()
    assert device.capability_calls == 1  # still the cached answer


def test_a_rejected_job_says_what_is_wrong(deskjet):
    device, client = deskjet(source="Platen")
    message = client._rejection_reason(409)
    assert "409" in message and "Papierformat" in message
    assert "A4" in message and "Legal" not in message  # names only what fits


def test_an_empty_feeder_is_named_as_such(deskjet):
    device, client = deskjet(source="Feeder")
    assert "Papier im Einzug" in client._rejection_reason(503)


# --- the sheet feeder tray ------------------------------------------------- #

def test_an_empty_tray_is_named_before_a_job_is_created(scanner):
    """The device answers an empty tray with a bare 409 that blames nothing."""
    device, client = scanner(sheets=3, source="Feeder", adf_state="ScannerAdfEmpty")
    with pytest.raises(RuntimeError, match="Einzug ist leer"):
        client.scan()
    # No job was created at all, so nothing has to be cleaned up on the device.
    assert device.settings_xml == ""


def test_a_paper_jam_is_named(scanner):
    device, client = scanner(sheets=1, source="Feeder", adf_state="ScannerAdfJam")
    with pytest.raises(RuntimeError, match="Papierstau im Einzug"):
        client.scan()


def test_a_device_without_a_feeder_says_so(scanner):
    device, client = scanner(sheets=1, source="Feeder", adf_state="")
    with pytest.raises(RuntimeError, match="keinen Einzug"):
        client.scan()


def test_a_loaded_tray_scans(scanner):
    device, client = scanner(sheets=3, source="Feeder", adf_state="ScannerAdfLoaded")
    assert len(client.scan()) == 3


def test_a_tray_already_pulling_pages_is_accepted(scanner):
    device, client = scanner(sheets=2, source="Feeder", adf_state="ScannerAdfProcessing")
    assert len(client.scan()) == 2


def test_the_flatbed_never_asks_about_the_tray(scanner):
    """An empty feeder must not stand in the way of a scan from the glass."""
    device, client = scanner(sheets=1, source="Platen", adf_state="ScannerAdfEmpty")
    assert len(client.scan()) == 1


def test_a_feeder_409_does_not_blame_the_paper_format(scanner):
    device, client = scanner(sheets=1, source="Feeder")
    message = client._rejection_reason(409)
    assert "kein Papier" in message
    assert "Papierformat" not in message  # that was the misleading part


def test_the_status_reports_state_and_tray_together(scanner):
    device, client = scanner(sheets=1, adf_state="ScannerAdfEmpty")
    assert client.full_status() == {"state": "Idle", "adf": "ScannerAdfEmpty"}
    assert client.status() == "Idle"


# --- cancelling a run ------------------------------------------------------ #

def test_cancelling_before_the_first_sheet_raises(scanner):
    from scandeck.escl import ScanCancelled

    device, client = scanner(sheets=3, source="Feeder")
    client.cancel.set()
    with pytest.raises(ScanCancelled):
        client.scan()


def test_cancelling_mid_run_leaves_nothing_behind(scanner):
    """Cancelling means this scan did not happen — no half document survives."""
    from scandeck.escl import ScanCancelled

    device, client = scanner(sheets=10, source="Feeder")
    original = device.get
    collected = []

    def stop_after_two(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith("NextDocument") and device.delivered == 2:
            client.cancel.set()
        return response

    device.get = stop_after_two
    output_dir = Path(client.config["output_dir"])
    with pytest.raises(ScanCancelled):
        client.scan()
    collected = list(output_dir.glob("scan_*"))
    assert collected == []  # the two sheets already pulled in are gone again
    assert device.deleted  # and the job was released on the device


def test_abort_releases_the_job_on_the_device(scanner):
    device, client = scanner(sheets=1)
    client._job_url = "https://10.0.0.31:443/eSCL/ScanJobs/4711"
    client.abort()
    assert client.cancel.is_set()
    assert device.deleted


def test_abort_without_a_running_job_is_harmless(scanner):
    device, client = scanner(sheets=1)
    client.abort()
    assert client.cancel.is_set()
    assert not device.deleted


# --- reading what the device can do ---------------------------------------- #

def test_the_resolution_steps_are_read_from_the_device(deck):
    from scandeck.escl import parse_capabilities

    xml = CAPABILITIES.replace(
        "<scan:PlatenInputCaps><scan:MinWidth>1</scan:MinWidth></scan:PlatenInputCaps>",
        "<scan:PlatenInputCaps>"
        "<scan:SupportedResolutions><scan:DiscreteResolutions>"
        "<scan:DiscreteResolution><scan:XResolution>75</scan:XResolution>"
        "<scan:YResolution>75</scan:YResolution></scan:DiscreteResolution>"
        "<scan:DiscreteResolution><scan:XResolution>300</scan:XResolution>"
        "<scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>"
        "<scan:DiscreteResolution><scan:XResolution>1200</scan:XResolution>"
        "<scan:YResolution>1200</scan:YResolution></scan:DiscreteResolution>"
        "</scan:DiscreteResolutions></scan:SupportedResolutions>"
        "</scan:PlatenInputCaps>",
    )
    limits = parse_capabilities(xml)["limits"]["Platen"]
    assert limits["resolutions"] == [75, 300, 1200]
    # Without MaxOpticalXResolution the highest step is the ceiling.
    assert limits["max_resolution"] == 1200


def test_colour_modes_and_formats_are_read_without_duplicates(deck):
    from scandeck.escl import parse_capabilities

    xml = CAPABILITIES.replace(
        "<scan:PlatenInputCaps><scan:MinWidth>1</scan:MinWidth></scan:PlatenInputCaps>",
        "<scan:PlatenInputCaps>"
        "<scan:ColorMode>RGB24</scan:ColorMode><scan:ColorMode>Grayscale8</scan:ColorMode>"
        "<pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>"
        "<scan:DocumentFormatExt>application/pdf</scan:DocumentFormatExt>"
        "<pwg:DocumentFormat>application/octet-stream</pwg:DocumentFormat>"
        "</scan:PlatenInputCaps>",
    )
    limits = parse_capabilities(xml)["limits"]["Platen"]
    assert limits["color_modes"] == ["RGB24", "Grayscale8"]
    assert limits["formats"] == ["application/pdf"]  # listed once, no octet-stream


def test_millimetres_converts_the_escl_unit(deck):
    from scandeck.escl import millimetres

    assert millimetres(2480) == 210  # A4 width
    assert millimetres(3508) == 297  # A4 height
    assert millimetres(None) is None

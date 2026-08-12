"""Talking eSCL to the scanner: capabilities, status and the scan itself."""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import urllib3

from scandeck.config import PAPER_SIZES
from scandeck.events import LogHub, progress_ramp
from scandeck.jobs import TimingStore
from scandeck.version import USER_AGENT

PWG_NS = "http://www.pwg.org/schemas/2010/12/sm"
SCAN_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"
# HP eSCL scanners commonly require the compatible client version 2.0 for
# ScanJobs, even if ScannerCapabilities reports a newer device version.
REQUEST_ESCL_VERSION = "2.0"

# What the device says about its sheet feeder tray.
ADF_EMPTY = "ScannerAdfEmpty"
ADF_JAM = "ScannerAdfJam"
ADF_READY = ("ScannerAdfLoaded", "ScannerAdfProcessing")
ADF_LABELS = {
    ADF_EMPTY: "leer",
    ADF_JAM: "Papierstau",
    "ScannerAdfLoaded": "bestückt",
    "ScannerAdfProcessing": "zieht ein",
}

# A sheet feeder that never reports "empty" must not scan forever.
MAX_FEEDER_PAGES = 200
FIRST_PAGE_TIMEOUT = 180
NEXT_PAGE_TIMEOUT = 120


# One session for every talk to the scanner. Each scan needs three requests
# (status, job, document) and HTTPS costs a full TLS handshake per connection —
# keeping it alive saves that handshake within a scan and between scans.
scanner_session = requests.Session()
scanner_session.headers.update({"User-Agent": USER_AGENT, "Connection": "keep-alive"})
for _scheme in ("https://", "http://"):
    scanner_session.mount(
        _scheme,
        requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0),
    )


class ScanCancelled(RuntimeError):
    """Raised when the user stopped the scan; not a failure to report as one."""


class CapabilityCache:
    """ScannerCapabilities per device, so a scan costs at most one extra request.

    The answer describes hardware and does not change while the device is
    plugged in, so a generous lifetime is enough — and a device that cannot be
    asked simply falls back to sending the settings unchanged.
    """

    TTL = 30 * 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, base_url: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(base_url)
        if not entry or time.time() - entry[0] > self.TTL:
            return None
        return entry[1]

    def put(self, base_url: str, capabilities: dict[str, Any]) -> None:
        with self._lock:
            self._entries[base_url] = (time.time(), capabilities)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


capability_cache = CapabilityCache()


class ScannerClient:
    def __init__(
        self,
        config: dict[str, Any],
        log: LogHub,
        timings: TimingStore | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.log = log
        self.timings = timings
        self.cancel = cancel or threading.Event()
        self.verify_ssl = config["verify_scanner_ssl"]
        self._job_url: str | None = None
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def abort(self) -> None:
        """Stop the run. Safe to call from another thread.

        Deleting the job on the device is what actually interrupts it: the
        request waiting for the next page cannot be cancelled from here, but it
        ends as soon as the scanner drops the job.
        """
        self.cancel.set()
        job_url = self._job_url
        if job_url:
            self._delete_job(job_url)

    def _stop_if_cancelled(self, pages: list[Path] | None = None) -> None:
        if self.cancel.is_set():
            self._discard(pages or [])
            raise ScanCancelled("Scan abgebrochen.")

    @staticmethod
    def _discard(pages: list[Path]) -> None:
        for path in pages:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @property
    def base_url(self) -> str:
        url = self.config.get("scanner_url", "")
        if not url:
            raise RuntimeError("Kein Scanner konfiguriert. Bitte im Einrichtungsassistenten hinterlegen.")
        return url

    def _get(self, endpoint: str, timeout: int = 20) -> requests.Response:
        return scanner_session.get(
            f"{self.base_url}/eSCL/{endpoint}",
            verify=self.verify_ssl,
            timeout=timeout,
        )

    def capabilities(self, quiet: bool = False) -> dict[str, Any]:
        if not quiet:
            self.log.publish("Lese ScannerCapabilities …")
        response = self._get("ScannerCapabilities")
        response.raise_for_status()
        result = parse_capabilities(response.text)
        capability_cache.put(self.base_url, result)
        if not quiet:
            extras = " · beidseitig" if result["duplex"] else ""
            self.log.publish(
                f"Scanner erreichbar · eSCL {result['version']} · "
                f"Quellen: {', '.join(result['sources']) or 'unbekannt'}{extras}",
                "success",
            )
        return result

    def known_capabilities(self) -> dict[str, Any]:
        """Cached capabilities, fetched on demand. Never raises."""
        cached = capability_cache.get(self.base_url)
        if cached is not None:
            return cached
        try:
            return self.capabilities(quiet=True)
        except (requests.RequestException, ET.ParseError, RuntimeError):
            return {}  # an unreadable device is sent the settings unchanged

    def status(self) -> str:
        return self.full_status()["state"]

    def full_status(self) -> dict[str, str]:
        """Device state plus what the sheet feeder reports about its tray."""
        response = self._get("ScannerStatus")
        response.raise_for_status()
        root = ET.fromstring(response.text)
        return {
            "state": root.findtext(f".//{{{PWG_NS}}}State", default="Unknown"),
            # Missing entirely on devices that have no feeder at all.
            "adf": root.findtext(f".//{{{SCAN_NS}}}AdfState", default=""),
        }

    def scan(self) -> list[Path]:
        """Run one scan job and return every sheet it produced.

        The flatbed answers with a single document; the sheet feeder keeps
        handing out one per sheet until it runs empty. Fetching NextDocument
        just once — as this did before — silently threw away every page after
        the first.
        """
        source = self.config["source"]
        started = time.monotonic()
        self.log.progress("connect", 8)
        status = self.full_status()
        self.log.publish(f"Scannerstatus: {status['state']}"
                         + (f" · Einzug: {ADF_LABELS.get(status['adf'], status['adf'])}" if status["adf"] else ""))
        if status["state"] != "Idle":
            raise RuntimeError(f"Scanner ist nicht bereit (Status: {status['state']}).")
        if source == "Feeder":
            # Asked with an empty tray, this device answers the job with a bare
            # HTTP 409 that reads like a settings problem. Better to say plainly
            # what is missing before a job is even created.
            self._require_loaded_feeder(status["adf"])

        self._stop_if_cancelled()
        job_url = self._create_job()
        self._job_url = job_url

        # The scanner streams for as long as the paper takes. With a measured
        # duration for this profile the bar tracks real time instead of guessing.
        timing_key = TimingStore.key(self.config)
        expected = self.timings.expected(timing_key) if self.timings else None
        if expected:
            self.log.publish(f"Erwartete Dauer je Seite für dieses Profil: ~{expected:.0f}s")

        capture_started = time.monotonic()
        pages: list[Path] = []
        try:
            with progress_ramp(self.log, "capture", start=25, end=78, expected=expected) as ramp:
                pages = self._collect_pages(job_url, ramp)
        except requests.RequestException:
            # A deleted job tears down the request that was waiting for a page.
            self._stop_if_cancelled(pages)
            raise
        finally:
            self._job_url = None
            self._delete_job(job_url)
        self._stop_if_cancelled(pages)

        if not pages:
            raise RuntimeError("Der Scanner hat kein Dokument geliefert.")
        if self.timings:
            self.timings.record(timing_key, (time.monotonic() - capture_started) / len(pages))

        total_bytes = sum(path.stat().st_size for path in pages)
        sheets = f"{len(pages)} Blatt" if len(pages) > 1 else pages[0].name
        self.log.publish(
            f"Scan gespeichert: {sheets} ({total_bytes:,} Bytes, {time.monotonic() - started:.1f}s)",
            "success",
        )
        return pages

    def _create_job(self) -> str:
        self.log.progress("job", 18)
        duplex = " · beidseitig" if self.config.get("duplex") else ""
        self.log.publish(
            f"Starte {self.config['source']}-Scan mit {self.config['resolution']} dpi "
            f"({self.config.get('paper_size', 'A4')}{duplex}) …"
        )
        response = scanner_session.post(
            f"{self.base_url}/eSCL/ScanJobs",
            data=self._build_settings().encode("utf-8"),
            headers={"Content-Type": "text/xml", "Accept": "*/*"},
            verify=self.verify_ssl,
            timeout=30,
        )
        if response.status_code != 201:
            raise RuntimeError(self._rejection_reason(response.status_code))
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("ScanJob wurde ohne Location-Header erstellt.")
        self.log.publish("ScanJob angenommen; warte auf das Dokument …", "success")
        return urljoin(f"{self.base_url}/", location)

    def _require_loaded_feeder(self, adf_state: str) -> None:
        if adf_state in ADF_READY:
            return
        # Short enough to read at a glance on a phone; the log carries the detail.
        if adf_state == ADF_EMPTY:
            self.log.publish(
                "Bleibt es bei „leer“, obwohl Papier eingelegt ist, hat dieses Gerät "
                "vermutlich keinen Einzug — dann bitte das Vorlagenglas nutzen.",
                "warning",
            )
            raise RuntimeError("Einzug ist leer — bitte Papier einlegen.")
        if adf_state == ADF_JAM:
            raise RuntimeError("Papierstau im Einzug — bitte Papier entfernen.")
        if not adf_state:
            # No AdfState at all: the device does not report a feeder, so a
            # feeder job would fail with something far less readable.
            raise RuntimeError("Dieser Scanner hat keinen Einzug — bitte Vorlagenglas wählen.")

    def _rejection_reason(self, status_code: int) -> str:
        """Say what the device meant. "HTTP 409" alone helps nobody."""
        source = self.config["source"]
        if status_code == 409:
            if source == "Feeder":
                # Measured on an HP DeskJet 4100: with an empty tray every single
                # feeder job is answered 409, whatever the settings say. Blaming
                # the paper format there would send people hunting in the wrong place.
                return (
                    "Der Einzug hat den Auftrag abgelehnt (HTTP 409). Das heißt bei diesen "
                    "Geräten fast immer: Es liegt kein Papier im Einzug, es wurde nicht weit "
                    "genug eingeschoben, oder der Drucker hat gar keinen Einzug."
                )
            usable = paper_sizes_for((self.known_capabilities().get("limits") or {}).get(source) or {})
            hint = f" Dieses Gerät kann hier: {', '.join(usable)}." if usable else ""
            return (
                "Der Scanner lehnt mindestens eine Einstellung ab (HTTP 409). "
                "Meist passen Papierformat oder Auflösung nicht zum Vorlagenglas." + hint
            )
        if status_code == 503:
            if source == "Feeder":
                return ("Der Scanner ist nicht bereit (HTTP 503). Liegt Papier im Einzug? "
                        "Ein leerer Einzug meldet sich genau so.")
            return "Der Scanner ist gerade belegt (HTTP 503). Bitte kurz warten und erneut versuchen."
        if status_code == 401:
            return "Der Scanner verlangt eine Anmeldung (HTTP 401)."
        return f"ScanJob abgelehnt (HTTP {status_code})."

    def _collect_pages(self, job_url: str, ramp: progress_ramp) -> list[Path]:
        """Fetch NextDocument until the scanner says there is no more paper."""
        limit = 1 if self.config["source"] == "Platen" else MAX_FEEDER_PAGES
        output_dir = Path(self.config["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        pages: list[Path] = []

        while len(pages) < limit:
            # Between sheets is the natural place to stop a feeder run.
            # Cancelling means this scan did not happen, so half a document is
            # not left behind for someone to find in Paperless later.
            if self.cancel.is_set():
                self._discard(pages)
                raise ScanCancelled("Scan abgebrochen.")
            response = scanner_session.get(
                f"{job_url.rstrip('/')}/NextDocument",
                headers={"Accept": "image/jpeg, application/pdf, application/octet-stream"},
                verify=self.verify_ssl,
                timeout=FIRST_PAGE_TIMEOUT if not pages else NEXT_PAGE_TIMEOUT,
            )
            # An empty feeder is the normal end of a run, not a failure — but
            # the very first page failing to arrive is one.
            if response.status_code in (404, 410, 204) or not response.content:
                if pages:
                    break
                if response.status_code in (404, 410, 204):
                    raise RuntimeError(
                        "Der Scanner hat keine Seite geliefert. "
                        "Liegt Papier im Einzug beziehungsweise auf dem Glas?"
                    )
                raise RuntimeError("Der Scanner hat ein leeres Dokument geliefert.")
            response.raise_for_status()

            suffix = self._suffix_for(response.headers.get("Content-Type", ""))
            name = f"scan_{stamp}{suffix}" if limit == 1 else f"scan_{stamp}_blatt-{len(pages) + 1:02d}{suffix}"
            target = output_dir / name
            target.write_bytes(response.content)
            pages.append(target)
            ramp.note(pages=len(pages))
            if limit > 1:
                self.log.publish(f"Blatt {len(pages)} eingezogen ({len(response.content):,} Bytes).")

        return pages

    def _delete_job(self, job_url: str) -> None:
        """Release the job so the next scan is not refused as "busy"."""
        try:
            scanner_session.delete(job_url, verify=self.verify_ssl, timeout=10)
        except requests.RequestException:
            pass  # the device drops finished jobs on its own soon enough

    def _suffix_for(self, content_type: str) -> str:
        content_type = content_type.lower()
        if "pdf" in content_type:
            return ".pdf"
        if "jpeg" in content_type or "jpg" in content_type:
            return ".jpg"
        return ".pdf" if self.config["output_format"] == "application/pdf" else ".jpg"

    def fit_to_device(self) -> dict[str, Any]:
        """Bend the requested settings into what this device accepts.

        Asking a flatbed for Legal, or a sheet feeder for 600 dpi it does not
        have, is answered with a bare "HTTP 409" that tells a user nothing. So
        the limits are read first and the request is trimmed to fit — and every
        trim is said out loud, because silently scanning something else than
        what was asked for would be worse than the error.
        """
        source = self.config["source"]
        size_name = self.config.get("paper_size", "A4")
        width, height = PAPER_SIZES.get(size_name, PAPER_SIZES["A4"])
        resolution = int(self.config["resolution"])
        duplex = bool(self.config.get("duplex")) and source == "Feeder"

        capabilities = self.known_capabilities()
        if not capabilities:
            return {"width": width, "height": height, "resolution": resolution, "duplex": duplex}

        limits = (capabilities.get("limits") or {}).get(source) or {}
        max_width, max_height = limits.get("max_width"), limits.get("max_height")
        if max_width and width > max_width or max_height and height > max_height:
            usable = paper_sizes_for(limits)
            width = min(width, max_width or width)
            height = min(height, max_height or height)
            self.log.publish(
                f"{size_name} passt nicht auf {'das Vorlagenglas' if source == 'Platen' else 'den Einzug'}; "
                f"es wird auf die größte mögliche Fläche zugeschnitten. "
                f"Vollständig unterstützt: {', '.join(usable) or 'keines der angebotenen Formate'}.",
                "warning",
            )

        max_resolution = limits.get("max_resolution")
        if max_resolution and resolution > max_resolution:
            self.log.publish(
                f"{resolution} dpi kann diese Quelle nicht; es wird mit {max_resolution} dpi gescannt.",
                "warning",
            )
            resolution = max_resolution

        if duplex and not capabilities.get("duplex"):
            self.log.publish(
                "Dieser Scanner kann nicht beidseitig einziehen; es wird einseitig gescannt.",
                "warning",
            )
            duplex = False

        return {"width": width, "height": height, "resolution": resolution, "duplex": duplex}

    def _build_settings(self) -> str:
        source = self.config["source"]
        fitted = self.fit_to_device()
        width, height, resolution = fitted["width"], fitted["height"], fitted["resolution"]
        duplex = ""
        if source == "Feeder":
            duplex = f"\n  <scan:Duplex>{'true' if fitted['duplex'] else 'false'}</scan:Duplex>"
        document_format = self.config["output_format"]
        # This HP firmware rejects the otherwise equivalent, heavily indented
        # form. Keep this proven interoperable eSCL layout compact.
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:pwg="{PWG_NS}" xmlns:scan="{SCAN_NS}">
  <pwg:Version>{REQUEST_ESCL_VERSION}</pwg:Version>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>{source}</pwg:InputSource>
  <scan:ColorMode>{self.config['color_mode']}</scan:ColorMode>
  <pwg:DocumentFormat>{document_format}</pwg:DocumentFormat>
  <scan:DocumentFormatExt>{document_format}</scan:DocumentFormatExt>
  <scan:XResolution>{resolution}</scan:XResolution>
  <scan:YResolution>{resolution}</scan:YResolution>{duplex}
</scan:ScanSettings>"""


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _collect(node: ET.Element, tag: str) -> list[str]:
    """Every distinct value of a tag below this node, order preserved."""
    seen: dict[str, None] = {}
    for element in node.iter():
        if _local(element) == tag and (element.text or "").strip():
            seen[element.text.strip()] = None
    return list(seen)


def _source_limits(node: ET.Element | None) -> dict[str, Any]:
    """Everything one input source can do, as the device describes it."""
    if node is None:
        return {}
    resolutions = sorted({
        value for value in (_int_or_none(text) for text in _collect(node, "XResolution")) if value
    })
    max_optical = _int_or_none(node.findtext(f"{{{SCAN_NS}}}MaxOpticalXResolution"))
    return {
        "max_width": _int_or_none(node.findtext(f"{{{SCAN_NS}}}MaxWidth")),
        "max_height": _int_or_none(node.findtext(f"{{{SCAN_NS}}}MaxHeight")),
        "max_resolution": max_optical or (resolutions[-1] if resolutions else None),
        "resolutions": resolutions,
        "color_modes": _collect(node, "ColorMode"),
        # Both spellings list the same thing; octet-stream is a fallback, not
        # a format anyone would choose.
        "formats": [value for value in
                    dict.fromkeys(_collect(node, "DocumentFormat") + _collect(node, "DocumentFormatExt"))
                    if value != "application/octet-stream"],
    }


def millimetres(units: int | None) -> int | None:
    """eSCL measures in 1/300 inch; people measure in millimetres."""
    return round(units / 300 * 25.4) if units else None


def parse_capabilities(xml: str) -> dict[str, Any]:
    """What the device says it can do, reduced to what the interface offers."""
    root = ET.fromstring(xml)
    platen = root.find(f".//{{{SCAN_NS}}}PlatenInputCaps")
    feeder = root.find(f".//{{{SCAN_NS}}}AdfSimplexInputCaps")
    sources = []
    if platen is not None:
        sources.append("Platen")
    if feeder is not None:
        sources.append("Feeder")
    return {
        "version": root.findtext(f".//{{{PWG_NS}}}Version", default="unbekannt"),
        "model": root.findtext(f".//{{{PWG_NS}}}MakeAndModel", default="eSCL-Scanner"),
        "sources": sources,
        "duplex": root.find(f".//{{{SCAN_NS}}}AdfDuplexInputCaps") is not None,
        "limits": {"Platen": _source_limits(platen), "Feeder": _source_limits(feeder)},
    }


def paper_sizes_for(limits: dict[str, Any]) -> list[str]:
    """Which of the offered paper sizes this source can actually cover."""
    if not limits or not limits.get("max_height"):
        return list(PAPER_SIZES)
    return [
        name for name, (width, height) in PAPER_SIZES.items()
        if width <= (limits.get("max_width") or width) and height <= limits["max_height"]
    ]

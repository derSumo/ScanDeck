"""Talking eSCL to the scanner: capabilities, status and the scan itself."""

from __future__ import annotations

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


class ScannerClient:
    def __init__(self, config: dict[str, Any], log: LogHub, timings: TimingStore | None = None) -> None:
        self.config = config
        self.log = log
        self.timings = timings
        self.verify_ssl = config["verify_scanner_ssl"]
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

    def capabilities(self) -> dict[str, Any]:
        self.log.publish("Lese ScannerCapabilities …")
        response = self._get("ScannerCapabilities")
        response.raise_for_status()
        result = parse_capabilities(response.text)
        extras = " · beidseitig" if result["duplex"] else ""
        self.log.publish(
            f"Scanner erreichbar · eSCL {result['version']} · "
            f"Quellen: {', '.join(result['sources']) or 'unbekannt'}{extras}",
            "success",
        )
        return result

    def status(self) -> str:
        response = self._get("ScannerStatus")
        response.raise_for_status()
        root = ET.fromstring(response.text)
        return root.findtext(f".//{{{PWG_NS}}}State", default="Unknown")

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
        status = self.status()
        self.log.publish(f"Scannerstatus: {status}")
        if status != "Idle":
            raise RuntimeError(f"Scanner ist nicht bereit (Status: {status}).")

        job_url = self._create_job()

        # The scanner streams for as long as the paper takes. With a measured
        # duration for this profile the bar tracks real time instead of guessing.
        timing_key = TimingStore.key(self.config)
        expected = self.timings.expected(timing_key) if self.timings else None
        if expected:
            self.log.publish(f"Erwartete Dauer je Seite für dieses Profil: ~{expected:.0f}s")

        capture_started = time.monotonic()
        try:
            with progress_ramp(self.log, "capture", start=25, end=78, expected=expected) as ramp:
                pages = self._collect_pages(job_url, ramp)
        finally:
            self._delete_job(job_url)

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
            raise RuntimeError(f"ScanJob abgelehnt (HTTP {response.status_code}).")
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("ScanJob wurde ohne Location-Header erstellt.")
        self.log.publish("ScanJob angenommen; warte auf das Dokument …", "success")
        return urljoin(f"{self.base_url}/", location)

    def _collect_pages(self, job_url: str, ramp: progress_ramp) -> list[Path]:
        """Fetch NextDocument until the scanner says there is no more paper."""
        limit = 1 if self.config["source"] == "Platen" else MAX_FEEDER_PAGES
        output_dir = Path(self.config["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        pages: list[Path] = []

        while len(pages) < limit:
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

    def _build_settings(self) -> str:
        source = self.config["source"]
        width, height = PAPER_SIZES.get(self.config.get("paper_size", "A4"), PAPER_SIZES["A4"])
        duplex = ""
        if source == "Feeder":
            duplex = f"\n  <scan:Duplex>{'true' if self.config.get('duplex') else 'false'}</scan:Duplex>"
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
  <scan:XResolution>{self.config['resolution']}</scan:XResolution>
  <scan:YResolution>{self.config['resolution']}</scan:YResolution>{duplex}
</scan:ScanSettings>"""


def parse_capabilities(xml: str) -> dict[str, Any]:
    """What the device says it can do, reduced to what the interface offers."""
    root = ET.fromstring(xml)
    sources = []
    if root.find(f".//{{{SCAN_NS}}}PlatenInputCaps") is not None:
        sources.append("Platen")
    if root.find(f".//{{{SCAN_NS}}}AdfSimplexInputCaps") is not None:
        sources.append("Feeder")
    return {
        "version": root.findtext(f".//{{{PWG_NS}}}Version", default="unbekannt"),
        "model": root.findtext(f".//{{{PWG_NS}}}MakeAndModel", default="eSCL-Scanner"),
        "sources": sources,
        "duplex": root.find(f".//{{{SCAN_NS}}}AdfDuplexInputCaps") is not None,
    }

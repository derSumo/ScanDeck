"""Paperless Scanner Hub: eSCL scan orchestration and Paperless-ngx upload."""

from __future__ import annotations

import io
import json
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import wraps
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
import urllib3
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
)


def read_version() -> str:
    """Single source of truth for the semantic version: the VERSION file."""
    try:
        return Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


APP_VERSION = read_version()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "derSumo/ScanDeck")
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
UPDATE_CACHE_SECONDS = 6 * 3600
APP_DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
CONFIG_PATH = APP_DATA_DIR / "config.json"
BATCH_DIR = APP_DATA_DIR / "batch"
DEFAULT_OUTPUT_DIR = os.environ.get("SCAN_OUTPUT_DIR", "/scans")
PWG_NS = "http://www.pwg.org/schemas/2010/12/sm"
SCAN_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"
REQUEST_ESCL_VERSION = "2.0"


DEFAULT_CONFIG: dict[str, Any] = {
    # Everything a user has to decide starts out empty so the setup wizard owns
    # the first run instead of shipping someone else's network as a default.
    "scanner_url": os.environ.get("SCANNER_URL", ""),
    "verify_scanner_ssl": False,
    "paperless_url": os.environ.get("PAPERLESS_URL", ""),
    "paperless_token": os.environ.get("PAPERLESS_TOKEN", ""),
    "output_dir": DEFAULT_OUTPUT_DIR,
    "default_tags": [],
    "create_missing_tags": True,
    "discovery_subnet": os.environ.get("DISCOVERY_SUBNET", ""),
    "source": "Platen",
    "resolution": 300,
    "color_mode": "RGB24",
    "output_format": "application/pdf",
    "upload_to_paperless": False,
    "title_prefix": "Scan",
    "preview_seconds": 10,
    "update_check": True,
    "setup_complete": False,
    "ha_enabled": False,
    "ha_api_key": "",
    "ha_webhook_url": "",
}

SECRET_KEYS = ("paperless_token", "ha_api_key")

ALLOWED_SOURCES = {"Platen", "Feeder"}
ALLOWED_RESOLUTIONS = {75, 100, 150, 200, 300, 600, 1200}
ALLOWED_COLOR_MODES = {"RGB24", "Grayscale8", "BlackAndWhite1"}
ALLOWED_FORMATS = {"image/jpeg", "application/pdf"}


class LogHub:
    """A small in-memory event fan-out for the single-process Docker service."""

    def __init__(self) -> None:
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._lock = threading.Lock()
        self.history: list[dict[str, Any]] = []

    def publish(self, message: str, level: str = "info") -> None:
        self._emit({
            "kind": "log",
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })

    def progress(self, stage: str, percent: int, **extra: Any) -> None:
        """Push a scan progress tick to every connected client."""
        percent = max(0, min(100, int(percent)))
        scan_state["stage"] = stage
        scan_state["progress"] = percent
        self._emit({"kind": "progress", "stage": stage, "progress": percent, **extra})

    def _emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            if event["kind"] == "log":
                self.history.append(event)
                self.history = self.history[-200:]
            for subscriber in self._subscribers.copy():
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    pass

    def stream(self) -> Iterator[str]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(subscriber)
            history = self.history.copy()
        try:
            for event in history:
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = subscriber.get(timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            merged = {**DEFAULT_CONFIG, **stored}
            # Configs written before the wizard existed are already set up.
            if "setup_complete" not in stored:
                merged["setup_complete"] = bool(merged.get("scanner_url"))
            return merged
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return DEFAULT_CONFIG.copy()

    def exists(self) -> bool:
        return self.path.exists()

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._config.copy()

    def public(self) -> dict[str, Any]:
        return self._public_from_config(self.get())

    @staticmethod
    def _public_from_config(config: dict[str, Any]) -> dict[str, Any]:
        public_config = config.copy()
        public_config["paperless_token_configured"] = bool(public_config.pop("paperless_token", ""))
        # The Home Assistant key has to be readable once so users can copy it
        # into their automation; it is generated locally and never leaves the LAN.
        public_config["ha_api_key_configured"] = bool(public_config.get("ha_api_key"))
        return public_config

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = {**self._config}
            for key in DEFAULT_CONFIG:
                if key in payload and key not in SECRET_KEYS:
                    updated[key] = payload[key]
            for key in SECRET_KEYS:
                if key in payload and str(payload[key]).strip():
                    updated[key] = str(payload[key]).strip()
            updated = validate_config(updated)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
            self._config = updated
        return self._public_from_config(updated)

    def patch(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            updated = validate_config({**self._config, **values})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
            self._config = updated
        return self._public_from_config(updated)


def normalise_url(value: str, field_name: str, required: bool = False) -> str:
    value = str(value or "").strip().rstrip("/")
    if not value and required:
        raise ValueError(f"{field_name} darf nicht leer sein.")
    if value and not re.match(r"^https?://", value, re.IGNORECASE):
        raise ValueError(f"{field_name} muss mit http:// oder https:// beginnen.")
    return value


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    # Empty endpoints stay valid: an unconfigured instance must be storable so
    # the wizard can save progress step by step.
    config["scanner_url"] = normalise_url(config.get("scanner_url", ""), "Scanner-URL")
    config["paperless_url"] = normalise_url(config.get("paperless_url", ""), "Paperless-URL")
    config["ha_webhook_url"] = normalise_url(config.get("ha_webhook_url", ""), "Home-Assistant-Webhook")
    config["discovery_subnet"] = str(config.get("discovery_subnet", "")).strip()
    config["verify_scanner_ssl"] = bool(config.get("verify_scanner_ssl"))
    config["source"] = config.get("source", "Platen")
    config["resolution"] = int(config.get("resolution", 300))
    config["color_mode"] = config.get("color_mode", "RGB24")
    config["output_format"] = config.get("output_format", "application/pdf")
    config["upload_to_paperless"] = bool(config.get("upload_to_paperless"))
    config["create_missing_tags"] = bool(config.get("create_missing_tags", True))
    config["title_prefix"] = str(config.get("title_prefix", "Scan")).strip() or "Scan"
    config["update_check"] = bool(config.get("update_check"))
    config["setup_complete"] = bool(config.get("setup_complete"))
    config["ha_enabled"] = bool(config.get("ha_enabled"))
    config["ha_api_key"] = str(config.get("ha_api_key", "")).strip()
    config["preview_seconds"] = max(0, min(60, int(config.get("preview_seconds", 10))))

    if config["source"] not in ALLOWED_SOURCES:
        raise ValueError("Unbekannte Scanquelle.")
    if config["resolution"] not in ALLOWED_RESOLUTIONS:
        raise ValueError("Nicht unterstützte Auflösung.")
    if config["color_mode"] not in ALLOWED_COLOR_MODES:
        raise ValueError("Nicht unterstützter Farbmodus.")
    if config["output_format"] not in ALLOWED_FORMATS:
        raise ValueError("Nicht unterstütztes Ausgabeformat.")

    if config["discovery_subnet"]:
        try:
            discovery_network = ip_network(config["discovery_subnet"], strict=False)
        except ValueError as error:
            raise ValueError("Netzwerk für die Scanner-Suche ist ungültig.") from error
        if (
            not isinstance(discovery_network, IPv4Network)
            or not discovery_network.is_private
            or discovery_network.num_addresses > 256
        ):
            raise ValueError("Die Scanner-Suche akzeptiert nur private IPv4-Netze bis /24.")
        config["discovery_subnet"] = str(discovery_network)

    output_dir = Path(str(config.get("output_dir") or DEFAULT_OUTPUT_DIR)).expanduser()
    if not output_dir.is_absolute():
        raise ValueError("Scan-Ausgabeverzeichnis muss ein absoluter Pfad im Container sein.")
    output_dir.mkdir(parents=True, exist_ok=True)
    config["output_dir"] = str(output_dir)

    tags = config.get("default_tags", [])
    if not isinstance(tags, list):
        raise ValueError("Standard-Tags müssen eine Liste sein.")
    config["default_tags"] = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    return config


def parse_version(value: str) -> tuple[int, ...]:
    """Turn "v1.2.3" into (1, 2, 3); anything unparsable sorts lowest."""
    core = re.split(r"[-+]", str(value or "").strip().lstrip("vV"), maxsplit=1)[0]
    parts = [int(part) for part in re.findall(r"\d+", core)[:3]]
    return tuple(parts + [0] * (3 - len(parts))) if parts else (0, 0, 0)


update_cache: dict[str, Any] = {"checked_at": 0.0, "latest": "", "url": RELEASES_URL, "error": ""}
update_lock = threading.Lock()


def check_for_update(force: bool = False) -> dict[str, Any]:
    """Ask GitHub for the newest release, at most once every few hours."""
    with update_lock:
        fresh = time.time() - update_cache["checked_at"] < UPDATE_CACHE_SECONDS
        if not fresh or force:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": f"ScanDeck/{APP_VERSION}"}
            try:
                response = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                    headers=headers,
                    timeout=8,
                )
                if response.status_code == 404:
                    # No published release yet: the newest semver tag is just as good.
                    tags = requests.get(
                        f"https://api.github.com/repos/{GITHUB_REPO}/tags?per_page=100",
                        headers=headers,
                        timeout=8,
                    )
                    tags.raise_for_status()
                    names = [str(tag.get("name", "")) for tag in tags.json()]
                    newest = max(names, key=parse_version, default="")
                    update_cache.update({
                        "latest": newest.lstrip("vV"),
                        "url": f"{RELEASES_URL}/tag/{newest}" if newest else RELEASES_URL,
                        "error": "",
                    })
                else:
                    response.raise_for_status()
                    body = response.json()
                    update_cache.update({
                        "latest": str(body.get("tag_name") or body.get("name") or "").lstrip("vV"),
                        "url": body.get("html_url") or RELEASES_URL,
                        "error": "",
                    })
            except (requests.RequestException, ValueError) as error:
                update_cache["error"] = str(error)
            update_cache["checked_at"] = time.time()

        latest = update_cache["latest"]
        return {
            "current": APP_VERSION,
            "latest": latest,
            "update_available": bool(latest) and parse_version(latest) > parse_version(APP_VERSION),
            "url": update_cache["url"],
            "checked_at": update_cache["checked_at"],
            "error": update_cache["error"],
        }


def guess_local_subnet() -> str:
    """Best guess for the /24 the container lives in, used to prefill the wizard."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()
    return subnet_of(address)


def subnet_of(address: str) -> str:
    """The /24 an IPv4 address belongs to, or "" if it is not a usable private one."""
    try:
        host = ip_address(str(address or "").strip())
    except ValueError:
        return ""
    if not isinstance(host, IPv4Address) or not host.is_private or host.is_loopback or host.is_link_local:
        return ""
    return str(ip_network(f"{host}/24", strict=False))


def is_container_network(subnet: str) -> bool:
    """Docker's own bridge ranges never hold the scanner, so they rank last."""
    try:
        network = ip_network(subnet, strict=False)
    except ValueError:
        return True
    return any(network.subnet_of(ip_network(pool)) for pool in ("172.17.0.0/16", "172.18.0.0/15", "10.88.0.0/16"))


# Typical home router defaults, tried when nothing better is known.
COMMON_SUBNETS = (
    "192.168.0.0/24",
    "192.168.1.0/24",
    "192.168.178.0/24",  # AVM Fritz!Box
    "192.168.2.0/24",
    "10.0.0.0/24",
)


def candidate_subnets(config: dict[str, Any], client_address: str | None = None) -> list[str]:
    """Rank the networks worth probing, best hint first.

    The device that opens the interface sits on the same LAN as the scanner, so
    its address beats everything the container can see about itself: in Docker's
    default bridge mode the container only knows its 172.x network.
    """
    candidates: list[str] = []
    fallbacks: list[str] = []

    def add(subnet: str) -> None:
        # Container networks never hold the scanner, so they go to the very end
        # instead of wasting the first search round.
        target = fallbacks if is_container_network(subnet) else candidates
        if subnet and subnet not in candidates and subnet not in fallbacks:
            target.append(subnet)

    add(subnet_of(client_address or ""))
    for url in (config.get("paperless_url", ""), config.get("scanner_url", "")):
        host = urlparse(url).hostname if url else None
        if host:
            add(subnet_of(host))
    add(guess_local_subnet())
    for subnet in COMMON_SUBNETS:
        add(subnet)
    return candidates + fallbacks


# One session for every talk to the scanner. Each scan needs three requests
# (status, job, document) and HTTPS costs a full TLS handshake per connection —
# keeping it alive saves that handshake within a scan and between scans.
scanner_session = requests.Session()
scanner_session.headers.update({"User-Agent": f"ScanDeck/{APP_VERSION}", "Connection": "keep-alive"})
scanner_session.mount(
    "https://",
    requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0),
)
scanner_session.mount(
    "http://",
    requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0),
)


class TimingStore:
    """Remembers how long a scan takes per profile so the bar can be honest.

    A 600 dpi colour scan takes many times longer than 300 dpi grayscale, so one
    average would be useless — every combination gets its own running mean.
    """

    SMOOTHING = 0.4  # weight of the newest measurement

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, dict[str, float]]:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            return stored if isinstance(stored, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def key(config: dict[str, Any]) -> str:
        return "|".join(str(config.get(field, "")) for field in
                        ("source", "resolution", "color_mode", "output_format"))

    def expected(self, key: str) -> float | None:
        with self._lock:
            entry = self._data.get(key)
        # A single measurement can be an outlier (cold lamp, sleeping device).
        return float(entry["seconds"]) if entry and entry.get("samples", 0) >= 1 else None

    def record(self, key: str, seconds: float) -> None:
        if seconds <= 0 or seconds > 3600:
            return
        with self._lock:
            entry = self._data.get(key)
            if entry:
                entry["seconds"] = round(entry["seconds"] * (1 - self.SMOOTHING) + seconds * self.SMOOTHING, 2)
                entry["samples"] = int(entry.get("samples", 1)) + 1
            else:
                entry = {"seconds": round(seconds, 2), "samples": 1}
            self._data[key] = entry
            snapshot = dict(self._data)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        except OSError:
            pass  # a read-only volume must not break the scan itself

    def summary(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return dict(self._data)


class ScannerClient:
    def __init__(self, config: dict[str, Any], log: LogHub) -> None:
        self.config = config
        self.log = log
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
        root = ET.fromstring(response.text)
        version = root.findtext(f".//{{{PWG_NS}}}Version", default="unbekannt")
        model = root.findtext(f".//{{{PWG_NS}}}MakeAndModel", default="eSCL-Scanner")
        sources = []
        if root.find(f".//{{{SCAN_NS}}}PlatenInputCaps") is not None:
            sources.append("Platen")
        if root.find(f".//{{{SCAN_NS}}}AdfSimplexInputCaps") is not None:
            sources.append("Feeder")
        self.log.publish(
            f"Scanner erreichbar · eSCL {version} · Quellen: {', '.join(sources) or 'unbekannt'}",
            "success",
        )
        return {"version": version, "model": model, "sources": sources}

    def status(self) -> str:
        response = self._get("ScannerStatus")
        response.raise_for_status()
        root = ET.fromstring(response.text)
        return root.findtext(f".//{{{PWG_NS}}}State", default="Unknown")

    def scan(self) -> Path:
        settings = self._build_settings()
        started = time.monotonic()
        self.log.progress("connect", 8)
        status = self.status()
        self.log.publish(f"Scannerstatus: {status}")
        if status != "Idle":
            raise RuntimeError(f"Scanner ist nicht bereit (Status: {status}).")

        self.log.progress("job", 18)
        self.log.publish(f"Starte {self.config['source']}-Scan mit {self.config['resolution']} dpi …")
        response = scanner_session.post(
            f"{self.base_url}/eSCL/ScanJobs",
            data=settings.encode("utf-8"),
            headers={"Content-Type": "text/xml", "Accept": "*/*"},
            verify=self.verify_ssl,
            timeout=30,
        )
        if response.status_code != 201:
            raise RuntimeError(f"ScanJob abgelehnt (HTTP {response.status_code}).")

        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("ScanJob wurde ohne Location-Header erstellt.")
        job_url = urljoin(f"{self.base_url}/", location)
        self.log.publish("ScanJob angenommen; warte auf das Dokument …", "success")

        # The scanner streams for as long as the paper takes. With a measured
        # duration for this profile the bar tracks real time instead of guessing.
        timing_key = TimingStore.key(self.config)
        expected = timings.expected(timing_key)
        if expected:
            self.log.publish(f"Erwartete Dauer für dieses Profil: ~{expected:.0f}s")
        capture_started = time.monotonic()
        with progress_ramp(self.log, "capture", start=25, end=78, expected=expected):
            document = scanner_session.get(
                f"{job_url.rstrip('/')}/NextDocument",
                headers={"Accept": "image/jpeg, application/pdf, application/octet-stream"},
                verify=self.verify_ssl,
                timeout=180,
            )
            document.raise_for_status()
        if not document.content:
            raise RuntimeError("Der Scanner hat ein leeres Dokument geliefert.")
        timings.record(timing_key, time.monotonic() - capture_started)

        self.log.progress("store", 82)
        content_type = document.headers.get("Content-Type", "").lower()
        extension = ".pdf" if "pdf" in content_type or self.config["output_format"] == "application/pdf" else ".jpg"
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output_dir = Path(self.config["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"scan_{timestamp}{extension}"
        target.write_bytes(document.content)
        self.log.publish(
            f"Scan gespeichert: {target.name} ({len(document.content):,} Bytes, "
            f"{time.monotonic() - started:.1f}s)",
            "success",
        )
        return target

    def _build_settings(self) -> str:
        source = self.config["source"]
        duplex = "\n  <scan:Duplex>false</scan:Duplex>" if source == "Feeder" else ""
        document_format = self.config["output_format"]
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:pwg="{PWG_NS}" xmlns:scan="{SCAN_NS}">
  <pwg:Version>{REQUEST_ESCL_VERSION}</pwg:Version>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>2480</pwg:Width>
      <pwg:Height>3508</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>{source}</pwg:InputSource>
  <scan:ColorMode>{self.config['color_mode']}</scan:ColorMode>
  <pwg:DocumentFormat>{document_format}</pwg:DocumentFormat>
  <scan:DocumentFormatExt>{document_format}</scan:DocumentFormatExt>
  <scan:XResolution>{self.config['resolution']}</scan:XResolution>
  <scan:YResolution>{self.config['resolution']}</scan:YResolution>{duplex}
</scan:ScanSettings>"""


class progress_ramp:
    """Eases the progress bar forward while a blocking call is in flight.

    With a measured duration for this scan profile the bar moves in real time and
    can name the seconds left; without one it falls back to an asymptotic curve
    that never quite arrives.
    """

    def __init__(
        self,
        log: LogHub,
        stage: str,
        start: int,
        end: int,
        expected: float | None = None,
        step_seconds: float = 0.5,
    ) -> None:
        self.log = log
        self.stage = stage
        self.start = start
        self.end = end
        self.expected = expected if expected and expected > 0 else None
        self.step_seconds = step_seconds
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "progress_ramp":
        self.log.progress(self.stage, self.start, eta=round(self.expected) if self.expected else None)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        started = time.monotonic()
        current = float(self.start)
        span = self.end - self.start
        while not self._done.wait(self.step_seconds):
            elapsed = time.monotonic() - started
            if self.expected:
                share = elapsed / self.expected
                if share < 1:
                    current = self.start + span * share
                    eta = max(0, round(self.expected - elapsed))
                else:
                    # Slower than usual: creep on so the bar never looks stuck.
                    current += (self.end - current) * 0.08
                    eta = None
            else:
                current += (self.end - current) * 0.12
                eta = None
            self.log.progress(self.stage, round(min(current, self.end)), eta=eta)

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        if self._thread:
            self._thread.join(timeout=1)


def discover_escl_scanners(config: dict[str, Any], log: LogHub) -> list[dict[str, str]]:
    """Probe a bounded private IPv4 subnet for secure eSCL ScannerCapabilities."""
    subnet = config.get("discovery_subnet") or guess_local_subnet()
    if not subnet:
        raise ValueError("Kein Netzwerk für die Suche angegeben.")
    network = ip_network(subnet, strict=False)
    assert isinstance(network, IPv4Network)
    if not config["verify_scanner_ssl"]:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log.publish(f"Suche eSCL-Scanner in {network} …")

    def probe(address: str) -> dict[str, str] | None:
        base_url = f"https://{address}:443"
        try:
            # Avoid waiting for an HTTPS timeout on every unused address. The
            # actual capability request gets enough time for slower HP firmware.
            with socket.create_connection((address, 443), timeout=0.35):
                pass
            response = requests.get(
                f"{base_url}/eSCL/ScannerCapabilities",
                verify=config["verify_scanner_ssl"],
                timeout=7,
            )
            if response.status_code != 200:
                return None
            root = ET.fromstring(response.text)
            model = root.findtext(f".//{{{PWG_NS}}}MakeAndModel", default="eSCL-Scanner")
            version = root.findtext(f".//{{{PWG_NS}}}Version", default="?")
            return {"url": base_url, "model": model, "version": version}
        except (OSError, requests.RequestException, ET.ParseError):
            return None

    devices: list[dict[str, str]] = []
    addresses = [str(host) for host in network.hosts()]
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(probe, address) for address in addresses]
        for future in as_completed(futures):
            device = future.result()
            if device:
                devices.append(device)
    devices.sort(key=lambda device: device["url"])
    log.publish(
        f"Scanner-Suche abgeschlossen · {len(devices)} Gerät(e) gefunden.",
        "success" if devices else "warning",
    )
    return devices


def autodiscover_scanners(
    config: dict[str, Any],
    log: LogHub,
    client_address: str | None = None,
    limit: int = 4,
) -> tuple[list[dict[str, str]], str]:
    """Probe the likeliest networks in order and stop at the first hit."""
    candidates = candidate_subnets(config, client_address)[:limit]
    log.publish(f"Automatische Suche in: {', '.join(candidates)}")
    for subnet in candidates:
        devices = discover_escl_scanners({**config, "discovery_subnet": subnet}, log)
        if devices:
            return devices, subnet
    return [], ""


class PaperlessClient:
    def __init__(self, config: dict[str, Any], log: LogHub) -> None:
        self.config = config
        self.log = log
        self.base_url = config["paperless_url"].rstrip("/")
        self.headers = {"Authorization": f"Token {config['paperless_token']}"}

    def test(self) -> None:
        self._require_config()
        response = requests.get(f"{self.base_url}/api/documents/?page_size=1", headers=self.headers, timeout=20)
        response.raise_for_status()
        self.log.publish("Paperless-ngx erreichbar und Token akzeptiert.", "success")

    def upload(self, file_path: Path) -> None:
        self._require_config()
        tag_ids = self._resolve_tags()
        data: list[tuple[str, str]] = [("title", f"{self.config['title_prefix']} {datetime.now():%Y-%m-%d %H:%M}")]
        data.extend(("tags", str(tag_id)) for tag_id in tag_ids)
        self.log.publish(f"Lade {file_path.name} nach Paperless-ngx hoch …")
        with file_path.open("rb") as document:
            response = requests.post(
                f"{self.base_url}/api/documents/post_document/",
                headers=self.headers,
                data=data,
                files={"document": (file_path.name, document)},
                timeout=90,
            )
        response.raise_for_status()
        task_id = response.text.strip().strip('"')
        self.log.publish(f"Paperless-ngx übernimmt das Dokument (Task: {task_id or 'gestartet'}).", "success")

    def _require_config(self) -> None:
        if not self.base_url or not self.config.get("paperless_token"):
            raise RuntimeError("Paperless-URL oder API-Token fehlt.")

    def _resolve_tags(self) -> list[int]:
        wanted = self.config["default_tags"]
        if not wanted:
            return []
        response = requests.get(f"{self.base_url}/api/tags/?page_size=100", headers=self.headers, timeout=20)
        response.raise_for_status()
        body = response.json()
        known = body.get("results", body) if isinstance(body, dict) else body
        by_name = {str(tag.get("name", "")).casefold(): tag.get("id") for tag in known}
        tag_ids: list[int] = []
        for tag_name in wanted:
            tag_id = by_name.get(tag_name.casefold())
            if tag_id is None and self.config["create_missing_tags"]:
                created = requests.post(
                    f"{self.base_url}/api/tags/",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json={"name": tag_name, "color": "#c99b52"},
                    timeout=20,
                )
                created.raise_for_status()
                tag_id = created.json().get("id")
                self.log.publish(f"Paperless-Tag erstellt: {tag_name}")
            if tag_id is None:
                self.log.publish(f"Tag fehlt in Paperless und wurde übersprungen: {tag_name}", "warning")
            else:
                tag_ids.append(int(tag_id))
        return tag_ids


app = Flask(__name__)
logs = LogHub()
store = ConfigStore(CONFIG_PATH)
timings = TimingStore(APP_DATA_DIR / "timings.json")
scan_lock = threading.Lock()
scan_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "progress": 0,
    "last_file": None,
    "last_name": None,
    "last_kind": None,
    "last_finished": None,
    "last_error": None,
    "trigger": None,
}
preview_cache: dict[str, Any] = {"path": None, "image": None, "mimetype": "image/png"}
# Pages collected for a multi-document scan; "replace_index" arms the next scan
# to overwrite one page instead of appending.
batch_state: dict[str, Any] = {"active": False, "pages": [], "replace_index": None}
batch_lock = threading.Lock()


def check_writable(directory: Path, label: str) -> None:
    """Report an unwritable volume at boot instead of at the first save."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".scandeck-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        message = (
            f"{label} ({directory}) ist nicht beschreibbar: {error.strerror or error}. "
            "Bitte die Rechte des gemounteten Ordners pruefen oder PUID/PGID setzen."
        )
        logs.publish(message, "error")
        print(f"ScanDeck: {message}", file=sys.stderr, flush=True)


check_writable(APP_DATA_DIR, "Konfigurationsverzeichnis")
check_writable(Path(store.get()["output_dir"]), "Scan-Ablage")


# --------------------------------------------------------------------------- #
# Batch scanning: collect single pages, then merge them into one PDF
# --------------------------------------------------------------------------- #

def batch_public() -> dict[str, Any]:
    with batch_lock:
        return {
            "active": batch_state["active"],
            "replace_index": batch_state["replace_index"],
            "pages": [
                {
                    "index": index,
                    "name": page["name"],
                    "kind": page["kind"],
                    "rotation": page.get("rotation", 0),
                }
                for index, page in enumerate(batch_state["pages"])
            ],
        }


def batch_clear() -> None:
    """Drop every collected page and its file."""
    with batch_lock:
        for page in batch_state["pages"]:
            try:
                Path(page["path"]).unlink(missing_ok=True)
            except OSError:
                pass
        batch_state.update({"active": False, "pages": [], "replace_index": None})


def batch_store(source: Path) -> int:
    """Move a finished scan into the batch, replacing a page when armed."""
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    target = BATCH_DIR / f"page_{int(time.time() * 1000)}{source.suffix.lower()}"
    target.write_bytes(source.read_bytes())
    source.unlink(missing_ok=True)
    entry = {
        "name": target.name,
        "path": str(target),
        "kind": "pdf" if target.suffix.lower() == ".pdf" else "image",
        "rotation": 0,
    }
    with batch_lock:
        index = batch_state["replace_index"]
        if index is not None and 0 <= index < len(batch_state["pages"]):
            previous = batch_state["pages"][index]
            try:
                Path(previous["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            batch_state["pages"][index] = entry
            batch_state["replace_index"] = None
            return index
        batch_state["pages"].append(entry)
        batch_state["replace_index"] = None
        return len(batch_state["pages"]) - 1


def as_pdf_document(path: Path, rotation: int = 0) -> "pypdfium2.PdfDocument":
    """Every page becomes a PDF so the merge only has to deal with one format."""
    import pypdfium2
    from PIL import Image

    rotation = normalise_rotation(rotation)
    if path.suffix.lower() == ".pdf":
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


def normalise_rotation(degrees: Any) -> int:
    try:
        return int(degrees) % 360 // 90 * 90
    except (TypeError, ValueError):
        return 0


def merge_batch(pages: list[dict[str, Any]], target: Path) -> int:
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


def finish_batch(config: dict[str, Any], session_tags: list[str]) -> Path:
    """Merge, store and (optionally) upload the collected pages as one document."""
    with batch_lock:
        pages = list(batch_state["pages"])
    if not pages:
        raise RuntimeError("Der Stapel enthält keine Seiten.")

    logs.progress("merge", 30)
    logs.publish(f"Füge {len(pages)} Seite(n) zu einem PDF zusammen …")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = Path(config["output_dir"]) / f"scan_{timestamp}_{len(pages)}-seiten.pdf"
    page_count = merge_batch(pages, target)
    logs.publish(f"Dokument erstellt: {target.name} ({page_count} Seiten)", "success")

    preview_cache.update({"path": None, "image": None})
    scan_state.update({
        "last_file": str(target),
        "last_name": target.name,
        "last_kind": "pdf",
        "last_finished": datetime.now().isoformat(timespec="seconds"),
    })

    if config["upload_to_paperless"]:
        logs.progress("upload", 80)
        upload_config = {**config}
        if session_tags:
            upload_config["default_tags"] = list(dict.fromkeys(config["default_tags"] + session_tags))
        PaperlessClient(upload_config, logs).upload(target)
    else:
        logs.publish("Upload nach Paperless-ngx ist deaktiviert.", "warning")

    batch_clear()
    return target


# --------------------------------------------------------------------------- #
# Scan workflow
# --------------------------------------------------------------------------- #

def start_scan_job(session_tags: list[str], trigger: str = "ui", overrides: dict[str, Any] | None = None) -> bool:
    """Kick off a scan unless one is already running. Returns False if busy."""
    if not scan_lock.acquire(blocking=False):
        return False
    scan_state.update({
        "running": True,
        "last_error": None,
        "stage": "start",
        "progress": 2,
        "trigger": trigger,
    })
    threading.Thread(
        target=scan_worker,
        args=(session_tags, trigger, overrides or {}),
        daemon=True,
    ).start()
    return True


def scan_worker(session_tags: list[str], trigger: str, overrides: dict[str, Any]) -> None:
    try:
        config = store.get()
        for key in ("source", "resolution", "color_mode", "output_format", "upload_to_paperless", "title_prefix"):
            if key in overrides and overrides[key] not in (None, ""):
                config[key] = overrides[key]
        config = validate_config(config)
        if session_tags:
            config["default_tags"] = list(dict.fromkeys(config["default_tags"] + session_tags))
            logs.publish(f"Session-Tags: {', '.join(session_tags)}")
        logs.publish(f"Scan angefordert ({'Home Assistant' if trigger == 'ha' else 'Oberfläche'}).")
        logs.progress("start", 4)
        if config["upload_to_paperless"]:
            PaperlessClient(config, logs)._require_config()
        scanner = ScannerClient(config, logs)
        file_path = scanner.scan()

        if batch_state["active"]:
            # Collect the page; nothing is uploaded until the batch is closed.
            replaced = batch_state["replace_index"]
            index = batch_store(file_path)
            total = len(batch_state["pages"])
            action = f"Seite {index + 1} ersetzt" if replaced is not None else f"Seite {total} aufgenommen"
            logs.publish(f"{action} · {total} Seite(n) im Stapel.", "success")
            logs.progress("done", 100, batch=True, pages=total, page_index=index)
            notify_home_assistant(config, "page", f"Seite {index + 1}", None)
            return

        preview_cache.update({"path": None, "image": None})
        scan_state.update({
            "last_file": str(file_path),
            "last_name": file_path.name,
            "last_kind": "pdf" if file_path.suffix.lower() == ".pdf" else "image",
            "last_finished": datetime.now().isoformat(timespec="seconds"),
        })
        if config["upload_to_paperless"]:
            logs.progress("upload", 88)
            PaperlessClient(config, logs).upload(file_path)
        else:
            logs.publish("Upload nach Paperless-ngx ist deaktiviert.", "warning")
        logs.progress("done", 100, file=file_path.name)
        logs.publish("Workflow abgeschlossen.", "success")
        notify_home_assistant(config, "success", file_path.name, None)
    except Exception as error:  # surface every device/API failure in the log stream
        scan_state["last_error"] = str(error)
        logs.progress("error", 100, error=str(error))
        logs.publish(f"Workflow fehlgeschlagen: {error}", "error")
        notify_home_assistant(store.get(), "error", None, str(error))
    finally:
        scan_state["running"] = False
        scan_lock.release()


def notify_home_assistant(config: dict[str, Any], status: str, filename: str | None, error: str | None) -> None:
    """Fire-and-forget callback so HA automations can react to a finished scan."""
    webhook = config.get("ha_webhook_url")
    if not config.get("ha_enabled") or not webhook:
        return

    def send() -> None:
        try:
            requests.post(
                webhook,
                json={
                    "status": status,
                    "file": filename,
                    "error": error,
                    "trigger": scan_state.get("trigger"),
                    "finished": datetime.now().isoformat(timespec="seconds"),
                },
                timeout=10,
            )
            logs.publish("Home Assistant benachrichtigt.", "info")
        except requests.RequestException as request_error:
            logs.publish(f"Home-Assistant-Webhook fehlgeschlagen: {request_error}", "warning")

    threading.Thread(target=send, daemon=True).start()


# --------------------------------------------------------------------------- #
# Preview rendering
# --------------------------------------------------------------------------- #

def render_preview(file_path: Path, rotation: int = 0) -> tuple[bytes, str]:
    """Return displayable image bytes for a scan (first page for PDFs)."""
    rotation = normalise_rotation(rotation)
    cache_key = (str(file_path), rotation)
    if preview_cache["path"] == cache_key and preview_cache["image"]:
        return preview_cache["image"], preview_cache["mimetype"]

    if file_path.suffix.lower() != ".pdf":
        if not rotation:
            payload = file_path.read_bytes()
            preview_cache.update({"path": cache_key, "image": payload, "mimetype": "image/jpeg"})
            return payload, "image/jpeg"
        from PIL import Image

        buffer = io.BytesIO()
        Image.open(file_path).convert("RGB").rotate(-rotation, expand=True).save(buffer, "JPEG", quality=88)
        payload = buffer.getvalue()
        preview_cache.update({"path": cache_key, "image": payload, "mimetype": "image/jpeg"})
        return payload, "image/jpeg"

    try:
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
        payload = buffer.getvalue()
        preview_cache.update({"path": cache_key, "image": payload, "mimetype": "image/png"})
        return payload, "image/png"
    except Exception as error:  # missing wheel or broken PDF: fall back to the file
        logs.publish(f"PDF-Vorschau nicht gerendert ({error}); zeige Originaldatei.", "warning")
        raise


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    response = send_from_directory(app.static_folder, "manifest.webmanifest")
    response.headers["Content-Type"] = "application/manifest+json"
    return response


@app.get("/sw.js")
def service_worker() -> Response:
    # Served from the root so the worker can control the whole origin.
    response = send_from_directory(app.static_folder, "sw.js")
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/config")
def get_config() -> Response:
    config = store.public()
    config["suggested_subnet"] = config.get("discovery_subnet") or guess_local_subnet()
    config["version"] = APP_VERSION
    config["releases_url"] = RELEASES_URL
    return jsonify(config)


def storage_error(error: OSError, target: Path) -> tuple[Response, int]:
    """Turn a bare PermissionError into something a user can act on."""
    message = (
        f"{target} ist nicht beschreibbar ({error.strerror or error}). "
        "Der Ordner gehoert vermutlich einem anderen Benutzer als dem Dienst — "
        "Container neu starten oder PUID/PGID passend zum Host setzen."
    )
    logs.publish(message, "error")
    return jsonify({"error": message}), 500


@app.put("/api/config")
def put_config() -> Response:
    try:
        config = store.save(request.get_json(force=True) or {})
        logs.publish("Einstellungen gespeichert.", "success")
        return jsonify(config)
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    except OSError as error:
        return storage_error(error, Path(getattr(error, "filename", None) or CONFIG_PATH))


@app.post("/api/setup/complete")
def complete_setup() -> Response:
    try:
        payload = request.get_json(silent=True) or {}
        if payload:
            store.save(payload)
        config = store.patch(setup_complete=True)
        logs.publish("Einrichtung abgeschlossen.", "success")
        return jsonify(config)
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    except OSError as error:
        return storage_error(error, Path(getattr(error, "filename", None) or CONFIG_PATH))


@app.post("/api/setup/reset")
def reset_setup() -> Response:
    """Wipe the stored configuration and restart the wizard."""
    try:
        CONFIG_PATH.unlink(missing_ok=True)
    except OSError as error:
        return jsonify({"error": str(error)}), 500
    store._config = DEFAULT_CONFIG.copy()
    logs.publish("Konfiguration zurückgesetzt.", "warning")
    return jsonify(store.public())


@app.get("/api/state")
def state() -> Response:
    return jsonify({
        **scan_state,
        "setup_complete": store.get()["setup_complete"],
        "batch": batch_public(),
    })


@app.post("/api/test/scanner")
def test_scanner() -> Response:
    try:
        result = ScannerClient(store.get(), logs).capabilities()
        return jsonify({"ok": True, **result})
    except (requests.RequestException, ET.ParseError, RuntimeError) as error:
        logs.publish(f"Scanner-Test fehlgeschlagen: {error}", "error")
        return jsonify({"ok": False, "error": str(error)}), 502


@app.post("/api/test/paperless")
def test_paperless() -> Response:
    try:
        PaperlessClient(store.get(), logs).test()
        return jsonify({"ok": True})
    except (requests.RequestException, RuntimeError) as error:
        logs.publish(f"Paperless-Test fehlgeschlagen: {error}", "error")
        return jsonify({"ok": False, "error": str(error)}), 502


@app.post("/api/discover/scanners")
def discover_scanners() -> Response:
    """Search one given network, or auto-detect the likely ones when none is set."""
    try:
        payload = request.get_json(silent=True) or {}
        config = store.get()
        subnet = str(payload.get("discovery_subnet") or "").strip()

        if subnet and not payload.get("auto"):
            config["discovery_subnet"] = subnet
            devices = discover_escl_scanners(config, logs)
            return jsonify({"ok": True, "devices": devices, "subnet": config["discovery_subnet"]})

        devices, found_in = autodiscover_scanners(config, logs, request.remote_addr)
        if not devices:
            logs.publish("Kein Scanner gefunden. Netzwerk bitte manuell angeben.", "warning")
        return jsonify({"ok": True, "devices": devices, "subnet": found_in, "auto": True})
    except (ValueError, requests.RequestException) as error:
        logs.publish(f"Scanner-Suche fehlgeschlagen: {error}", "error")
        return jsonify({"ok": False, "error": str(error)}), 400


@app.get("/api/discover/candidates")
def discovery_candidates() -> Response:
    return jsonify({"candidates": candidate_subnets(store.get(), request.remote_addr)[:4]})


@app.post("/api/scan")
def start_scan() -> Response:
    payload = request.get_json(silent=True) or {}
    session_tags = list(dict.fromkeys(
        str(tag).strip() for tag in payload.get("session_tags", []) if str(tag).strip()
    ))
    if not start_scan_job(session_tags, "ui", payload.get("overrides")):
        return jsonify({"error": "Ein Scan läuft bereits."}), 409
    return jsonify({"ok": True, "message": "Scan wurde gestartet."}), 202


@app.get("/api/batch")
def get_batch() -> Response:
    return jsonify(batch_public())


@app.post("/api/batch/start")
def start_batch() -> Response:
    batch_clear()
    with batch_lock:
        batch_state["active"] = True
    logs.publish("Stapel gestartet — Seiten werden gesammelt.", "success")
    return jsonify(batch_public())


@app.post("/api/batch/cancel")
def cancel_batch() -> Response:
    pages = len(batch_state["pages"])
    batch_clear()
    logs.publish(f"Stapel verworfen ({pages} Seite(n)).", "warning")
    return jsonify(batch_public())


@app.post("/api/batch/replace")
def arm_replace() -> Response:
    """Arm the next scan to overwrite one page instead of appending."""
    payload = request.get_json(silent=True) or {}
    index = payload.get("index")
    with batch_lock:
        if index is None:
            batch_state["replace_index"] = None
        elif not isinstance(index, int) or not 0 <= index < len(batch_state["pages"]):
            return jsonify({"error": "Diese Seite gibt es nicht."}), 400
        else:
            batch_state["replace_index"] = index
    if index is not None:
        logs.publish(f"Nächster Scan ersetzt Seite {int(index) + 1}.")
    return jsonify(batch_public())


@app.delete("/api/batch/page/<int:index>")
def delete_batch_page(index: int) -> Response:
    with batch_lock:
        if not 0 <= index < len(batch_state["pages"]):
            return jsonify({"error": "Diese Seite gibt es nicht."}), 404
        page = batch_state["pages"].pop(index)
        if batch_state["replace_index"] is not None:
            batch_state["replace_index"] = None
    try:
        Path(page["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    logs.publish(f"Seite {index + 1} aus dem Stapel entfernt.")
    return jsonify(batch_public())


@app.get("/api/batch/page/<int:index>/preview")
def batch_page_preview(index: int) -> Response:
    with batch_lock:
        if not 0 <= index < len(batch_state["pages"]):
            return jsonify({"error": "Diese Seite gibt es nicht."}), 404
        page = batch_state["pages"][index]
    path = Path(page["path"])
    if not path.exists():
        return jsonify({"error": "Seite nicht mehr vorhanden."}), 404
    try:
        payload, mimetype = render_preview(path, page.get("rotation", 0))
    except Exception:
        return send_file(path, mimetype="application/pdf", max_age=0)
    return Response(payload, mimetype=mimetype, headers={"Cache-Control": "no-store"})


@app.post("/api/batch/page/<int:index>/rotate")
def rotate_batch_page(index: int) -> Response:
    """Turn a single page; the rotation is applied when the batch is merged."""
    degrees = normalise_rotation((request.get_json(silent=True) or {}).get("degrees", 90)) or 90
    with batch_lock:
        if not 0 <= index < len(batch_state["pages"]):
            return jsonify({"error": "Diese Seite gibt es nicht."}), 404
        page = batch_state["pages"][index]
        page["rotation"] = (page.get("rotation", 0) + degrees) % 360
    return jsonify(batch_public())


@app.post("/api/batch/order")
def reorder_batch() -> Response:
    """Apply a new page order, e.g. after dragging a page to another slot."""
    order = (request.get_json(silent=True) or {}).get("order")
    with batch_lock:
        pages = batch_state["pages"]
        if not isinstance(order, list) or sorted(order) != list(range(len(pages))):
            return jsonify({"error": "Die Reihenfolge passt nicht zum Stapel."}), 400
        armed = batch_state["replace_index"]
        batch_state["pages"] = [pages[position] for position in order]
        if armed is not None:
            # Keep the armed page armed, even though it sits somewhere else now.
            batch_state["replace_index"] = order.index(armed) if armed in order else None
    logs.publish("Reihenfolge im Stapel geändert.")
    return jsonify(batch_public())


@app.post("/api/batch/finish")
def close_batch() -> Response:
    if not batch_state["pages"]:
        return jsonify({"error": "Der Stapel enthält keine Seiten."}), 400
    if not scan_lock.acquire(blocking=False):
        return jsonify({"error": "Ein Scan läuft gerade."}), 409

    payload = request.get_json(silent=True) or {}
    session_tags = list(dict.fromkeys(
        str(tag).strip() for tag in payload.get("session_tags", []) if str(tag).strip()
    ))
    scan_state.update({"running": True, "last_error": None, "stage": "merge", "progress": 10, "trigger": "batch"})
    threading.Thread(target=batch_finish_worker, args=(session_tags,), daemon=True).start()
    return jsonify({"ok": True}), 202


def batch_finish_worker(session_tags: list[str]) -> None:
    try:
        config = validate_config(store.get())
        target = finish_batch(config, session_tags)
        logs.progress("done", 100, file=target.name)
        logs.publish("Stapel abgeschlossen.", "success")
        notify_home_assistant(config, "success", target.name, None)
    except Exception as error:
        scan_state["last_error"] = str(error)
        logs.progress("error", 100, error=str(error))
        logs.publish(f"Stapel fehlgeschlagen: {error}", "error")
        notify_home_assistant(store.get(), "error", None, str(error))
    finally:
        scan_state["running"] = False
        scan_lock.release()


@app.get("/api/preview")
def preview() -> Response:
    """Rasterised view of the most recent scan for the post-scan preview."""
    last_file = scan_state.get("last_file")
    if not last_file or not Path(last_file).exists():
        return jsonify({"error": "Keine Vorschau verfügbar."}), 404
    path = Path(last_file)
    try:
        payload, mimetype = render_preview(path)
    except Exception:
        return send_file(path, mimetype="application/pdf", max_age=0)
    return Response(payload, mimetype=mimetype, headers={"Cache-Control": "no-store"})


@app.get("/api/preview/file")
def preview_file() -> Response:
    last_file = scan_state.get("last_file")
    if not last_file or not Path(last_file).exists():
        return jsonify({"error": "Keine Datei verfügbar."}), 404
    path = Path(last_file)
    mimetype = "application/pdf" if path.suffix.lower() == ".pdf" else "image/jpeg"
    return send_file(path, mimetype=mimetype, as_attachment=False, download_name=path.name, max_age=0)


@app.get("/api/logs")
def stream_logs() -> Response:
    return Response(
        stream_with_context(logs.stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/update")
def update_info() -> Response:
    """Version banner data; never blocks the UI when GitHub is unreachable."""
    if not store.get().get("update_check"):
        return jsonify({
            "current": APP_VERSION,
            "latest": "",
            "update_available": False,
            "url": RELEASES_URL,
            "disabled": True,
        })
    return jsonify(check_for_update(force=request.args.get("force") == "1"))


@app.get("/health")
def health() -> Response:
    return jsonify({"ok": True, "version": APP_VERSION})


# --------------------------------------------------------------------------- #
# Home Assistant integration
# --------------------------------------------------------------------------- #

def require_api_key(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        config = store.get()
        if not config.get("ha_enabled"):
            return jsonify({"error": "Home-Assistant-Schnittstelle ist deaktiviert."}), 403
        expected = config.get("ha_api_key", "")
        provided = (
            request.headers.get("X-API-Key")
            or request.args.get("api_key")
            or (request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        )
        if not expected or not provided or not secrets.compare_digest(str(provided), str(expected)):
            return jsonify({"error": "Ungültiger API-Key."}), 401
        return view(*args, **kwargs)

    return wrapper


@app.post("/api/ha/key")
def rotate_ha_key() -> Response:
    """Generate a fresh API key for Home Assistant and return it once."""
    key = secrets.token_urlsafe(32)
    store.patch(ha_api_key=key, ha_enabled=True)
    logs.publish("Neuer Home-Assistant-API-Key erzeugt.", "success")
    return jsonify({"ok": True, "api_key": key})


@app.get("/api/ha/key")
def read_ha_key() -> Response:
    return jsonify({"api_key": store.get().get("ha_api_key", "")})


@app.post("/api/ha/scan")
@require_api_key
def ha_scan() -> Response:
    """Trigger endpoint for HA automations (motion sensor, button, script, …)."""
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags") or payload.get("session_tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    session_tags = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    overrides = {
        key: payload[key]
        for key in ("source", "resolution", "color_mode", "output_format", "upload_to_paperless", "title_prefix")
        if key in payload
    }
    if not start_scan_job(session_tags, "ha", overrides):
        return jsonify({"ok": False, "error": "Ein Scan läuft bereits."}), 409
    return jsonify({"ok": True, "message": "Scan gestartet."}), 202


@app.get("/api/ha/state")
@require_api_key
def ha_state() -> Response:
    """Flat payload for a Home Assistant RESTful sensor."""
    config = store.get()
    return jsonify({
        "state": "scanning" if scan_state["running"] else ("error" if scan_state["last_error"] else "idle"),
        "running": scan_state["running"],
        "stage": scan_state["stage"],
        "progress": scan_state["progress"],
        "last_file": scan_state["last_name"],
        "last_finished": scan_state["last_finished"],
        "last_error": scan_state["last_error"],
        "trigger": scan_state["trigger"],
        "batch_active": batch_state["active"],
        "batch_pages": len(batch_state["pages"]),
        "version": APP_VERSION,
        "scanner_url": config["scanner_url"],
        "upload_to_paperless": config["upload_to_paperless"],
    })


@app.post("/api/ha/batch")
@require_api_key
def ha_batch() -> Response:
    """Let an automation open, close or discard a batch (e.g. two buttons)."""
    action = str((request.get_json(silent=True) or {}).get("action", "")).lower()
    if action == "start":
        batch_clear()
        with batch_lock:
            batch_state["active"] = True
        logs.publish("Stapel über Home Assistant gestartet.", "success")
        return jsonify({"ok": True, **batch_public()})
    if action == "cancel":
        batch_clear()
        logs.publish("Stapel über Home Assistant verworfen.", "warning")
        return jsonify({"ok": True, **batch_public()})
    if action == "finish":
        if not batch_state["pages"]:
            return jsonify({"ok": False, "error": "Der Stapel enthält keine Seiten."}), 400
        if not scan_lock.acquire(blocking=False):
            return jsonify({"ok": False, "error": "Ein Scan läuft gerade."}), 409
        scan_state.update({"running": True, "last_error": None, "stage": "merge", "progress": 10, "trigger": "ha"})
        threading.Thread(target=batch_finish_worker, args=([],), daemon=True).start()
        return jsonify({"ok": True, "message": "Stapel wird abgeschlossen."}), 202
    return jsonify({"ok": False, "error": "action muss start, finish oder cancel sein."}), 400


@app.post("/api/ha/test")
@require_api_key
def ha_test() -> Response:
    logs.publish("Home Assistant hat die Verbindung getestet.", "success")
    return jsonify({"ok": True, "message": "Verbindung steht."})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)

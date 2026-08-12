"""Settings: what may be stored, how it is checked, and where it lives."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from typing import Any

APP_DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
CONFIG_PATH = APP_DATA_DIR / "config.json"
BATCH_DIR = APP_DATA_DIR / "batch"
JOBS_PATH = APP_DATA_DIR / "jobs.json"
TIMINGS_PATH = APP_DATA_DIR / "timings.json"
DEFAULT_OUTPUT_DIR = os.environ.get("SCAN_OUTPUT_DIR", "/scans")


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
    "paper_size": "A4",
    "duplex": False,
    "upload_to_paperless": False,
    "title_prefix": "Scan",
    "preview_seconds": 10,
    "update_check": True,
    # Extras bleiben aus, damit die Startoberflaeche schlank bleibt.
    "metadata_enabled": False,
    "quick_tags_enabled": False,
    "cleanup_enabled": False,
    "cleanup_hours": 24,
    "prewarm_enabled": True,
    "sound_enabled": False,
    # Nachbearbeitung: beide aus, weil sie Seiten verändern beziehungsweise
    # verwerfen — das soll niemand ungefragt bekommen.
    "skip_blank_pages": False,
    "deskew_enabled": False,
    "profiles": [],
    "setup_complete": False,
    "ha_enabled": False,
    "ha_api_key": "",
    "ha_webhook_url": "",
    # MQTT: Home Assistant legt die Geraete selbst an, sobald ein Broker steht.
    "mqtt_enabled": False,
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_password": "",
    "mqtt_device_name": "ScanDeck",
    # Push aufs Handy, wenn ein Scan fertig ist.
    "push_enabled": False,
    "push_public_key": "",
    "push_private_key": "",
    "push_subscriptions": [],
    # Zugriffsschutz ist Opt-in; das Passwort liegt nur als Hash hier.
    "auth_enabled": False,
    "auth_password_hash": "",
    "session_secret": "",
}

# Never overwritten by an empty value when settings are saved.
SECRET_KEYS = ("paperless_token", "ha_api_key", "auth_password_hash", "session_secret",
               "mqtt_password", "push_private_key")
# Never handed out over the API. The Home Assistant key is deliberately not in
# here: it has to be readable once so it can be copied into an automation.
PRIVATE_KEYS = ("paperless_token", "auth_password_hash", "session_secret",
                "mqtt_password", "push_private_key", "push_subscriptions")
# Not settable through the generic settings endpoint — these have their own
# routes, so a password never travels inside a bulk save.
PROTECTED_KEYS = ("auth_enabled", "auth_password_hash", "session_secret")

ALLOWED_SOURCES = {"Platen", "Feeder"}
ALLOWED_RESOLUTIONS = {75, 100, 150, 200, 300, 600, 1200}
# Schwarzweiß (1 bit) bringt für Belege nichts, was Graustufen nicht besser
# können: Text franst aus und Paperless erkennt ihn schlechter. Bestehende
# Konfigurationen werden still auf Graustufen gehoben statt abgelehnt.
ALLOWED_COLOR_MODES = {"RGB24", "Grayscale8"}
RETIRED_COLOR_MODES = {"BlackAndWhite1": "Grayscale8"}
ALLOWED_FORMATS = {"image/jpeg", "application/pdf"}

# Scan regions in escl:ThreeHundredthsOfInches, the unit eSCL expects.
PAPER_SIZES: dict[str, tuple[int, int]] = {
    "A4": (2480, 3508),
    "Letter": (2550, 3300),
    "Legal": (2550, 4200),
    "A5": (1748, 2480),
}


def default_config() -> dict[str, Any]:
    """A fresh copy of the defaults.

    A shallow copy would hand out the same "default_tags" list every time, so a
    single in-place edit anywhere would rewrite the defaults for the process.
    """
    return copy.deepcopy(DEFAULT_CONFIG)


def normalise_url(value: str, field_name: str, required: bool = False) -> str:
    value = str(value or "").strip().rstrip("/")
    if not value and required:
        raise ValueError(f"{field_name} darf nicht leer sein.")
    if value and not re.match(r"^https?://", value, re.IGNORECASE):
        raise ValueError(f"{field_name} muss mit http:// oder https:// beginnen.")
    return value


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Coerce and check every stored field. Free of side effects on purpose:
    this runs before every scan, so it must not touch the filesystem."""
    # Empty endpoints stay valid: an unconfigured instance must be storable so
    # the wizard can save progress step by step.
    config["scanner_url"] = normalise_url(config.get("scanner_url", ""), "Scanner-URL")
    config["paperless_url"] = normalise_url(config.get("paperless_url", ""), "Paperless-URL")
    config["ha_webhook_url"] = normalise_url(config.get("ha_webhook_url", ""), "Home-Assistant-Webhook")
    config["discovery_subnet"] = str(config.get("discovery_subnet", "")).strip()
    config["verify_scanner_ssl"] = bool(config.get("verify_scanner_ssl"))
    config["source"] = config.get("source", "Platen")
    config["resolution"] = int(config.get("resolution", 300))
    color_mode = config.get("color_mode", "RGB24")
    config["color_mode"] = RETIRED_COLOR_MODES.get(color_mode, color_mode)
    config["output_format"] = config.get("output_format", "application/pdf")
    config["paper_size"] = str(config.get("paper_size", "A4") or "A4")
    config["duplex"] = bool(config.get("duplex"))
    config["upload_to_paperless"] = bool(config.get("upload_to_paperless"))
    config["create_missing_tags"] = bool(config.get("create_missing_tags", True))
    config["title_prefix"] = str(config.get("title_prefix", "Scan")).strip() or "Scan"
    config["update_check"] = bool(config.get("update_check"))
    config["metadata_enabled"] = bool(config.get("metadata_enabled"))
    config["quick_tags_enabled"] = bool(config.get("quick_tags_enabled"))
    config["cleanup_enabled"] = bool(config.get("cleanup_enabled"))
    config["prewarm_enabled"] = bool(config.get("prewarm_enabled"))
    config["sound_enabled"] = bool(config.get("sound_enabled"))
    config["cleanup_hours"] = max(1, min(8760, int(config.get("cleanup_hours", 24) or 24)))
    config["setup_complete"] = bool(config.get("setup_complete"))
    config["ha_enabled"] = bool(config.get("ha_enabled"))
    config["ha_api_key"] = str(config.get("ha_api_key", "")).strip()
    config["auth_password_hash"] = str(config.get("auth_password_hash", "")).strip()
    config["session_secret"] = str(config.get("session_secret", "")).strip()
    # A lock without a key would shut everyone out permanently.
    config["auth_enabled"] = bool(config.get("auth_enabled")) and bool(config["auth_password_hash"])
    config["preview_seconds"] = max(0, min(60, int(config.get("preview_seconds", 10))))

    if config["source"] not in ALLOWED_SOURCES:
        raise ValueError("Unbekannte Scanquelle.")
    if config["resolution"] not in ALLOWED_RESOLUTIONS:
        raise ValueError("Nicht unterstützte Auflösung.")
    if config["color_mode"] not in ALLOWED_COLOR_MODES:
        raise ValueError("Nicht unterstützter Farbmodus.")
    if config["output_format"] not in ALLOWED_FORMATS:
        raise ValueError("Nicht unterstütztes Ausgabeformat.")
    if config["paper_size"] not in PAPER_SIZES:
        raise ValueError("Unbekanntes Papierformat.")
    # Duplex is a property of the sheet feeder; on the flatbed it means nothing.
    if config["source"] != "Feeder":
        config["duplex"] = False

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
    config["output_dir"] = str(output_dir)

    config["skip_blank_pages"] = bool(config.get("skip_blank_pages"))
    config["deskew_enabled"] = bool(config.get("deskew_enabled"))

    config["mqtt_enabled"] = bool(config.get("mqtt_enabled"))
    config["mqtt_host"] = str(config.get("mqtt_host", "")).strip()
    config["mqtt_port"] = max(1, min(65535, int(config.get("mqtt_port") or 1883)))
    config["mqtt_user"] = str(config.get("mqtt_user", "")).strip()
    config["mqtt_password"] = str(config.get("mqtt_password", ""))
    config["mqtt_device_name"] = str(config.get("mqtt_device_name", "") or "ScanDeck").strip()[:40]
    # A bridge without a broker would only produce connection errors.
    config["mqtt_enabled"] = config["mqtt_enabled"] and bool(config["mqtt_host"])

    config["push_enabled"] = bool(config.get("push_enabled"))
    config["push_public_key"] = str(config.get("push_public_key", "")).strip()
    config["push_private_key"] = str(config.get("push_private_key", "")).strip()
    subscriptions = config.get("push_subscriptions", [])
    if not isinstance(subscriptions, list):
        raise ValueError("Push-Abonnements müssen eine Liste sein.")
    config["push_subscriptions"] = [entry for entry in subscriptions
                                    if isinstance(entry, dict) and entry.get("endpoint")][:20]

    tags = config.get("default_tags", [])
    if not isinstance(tags, list):
        raise ValueError("Standard-Tags müssen eine Liste sein.")
    config["default_tags"] = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    config["profiles"] = validate_profiles(config.get("profiles", []))
    return config


# What a profile may carry: the scan settings plus where the result should go.
PROFILE_FIELDS = ("source", "resolution", "color_mode", "output_format", "paper_size",
                  "duplex", "title_prefix", "correspondent", "document_type")
MAX_PROFILES = 12


def validate_profiles(profiles: Any) -> list[dict[str, Any]]:
    """One-tap presets: a name, a few scan settings, optional tags."""
    if not isinstance(profiles, list):
        raise ValueError("Profile müssen eine Liste sein.")
    cleaned: list[dict[str, Any]] = []
    for entry in profiles[:MAX_PROFILES]:
        if not isinstance(entry, dict):
            raise ValueError("Jedes Profil muss ein Objekt sein.")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ValueError("Jedes Profil braucht einen Namen.")
        profile: dict[str, Any] = {"name": name[:40], "id": str(entry.get("id") or name.lower())[:60]}
        settings = {key: entry[key] for key in PROFILE_FIELDS if entry.get(key) not in (None, "")}
        # Validated against the same rules as the stored settings, so a profile
        # can never smuggle in a value a scan would choke on.
        checked = validate_config({**default_config(), **settings})
        profile.update({key: checked[key] for key in settings})
        tags = entry.get("tags", [])
        if not isinstance(tags, list):
            raise ValueError("Profil-Tags müssen eine Liste sein.")
        profile["tags"] = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
        # Only stored when the profile really wants to decide this. Otherwise
        # the global setting applies — a profile should not switch the upload on
        # for someone who never configured Paperless.
        if "upload_to_paperless" in entry:
            profile["upload_to_paperless"] = bool(entry["upload_to_paperless"])
        cleaned.append(profile)
    if len({profile["id"] for profile in cleaned}) != len(cleaned):
        raise ValueError("Profilnamen müssen sich unterscheiden.")
    return cleaned


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            merged = {**default_config(), **stored}
            # Configs written before the wizard existed are already set up.
            if "setup_complete" not in stored:
                merged["setup_complete"] = bool(merged.get("scanner_url"))
            return merged
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default_config()

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
        public_config["paperless_token_configured"] = bool(config.get("paperless_token"))
        public_config["auth_configured"] = bool(config.get("auth_password_hash"))
        public_config["mqtt_password_configured"] = bool(config.get("mqtt_password"))
        public_config["push_devices"] = len(config.get("push_subscriptions") or [])
        # Strip every secret in one place, so a newly added one cannot be
        # forgotten here and leak through the settings endpoint.
        for key in PRIVATE_KEYS:
            public_config.pop(key, None)
        # The Home Assistant key has to be readable once so users can copy it
        # into their automation; it is generated locally and never leaves the LAN.
        public_config["ha_api_key_configured"] = bool(public_config.get("ha_api_key"))
        return public_config

    def _persist(self, updated: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
        self._config = updated
        return self._public_from_config(updated)

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = {**self._config}
            for key in DEFAULT_CONFIG:
                if key in payload and key not in SECRET_KEYS and key not in PROTECTED_KEYS:
                    updated[key] = payload[key]
            for key in SECRET_KEYS:
                # Protected secrets are never accepted here — a password hash
                # arriving in a bulk save would be someone handing themselves
                # a key to the front door.
                if key in PROTECTED_KEYS:
                    continue
                if key in payload and str(payload[key]).strip():
                    updated[key] = str(payload[key]).strip()
            return self._persist(validate_config(updated))

    def patch(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            return self._persist(validate_config({**self._config, **values}))

    def reset(self) -> dict[str, Any]:
        """Forget everything and hand the first run back to the wizard."""
        with self._lock:
            self.path.unlink(missing_ok=True)
            self._config = default_config()
            return self._public_from_config(self._config)

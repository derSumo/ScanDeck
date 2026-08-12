"""Where the version comes from, and where releases live."""

from __future__ import annotations

import os
from pathlib import Path


def read_version() -> str:
    """Single source of truth for the semantic version: the VERSION file."""
    try:
        return (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


APP_VERSION = read_version()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "derSumo/ScanDeck")
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
USER_AGENT = f"ScanDeck/{APP_VERSION}"

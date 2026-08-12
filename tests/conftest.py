"""Test setup: every test gets its own /data and /scans, never the real ones."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _forget_modules() -> None:
    """Drop app and package alike so paths are read from the environment again."""
    for name in [name for name in sys.modules if name == "app" or name.startswith("scandeck")]:
        del sys.modules[name]


@pytest.fixture()
def deck(tmp_path, monkeypatch):
    """A freshly imported application pointed at a throwaway data directory.

    Config, history and batch live in module-level stores, so the whole package
    is reimported per test instead of leaking one test's state into the next.
    """
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scans"))
    for name in ("SCANNER_URL", "PAPERLESS_URL", "PAPERLESS_TOKEN", "DISCOVERY_SUBNET"):
        monkeypatch.delenv(name, raising=False)

    _forget_modules()
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True)
    yield module
    _forget_modules()


@pytest.fixture()
def client(deck):
    return deck.app.test_client()

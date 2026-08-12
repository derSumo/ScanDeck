"""Configuration: validation, secrets and the reset the wizard relies on."""

from __future__ import annotations

import pytest


def test_defaults_are_not_shared_between_copies(deck):
    from scandeck.config import DEFAULT_CONFIG, default_config

    first = default_config()
    first["default_tags"].append("Rechnung")
    assert default_config()["default_tags"] == []
    assert DEFAULT_CONFIG["default_tags"] == []


def test_validate_rejects_unknown_options(deck):
    from scandeck.config import default_config

    for field, value in (
        ("source", "Nirgendwo"),
        ("resolution", 42),
        ("color_mode", "CMYK"),
        ("output_format", "image/png"),
        ("paper_size", "A0"),
    ):
        with pytest.raises(ValueError):
            deck.validate_config({**default_config(), field: value})


def test_every_offered_paper_size_validates(deck):
    from scandeck.config import PAPER_SIZES, default_config

    assert set(PAPER_SIZES) >= {"A4", "Letter", "Legal"}
    for name, (width, height) in PAPER_SIZES.items():
        assert deck.validate_config({**default_config(), "paper_size": name})["paper_size"] == name
        assert 0 < width < height  # portrait, in 1/300 inch


def test_duplex_only_survives_on_the_feeder(deck):
    from scandeck.config import default_config

    feeder = deck.validate_config({**default_config(), "source": "Feeder", "duplex": True})
    assert feeder["duplex"] is True
    platen = deck.validate_config({**default_config(), "source": "Platen", "duplex": True})
    assert platen["duplex"] is False


def test_validate_does_not_touch_the_filesystem(deck, tmp_path):
    """Validation runs before every scan; it must not create anything."""
    from scandeck.config import default_config

    target = tmp_path / "nicht-vorhanden"
    config = deck.validate_config({**default_config(), "output_dir": str(target)})
    assert config["output_dir"] == str(target)
    assert not target.exists()


def test_validate_requires_an_absolute_output_dir(deck):
    from scandeck.config import default_config

    with pytest.raises(ValueError):
        deck.validate_config({**default_config(), "output_dir": "scans"})


def test_discovery_subnet_must_be_a_small_private_network(deck):
    from scandeck.config import default_config

    config = deck.validate_config({**default_config(), "discovery_subnet": "192.168.5.7/24"})
    assert config["discovery_subnet"] == "192.168.5.0/24"

    for bad in ("8.8.8.0/24", "10.0.0.0/16", "keinnetz"):
        with pytest.raises(ValueError):
            deck.validate_config({**default_config(), "discovery_subnet": bad})


def test_urls_need_a_scheme(deck):
    from scandeck.config import default_config

    with pytest.raises(ValueError):
        deck.validate_config({**default_config(), "scanner_url": "10.0.0.31"})
    config = deck.validate_config({**default_config(), "scanner_url": "https://10.0.0.31/"})
    assert config["scanner_url"] == "https://10.0.0.31"


def test_token_is_never_returned_and_never_cleared_by_an_empty_value(client, deck):
    client.put("/api/config", json={"paperless_token": "geheim"})
    body = client.get("/api/config").get_json()
    assert "paperless_token" not in body
    assert body["paperless_token_configured"] is True

    # The interface sends the form back without the token; that must not wipe it.
    client.put("/api/config", json={"paperless_token": "", "title_prefix": "Beleg"})
    assert deck.store.get()["paperless_token"] == "geheim"
    assert client.get("/api/config").get_json()["title_prefix"] == "Beleg"


def test_config_endpoint_offers_the_paper_sizes(client):
    assert "A4" in client.get("/api/config").get_json()["paper_sizes"]


def test_reset_restores_the_defaults_and_removes_the_file(client, deck):
    client.put("/api/config", json={"title_prefix": "Beleg", "paperless_token": "geheim"})
    assert deck.CONFIG_PATH.exists()

    body = client.post("/api/setup/reset").get_json()
    assert body["title_prefix"] == "Scan"
    assert body["paperless_token_configured"] is False
    assert body["setup_complete"] is False
    assert not deck.CONFIG_PATH.exists()
    assert deck.store.get()["paperless_token"] == ""


def test_parse_version_orders_releases(deck):
    from scandeck.updates import parse_version

    assert parse_version("v1.10.0") > parse_version("1.9.9")
    assert parse_version("1.5.0-rc1") == (1, 5, 0)
    assert parse_version("kaputt") == (0, 0, 0)

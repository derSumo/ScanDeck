"""Profiles, the MQTT bridge and notifications on the phone."""

from __future__ import annotations

import json

import pytest


# --- profiles -------------------------------------------------------------- #

def test_a_profile_keeps_the_settings_it_was_given(client, deck):
    body = client.put("/api/profiles", json={"profiles": [
        {"name": "Rechnung", "source": "Feeder", "resolution": 300,
         "color_mode": "Grayscale8", "tags": ["Rechnung", "2026"]},
    ]}).get_json()
    profile = body["profiles"][0]
    assert profile["name"] == "Rechnung"
    assert profile["source"] == "Feeder"
    assert profile["color_mode"] == "Grayscale8"
    assert profile["tags"] == ["Rechnung", "2026"]
    assert profile["id"] == "rechnung"


def test_a_profile_is_checked_like_the_settings_are(client, deck):
    """A profile must not smuggle in a value a scan would choke on."""
    for bad in ({"name": "X", "resolution": 42},
                {"name": "X", "color_mode": "CMYK"},
                {"name": "X", "paper_size": "A0"},
                {"name": "X", "source": "Nirgendwo"}):
        assert client.put("/api/profiles", json={"profiles": [bad]}).status_code == 400
    assert deck.store.get()["profiles"] == []


def test_a_profile_needs_a_name(client):
    assert client.put("/api/profiles", json={"profiles": [{"resolution": 300}]}).status_code == 400
    assert client.put("/api/profiles", json={"profiles": [{"name": "   "}]}).status_code == 400


def test_two_profiles_cannot_share_a_name(client):
    response = client.put("/api/profiles", json={"profiles": [{"name": "Beleg"}, {"name": "beleg"}]})
    assert response.status_code == 400


def test_profiles_survive_a_restart(client, deck):
    client.put("/api/profiles", json={"profiles": [{"name": "Foto", "resolution": 600}]})
    from scandeck.config import ConfigStore

    assert ConfigStore(deck.CONFIG_PATH).get()["profiles"][0]["name"] == "Foto"


def test_scanning_with_an_unknown_profile_is_refused(client):
    assert client.post("/api/profiles/gibtsnicht/scan").status_code == 404


def test_a_profile_scan_uses_its_settings(client, deck, monkeypatch):
    from scandeck import escl
    from tests.test_escl import FakeScanner

    client.put("/api/profiles", json={"profiles": [
        {"name": "Grau", "source": "Platen", "resolution": 150,
         "color_mode": "Grayscale8", "tags": ["Beleg"]},
    ]})
    device = FakeScanner(sheets=1)
    monkeypatch.setattr(escl, "scanner_session", device)
    deck.store.patch(scanner_url="https://10.0.0.31:443")

    started = {}
    monkeypatch.setattr(deck, "start_scan_job",
                        lambda tags, trigger, overrides: started.update(tags=tags, trigger=trigger,
                                                                       overrides=overrides) or True)
    assert client.post("/api/profiles/grau/scan").status_code == 202
    assert started["tags"] == ["Beleg"]
    assert started["overrides"]["resolution"] == 150
    assert started["overrides"]["color_mode"] == "Grayscale8"


def test_home_assistant_can_name_a_profile(client, deck, monkeypatch):
    client.put("/api/profiles", json={"profiles": [{"name": "Post", "resolution": 300}]})
    key = client.post("/api/ha/key").get_json()["api_key"]
    started = {}
    monkeypatch.setattr(deck, "start_scan_job",
                        lambda tags, trigger, overrides: started.update(trigger=trigger) or True)

    response = client.post("/api/ha/scan", headers={"X-API-Key": key}, json={"profile": "post"})
    assert response.status_code == 202
    assert started["trigger"] == "ha"


# --- MQTT ------------------------------------------------------------------ #

def test_the_bridge_stays_off_without_a_broker(deck):
    config = deck.validate_config({**deck.store.get(), "mqtt_enabled": True, "mqtt_host": ""})
    assert config["mqtt_enabled"] is False  # a bridge to nowhere only makes errors


def test_the_broker_password_never_leaves_the_server(client, deck):
    deck.store.patch(mqtt_enabled=True, mqtt_host="broker.local", mqtt_password="geheim")
    body = client.get("/api/config").get_json()
    assert "mqtt_password" not in body
    assert body["mqtt_host"] == "broker.local"
    # And an empty value in a save must not wipe it.
    client.put("/api/config", json={"mqtt_password": "", "mqtt_device_name": "Scanner"})
    assert deck.store.get()["mqtt_password"] == "geheim"


def test_every_entity_home_assistant_needs_is_announced(deck):
    from scandeck.mqtt import BUTTONS, ENTITIES, device_block

    keys = {entity["key"] for entity in ENTITIES}
    assert {"state", "progress", "queue", "running", "feeder"} <= keys
    assert {button["key"] for button in BUTTONS} == {
        "scan", "cancel", "batch_start", "batch_finish", "batch_cancel"}
    assert device_block("Test")["identifiers"] == ["scandeck"]


def test_the_snapshot_carries_what_the_entities_read(deck):
    snapshot = deck.mqtt_snapshot()
    for entity in ("state", "running", "progress", "queue", "batch_active", "feeder", "last_file"):
        assert entity in snapshot


def test_a_button_press_drives_the_app(deck):
    deck.run_mqtt_command("batch_start")
    assert deck.batch.active() is True
    deck.run_mqtt_command("batch_cancel")
    assert deck.batch.active() is False


def test_an_unknown_button_is_shrugged_off(deck):
    deck.run_mqtt_command("tanzen")  # must not raise
    assert deck.batch.active() is False


# --- notifications --------------------------------------------------------- #

def test_notifications_are_off_and_keyless_at_first(client):
    body = client.get("/api/push/key").get_json()
    assert body == {"enabled": False, "public_key": ""}


def test_enabling_creates_a_key_pair_once(client, deck):
    first = client.post("/api/push/enable").get_json()
    assert first["ok"] is True and len(first["public_key"]) > 40
    second = client.post("/api/push/enable").get_json()
    assert second["public_key"] == first["public_key"]  # not rotated on every call


def test_the_private_key_never_leaves_the_server(client, deck):
    client.post("/api/push/enable")
    body = client.get("/api/config").get_json()
    assert "push_private_key" not in body
    assert deck.store.get()["push_private_key"]


def test_a_device_can_subscribe_and_unsubscribe(client, deck):
    client.post("/api/push/enable")
    subscription = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
    assert client.post("/api/push/subscribe", json=subscription).get_json()["devices"] == 1
    # Subscribing twice from the same browser must not count twice.
    assert client.post("/api/push/subscribe", json=subscription).get_json()["devices"] == 1
    assert client.post("/api/push/unsubscribe",
                       json={"endpoint": subscription["endpoint"]}).get_json()["devices"] == 0


def test_a_bogus_subscription_is_refused(client):
    client.post("/api/push/enable")
    assert client.post("/api/push/subscribe", json={"endpoint": "http://unsicher"}).status_code == 400
    assert client.post("/api/push/subscribe", json={}).status_code == 400


def test_a_test_notification_needs_a_device(client):
    client.post("/api/push/enable")
    assert client.post("/api/push/test").status_code == 400


def test_switching_off_forgets_every_device(client, deck):
    client.post("/api/push/enable")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/abc", "keys": {}})
    client.post("/api/push/disable")
    assert deck.store.get()["push_subscriptions"] == []
    assert deck.store.get()["push_enabled"] is False


def test_nothing_is_sent_while_notifications_are_off(deck):
    """No keys, no devices, no attempt — and above all no exception."""
    deck.push_service.notify("Test", "Nachricht")  # must not raise


def test_no_secret_ever_appears_in_the_settings_endpoint(client, deck):
    """One guard for all of them, so the next secret cannot be forgotten."""
    from scandeck.config import PRIVATE_KEYS

    client.post("/api/push/enable")
    deck.store.patch(mqtt_password="geheim", paperless_token="token", mqtt_host="broker")
    body = client.get("/api/config").get_json()
    for key in PRIVATE_KEYS:
        assert key not in body, f"{key} darf nicht ausgeliefert werden"
    # But the interface still learns whether they are set.
    assert body["mqtt_password_configured"] is True
    assert body["paperless_token_configured"] is True


def test_a_profile_inherits_the_upload_setting_unless_it_says_otherwise(client, deck):
    """A profile must not switch the upload on for someone without Paperless."""
    client.put("/api/profiles", json={"profiles": [
        {"name": "Schlicht", "resolution": 300},
        {"name": "Ohne Upload", "resolution": 300, "upload_to_paperless": False},
    ]})
    profiles = {entry["name"]: entry for entry in deck.store.get()["profiles"]}
    assert "upload_to_paperless" not in profiles["Schlicht"]
    assert profiles["Ohne Upload"]["upload_to_paperless"] is False


def test_a_profile_scan_does_not_force_an_upload(client, deck, monkeypatch):
    client.put("/api/profiles", json={"profiles": [{"name": "Schlicht", "resolution": 300}]})
    started = {}
    monkeypatch.setattr(deck, "start_scan_job",
                        lambda tags, trigger, overrides: started.update(overrides=overrides) or True)
    client.post("/api/profiles/schlicht/scan")
    assert "upload_to_paperless" not in started["overrides"]

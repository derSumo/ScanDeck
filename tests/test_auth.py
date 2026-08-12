"""Optional access protection: off by default, closed tight when switched on."""

from __future__ import annotations

import pytest

PASSWORD = "ein-gutes-passwort"


def enable(client, password=PASSWORD):
    return client.post("/api/auth/enable", json={"password": password})


# --- off by default -------------------------------------------------------- #

def test_everything_is_open_until_it_is_switched_on(client):
    assert client.get("/api/auth/state").get_json() == {
        "enabled": False, "authenticated": True, "min_length": 8,
    }
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/state").status_code == 200
    assert client.get("/api/history").status_code == 200


def test_logging_in_without_protection_is_a_no_op(client):
    assert client.post("/api/auth/login", json={"password": "egal"}).status_code == 200


# --- switching it on ------------------------------------------------------- #

def test_enabling_locks_the_interface_and_signs_this_browser_in(client, deck):
    body = enable(client).get_json()
    assert body["ok"] is True and body["enabled"] is True
    # The browser that switched it on stays signed in; locking yourself out of
    # your own scanner would be a poor way to start.
    assert client.get("/api/config").status_code == 200
    assert deck.store.get()["auth_enabled"] is True


def test_a_short_password_is_refused(client, deck):
    response = client.post("/api/auth/enable", json={"password": "kurz"})
    assert response.status_code == 400
    assert "8 Zeichen" in response.get_json()["error"]
    assert deck.store.get()["auth_enabled"] is False


def test_the_password_is_only_ever_stored_hashed(client, deck):
    enable(client)
    stored = deck.store.get()["auth_password_hash"]
    assert stored and PASSWORD not in stored
    assert deck.CONFIG_PATH.exists() and PASSWORD not in deck.CONFIG_PATH.read_text(encoding="utf-8")


def test_neither_hash_nor_session_secret_leave_the_server(client):
    enable(client)
    body = client.get("/api/config").get_json()
    assert "auth_password_hash" not in body
    assert "session_secret" not in body
    assert body["auth_configured"] is True


# --- what protection actually covers --------------------------------------- #

@pytest.fixture()
def locked(deck):
    """A protected instance, seen from a browser that is not signed in."""
    setup = deck.app.test_client()
    enable(setup)
    return deck.app.test_client()  # a fresh client carries no session cookie


@pytest.mark.parametrize("path", [
    "/api/config", "/api/state", "/api/history", "/api/batch",
    "/api/preview", "/api/logs", "/api/update", "/api/discover/candidates",
    "/api/paperless/collections", "/api/ha/key",
])
def test_reading_is_closed_without_a_session(locked, path):
    response = locked.get(path)
    assert response.status_code == 401
    assert response.get_json()["auth_required"] is True


@pytest.mark.parametrize("path", [
    "/api/scan", "/api/batch/start", "/api/batch/finish", "/api/setup/reset",
    "/api/test/scanner", "/api/discover/scanners", "/api/ha/key",
])
def test_acting_is_closed_without_a_session(locked, path):
    assert locked.post(path, json={}).status_code == 401


def test_settings_cannot_be_changed_without_a_session(locked, deck):
    assert locked.put("/api/config", json={"paperless_token": "geklaut"}).status_code == 401
    assert deck.store.get()["paperless_token"] == ""


def test_the_health_probe_stays_open(locked):
    """The container's own healthcheck must not need a password."""
    assert locked.get("/health").status_code == 200


def test_the_shell_stays_reachable_so_the_login_can_be_shown(locked):
    assert locked.get("/").status_code == 200
    assert locked.get("/sw.js").status_code == 200
    assert locked.get("/manifest.webmanifest").status_code == 200
    assert locked.get("/api/auth/state").get_json() == {
        "enabled": True, "authenticated": False, "min_length": 8,
    }


def test_home_assistant_keeps_working_on_its_own_key(locked, deck):
    """An automation must not break because a password was added."""
    key = deck.store.patch(ha_api_key="ha-schluessel", ha_enabled=True) and "ha-schluessel"
    assert locked.get("/api/ha/state", headers={"X-API-Key": key}).status_code == 200
    assert locked.post("/api/ha/test", headers={"X-API-Key": key}, json={}).status_code == 200
    # And a wrong key is still a wrong key.
    assert locked.get("/api/ha/state", headers={"X-API-Key": "falsch"}).status_code == 401


# --- logging in ------------------------------------------------------------ #

def test_the_right_password_opens_the_door(locked):
    assert locked.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert locked.get("/api/config").status_code == 200


def test_a_wrong_password_does_not(locked):
    assert locked.post("/api/auth/login", json={"password": "daneben"}).status_code == 401
    assert locked.post("/api/auth/login", json={}).status_code == 401
    assert locked.get("/api/config").status_code == 401


def test_logging_out_closes_the_door_again(locked):
    locked.post("/api/auth/login", json={"password": PASSWORD})
    assert locked.post("/api/auth/logout").status_code == 200
    assert locked.get("/api/config").status_code == 401


def test_repeated_wrong_passwords_are_throttled(locked, deck):
    for _ in range(deck.auth.MAX_FAILURES):
        locked.post("/api/auth/login", json={"password": "daneben"})
    response = locked.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 429  # even the right one has to wait now
    assert "warten" in response.get_json()["error"]


def test_a_successful_login_clears_the_failure_count(locked, deck):
    for _ in range(deck.auth.MAX_FAILURES - 1):
        locked.post("/api/auth/login", json={"password": "daneben"})
    assert locked.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert deck.login_throttle.blocked_seconds() == 0


# --- changing and switching off -------------------------------------------- #

def test_changing_the_password_signs_other_browsers_out(client, deck):
    enable(client)
    other = deck.app.test_client()
    other.post("/api/auth/login", json={"password": PASSWORD})
    assert other.get("/api/config").status_code == 200

    assert client.post("/api/auth/password",
                       json={"current": PASSWORD, "password": "neues-passwort"}).status_code == 200
    # The one that changed it stays in, everyone else has to sign in again.
    assert client.get("/api/config").status_code == 200
    assert other.get("/api/config").status_code == 401
    assert other.post("/api/auth/login", json={"password": "neues-passwort"}).status_code == 200


def test_changing_needs_the_current_password(client):
    enable(client)
    response = client.post("/api/auth/password", json={"current": "falsch", "password": "neues-passwort"})
    assert response.status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def test_switching_off_opens_everything_again(client, deck):
    enable(client)
    assert client.post("/api/auth/disable").status_code == 200
    assert deck.app.test_client().get("/api/config").status_code == 200
    assert deck.store.get()["auth_password_hash"] == ""


def test_switching_off_needs_a_session(locked):
    assert locked.post("/api/auth/disable").status_code == 401


# --- consistency ----------------------------------------------------------- #

def test_protection_cannot_be_turned_on_through_the_settings_endpoint(client, deck):
    """A password belongs in its own route, not in a bulk save."""
    client.put("/api/config", json={"auth_enabled": True, "auth_password_hash": "x"})
    assert deck.store.get()["auth_enabled"] is False
    assert deck.store.get()["auth_password_hash"] == ""
    assert deck.app.test_client().get("/api/config").status_code == 200


def test_a_flag_without_a_password_never_locks_anyone_out(deck):
    """Config edited by hand must not produce a door without a key."""
    config = deck.validate_config({**deck.store.get(), "auth_enabled": True, "auth_password_hash": ""})
    assert config["auth_enabled"] is False


def test_the_session_survives_a_restart(deck, tmp_path):
    """The signing key is stored, so a redeploy does not sign everyone out."""
    first = deck.store.get()["session_secret"]
    assert first
    from scandeck.config import ConfigStore

    assert ConfigStore(deck.CONFIG_PATH).get()["session_secret"] == first


def test_password_verification_survives_a_broken_hash(deck):
    assert deck.auth.verify_password("kein-gueltiger-hash", PASSWORD) is False
    assert deck.auth.verify_password("", PASSWORD) is False
    assert deck.auth.verify_password(deck.auth.hash_password(PASSWORD), "") is False

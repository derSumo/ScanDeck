"""The Home Assistant interface, network guessing and the preview cache."""

from __future__ import annotations

import pytest


# --- Home Assistant -------------------------------------------------------- #

def enable_ha(client):
    return client.post("/api/ha/key").get_json()["api_key"]


def test_ha_endpoints_are_closed_while_the_interface_is_off(client):
    for path in ("/api/ha/scan", "/api/ha/test", "/api/ha/batch"):
        assert client.post(path, json={}).status_code == 403
    assert client.get("/api/ha/state").status_code == 403


def test_ha_rejects_a_wrong_or_missing_key(client):
    enable_ha(client)
    assert client.post("/api/ha/test", json={}).status_code == 401
    assert client.post("/api/ha/test", headers={"X-API-Key": "falsch"}, json={}).status_code == 401


def test_ha_accepts_every_documented_way_to_pass_the_key(client):
    key = enable_ha(client)
    assert client.post("/api/ha/test", headers={"X-API-Key": key}, json={}).status_code == 200
    assert client.post("/api/ha/test", headers={"Authorization": f"Bearer {key}"}, json={}).status_code == 200
    assert client.post(f"/api/ha/test?api_key={key}", json={}).status_code == 200


def test_rotating_the_key_invalidates_the_old_one(client):
    old = enable_ha(client)
    new = enable_ha(client)
    assert old != new
    assert client.post("/api/ha/test", headers={"X-API-Key": old}, json={}).status_code == 401
    assert client.post("/api/ha/test", headers={"X-API-Key": new}, json={}).status_code == 200


def test_ha_state_reports_what_an_automation_needs(client, deck):
    key = enable_ha(client)
    body = client.get("/api/ha/state", headers={"X-API-Key": key}).get_json()
    assert body["state"] == "idle"
    assert body["running"] is False
    assert body["batch_pages"] == 0
    assert body["version"] == deck.APP_VERSION


def test_ha_batch_rejects_an_unknown_action(client):
    key = enable_ha(client)
    response = client.post("/api/ha/batch", headers={"X-API-Key": key}, json={"action": "tanzen"})
    assert response.status_code == 400


def test_ha_and_the_interface_drive_the_same_batch(client, deck):
    key = enable_ha(client)
    client.post("/api/ha/batch", headers={"X-API-Key": key}, json={"action": "start"})
    assert client.get("/api/batch").get_json()["active"] is True

    client.post("/api/ha/batch", headers={"X-API-Key": key}, json={"action": "cancel"})
    assert deck.batch.active() is False


def test_ha_scan_is_refused_while_one_runs(client, deck):
    key = enable_ha(client)
    deck.scan_lock.acquire()
    try:
        response = client.post("/api/ha/scan", headers={"X-API-Key": key}, json={})
        assert response.status_code == 409
    finally:
        deck.scan_lock.release()


def test_ha_tags_may_arrive_as_a_string_or_a_list(deck):
    assert deck.parse_session_tags("Rechnung, Steuer ,Rechnung") == ["Rechnung", "Steuer"]
    assert deck.parse_session_tags(["A", " ", "A", "B"]) == ["A", "B"]
    assert deck.parse_session_tags(None) == []
    assert deck.parse_session_tags(17) == []


# --- network guessing ------------------------------------------------------ #

def test_subnet_of_accepts_only_usable_private_addresses(deck):
    from scandeck.network import subnet_of

    assert subnet_of("192.168.178.42") == "192.168.178.0/24"
    for bad in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "keine ip", ""):
        assert subnet_of(bad) == ""


def test_container_networks_are_ranked_last(deck):
    from scandeck.network import is_container_network

    assert is_container_network("172.17.0.0/24") is True
    assert is_container_network("192.168.1.0/24") is False

    config = {"paperless_url": "", "scanner_url": ""}
    candidates = deck.candidate_subnets(config, "172.17.0.5")
    assert candidates[0] != "172.17.0.0/24"
    assert "172.17.0.0/24" in candidates


def test_the_browsers_network_is_tried_first(deck):
    config = {"paperless_url": "http://10.0.0.9:8000", "scanner_url": ""}
    assert deck.candidate_subnets(config, "192.168.5.20")[0] == "192.168.5.0/24"
    # And the known Paperless host is still worth a look.
    assert "10.0.0.0/24" in deck.candidate_subnets(config, "192.168.5.20")


def test_candidates_endpoint_returns_a_short_list(client):
    candidates = client.get("/api/discover/candidates").get_json()["candidates"]
    assert 0 < len(candidates) <= 4
    assert len(candidates) == len(set(candidates))


# --- preview cache --------------------------------------------------------- #

def test_preview_cache_only_answers_for_the_page_it_holds(deck):
    from scandeck.documents import PreviewCache

    cache = PreviewCache()
    cache.put(("/scans/a.pdf", 0), (b"seite-a", "image/png"))

    assert cache.get(("/scans/a.pdf", 0)) == (b"seite-a", "image/png")
    assert cache.get(("/scans/b.pdf", 0)) is None  # never another document
    assert cache.get(("/scans/a.pdf", 90)) is None  # nor the unrotated version

    cache.clear()
    assert cache.get(("/scans/a.pdf", 0)) is None


def test_preview_of_an_image_is_returned_untouched(deck, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "scan.jpg"
    Image.new("RGB", (40, 60)).save(path, "JPEG")

    payload, mimetype = deck.render_preview(path)
    assert mimetype == "image/jpeg"
    assert payload == path.read_bytes()
    # Second call comes from the cache and is identical.
    assert deck.render_preview(path) == (payload, mimetype)


def test_preview_endpoints_answer_404_without_a_scan(client):
    assert client.get("/api/preview").status_code == 404
    assert client.get("/api/preview/file").status_code == 404
    assert client.get("/api/history/unbekannt/preview").status_code == 404


# --- misc ------------------------------------------------------------------ #

def test_health_and_update_never_block_the_interface(client, deck):
    assert client.get("/health").get_json()["ok"] is True
    deck.store.patch(update_check=False)
    body = client.get("/api/update").get_json()
    assert body["disabled"] is True
    assert body["update_available"] is False


def test_a_second_scan_is_refused_while_one_runs(client, deck):
    deck.scan_lock.acquire()
    try:
        assert client.post("/api/scan", json={}).status_code == 409
    finally:
        deck.scan_lock.release()


def test_service_worker_carries_the_version(client, deck):
    body = client.get("/sw.js")
    assert body.headers["Content-Type"].startswith("application/javascript")
    assert deck.APP_VERSION in body.get_data(as_text=True)

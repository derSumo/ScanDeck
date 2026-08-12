"""The upload queue: nothing is lost, nothing is uploaded twice."""

from __future__ import annotations

import time


def make_scan(deck, tmp_path, name="scan.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 fake")
    return path


def test_history_is_trimmed_in_memory_too(deck, tmp_path):
    store = deck.JobStore(tmp_path / "jobs.json")
    store.MAX_JOBS = 10
    for index in range(25):
        store.add(tmp_path / f"scan_{index}.pdf", "success")
    assert len(store._jobs) == 10
    # What the interface can list is what a restart would find again.
    assert len(store.list(limit=500)) == len(store._load())


def test_open_uploads_survive_the_trim(deck, tmp_path):
    store = deck.JobStore(tmp_path / "jobs.json")
    store.MAX_JOBS = 5
    open_job = store.add(tmp_path / "wichtig.pdf", "pending")
    for index in range(20):
        store.add(tmp_path / f"scan_{index}.pdf", "success")
    assert store.get(open_job["id"]) is not None
    assert store.pending_count() == 1


def test_claim_hands_a_job_to_exactly_one_worker(deck, tmp_path):
    store = deck.JobStore(tmp_path / "jobs.json")
    job = store.add(tmp_path / "scan.pdf", "pending")

    assert store.claim(job["id"]) is not None
    # A second attempt beside the first would file a duplicate in Paperless.
    assert store.claim(job["id"]) is None
    assert store.claim("gibtsnicht") is None


def test_claim_refuses_jobs_that_are_not_due_yet(deck, tmp_path):
    store = deck.JobStore(tmp_path / "jobs.json")
    job = store.add(tmp_path / "scan.pdf", "pending")
    store.update(job["id"], next_attempt=time.time() + 600)
    assert store.claim(job["id"]) is None


def test_retry_delays_grow_and_then_level_off(deck, tmp_path):
    store = deck.JobStore(tmp_path / "jobs.json")
    job = store.add(tmp_path / "scan.pdf", "pending")
    delays = []
    for _ in range(7):
        before = time.time()
        store.schedule_retry(job["id"], "Paperless nicht erreichbar")
        delays.append(round(store.get(job["id"])["next_attempt"] - before))
    assert delays == [30, 120, 300, 900, 3600, 3600, 3600]
    assert store.get(job["id"])["status"] == "pending"


def test_a_failed_upload_stays_in_the_queue(deck, tmp_path, monkeypatch):
    path = make_scan(deck, tmp_path)
    config = {**deck.store.get(), "upload_to_paperless": True,
              "paperless_url": "http://paperless:8000", "paperless_token": "t"}

    def explode(self, file_path):
        raise deck.requests.ConnectionError("Netzwerk weg")

    monkeypatch.setattr(deck.PaperlessClient, "upload", explode)
    job = deck.jobs.add(path, "pending")
    assert deck.attempt_upload(job["id"], config) is False

    stored = deck.jobs.get(job["id"])
    assert stored["status"] == "pending"  # retried, not dropped
    assert stored["attempts"] == 1
    assert path.exists()  # and the scan is still on disk


def test_a_missing_file_fails_instead_of_retrying_forever(deck, tmp_path):
    job = deck.jobs.add(tmp_path / "weg.pdf", "pending")
    assert deck.attempt_upload(job["id"], deck.store.get()) is False
    assert deck.jobs.get(job["id"])["status"] == "failed"


def test_cleanup_only_removes_confirmed_documents(deck, tmp_path):
    config = {**deck.store.get(), "cleanup_enabled": True, "cleanup_hours": 1}
    old = "2020-01-01T00:00:00"

    confirmed = make_scan(deck, tmp_path, "bestaetigt.pdf")
    pending = make_scan(deck, tmp_path, "offen.pdf")
    rejected = make_scan(deck, tmp_path, "abgelehnt.pdf")

    deck.jobs.add(confirmed, "success")
    deck.jobs.add(pending, "pending")
    deck.jobs.add(rejected, "failed")
    for job in deck.jobs.list():
        deck.jobs.update(job["id"], confirmed_at=old)

    deck.cleanup_uploaded(config)
    assert not confirmed.exists()
    assert pending.exists()
    assert rejected.exists()


def test_cleanup_respects_the_grace_period(deck, tmp_path):
    config = {**deck.store.get(), "cleanup_enabled": True, "cleanup_hours": 24}
    path = make_scan(deck, tmp_path, "frisch.pdf")
    deck.jobs.add(path, "success")
    deck.cleanup_uploaded(config)
    assert path.exists()


def test_history_never_reveals_server_paths(client, deck, tmp_path):
    deck.jobs.add(make_scan(deck, tmp_path), "success")
    entries = client.get("/api/history").get_json()["jobs"]
    assert entries and "path" not in entries[0]
    assert entries[0]["exists"] is True


def test_deleting_a_locked_file_keeps_the_entry(client, deck, tmp_path, monkeypatch):
    """Losing the entry would leave a scan on disk that nothing can reach."""
    job = deck.jobs.add(make_scan(deck, tmp_path), "success")

    def refuse(self, missing_ok=False):
        raise PermissionError(13, "Zugriff verweigert")

    monkeypatch.setattr(deck.Path, "unlink", refuse)
    assert client.delete(f"/api/history/{job['id']}").status_code == 500
    assert deck.jobs.get(job["id"]) is not None


def test_deleting_removes_entry_and_file(client, deck, tmp_path):
    path = make_scan(deck, tmp_path)
    job = deck.jobs.add(path, "success")
    assert client.delete(f"/api/history/{job['id']}").status_code == 200
    assert deck.jobs.get(job["id"]) is None
    assert not path.exists()

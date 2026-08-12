"""The upload queue and the scan-duration memory, both backed by one JSON file."""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class JobStore:
    """Every scan and what became of it, so nothing gets lost silently.

    A scan is only ever deleted once Paperless confirmed the document. Until
    then the entry stays in the queue and is retried, which also means the
    cleanup can never remove a file whose upload is still open.
    """

    # pending  -> waiting for its (first or next) upload attempt
    # processing -> Paperless accepted the file, task is running
    # success  -> document exists in Paperless, file may be cleaned up
    # duplicate/failed -> needs a decision, file is kept
    # local    -> upload is switched off, file is kept
    OPEN_STATES = ("pending", "processing")
    RETRY_DELAYS = (30, 120, 300, 900, 3600)  # seconds, then hourly
    MAX_JOBS = 500  # kept in memory and on disk alike

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._jobs = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            return stored if isinstance(stored, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._jobs, indent=2), encoding="utf-8")
        except OSError as error:
            print(f"ScanDeck: Verlauf nicht gespeichert: {error}", file=sys.stderr, flush=True)

    def add(self, file_path: Path, status: str, pages: int = 1, tags: list[str] | None = None) -> dict[str, Any]:
        job = {
            "id": secrets.token_hex(8),
            "name": file_path.name,
            "path": str(file_path),
            "created": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "pages": pages,
            "tags": tags or [],
            "task_id": None,
            "document_id": None,
            "error": None,
            "attempts": 0,
            "next_attempt": 0.0,
            "confirmed_at": None,
            "file_deleted": False,
        }
        with self._lock:
            self._jobs.append(job)
            # Trim here, not while writing: otherwise the list in memory grows
            # without end and reports entries the file no longer holds.
            if len(self._jobs) > self.MAX_JOBS:
                keep = self._jobs[-self.MAX_JOBS:]
                kept_ids = {entry["id"] for entry in keep}
                # An unfinished upload is never dropped, however old it is.
                self._jobs = [entry for entry in self._jobs
                              if entry["id"] not in kept_ids
                              and entry["status"] in self.OPEN_STATES] + keep
            self._write()
        return job.copy()

    def update(self, job_id: str, **values: Any) -> dict[str, Any] | None:
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    job.update(values)
                    self._write()
                    return job.copy()
        return None

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((job.copy() for job in self._jobs if job["id"] == job_id), None)

    def remove(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            for index, job in enumerate(self._jobs):
                if job["id"] == job_id:
                    removed = self._jobs.pop(index)
                    self._write()
                    return removed
        return None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [job.copy() for job in reversed(self._jobs[-limit:])]

    def due(self, status: str) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [job.copy() for job in self._jobs
                    if job["status"] == status and job.get("next_attempt", 0) <= now]

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs if job["status"] in self.OPEN_STATES)

    def claim(self, job_id: str, hold: float = 180.0) -> dict[str, Any] | None:
        """Take a pending job for one upload attempt, or return None.

        Pushing "next_attempt" out under the lock is what keeps the queue worker
        from starting a second attempt beside a running one — which Paperless
        would file as a duplicate.
        """
        now = time.time()
        with self._lock:
            for job in self._jobs:
                if job["id"] != job_id:
                    continue
                if job["status"] != "pending" or job.get("next_attempt", 0) > now:
                    return None
                job["next_attempt"] = now + hold
                self._write()
                return job.copy()
        return None

    def schedule_retry(self, job_id: str, error: str) -> None:
        job = self.get(job_id)
        attempts = int(job["attempts"]) + 1 if job else 1
        delay = self.RETRY_DELAYS[min(attempts - 1, len(self.RETRY_DELAYS) - 1)]
        self.update(job_id, status="pending", attempts=attempts, error=error, next_attempt=time.time() + delay)


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

"""The live log and progress stream every connected interface listens to."""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterator

# Every open interface holds one connection, and every connection holds one
# worker thread for as long as it lives. Both numbers are therefore bounded.
MAX_SUBSCRIBERS = 24
HEARTBEAT_SECONDS = 15
# Streams are closed after a while so a tab that went away for good cannot keep
# a thread forever; EventSource reconnects on its own within a few seconds.
MAX_STREAM_SECONDS = 15 * 60


class TooManySubscribers(RuntimeError):
    """Raised instead of silently starving the worker pool."""


class LogHub:
    """A small in-memory event fan-out for the single-process Docker service."""

    def __init__(self) -> None:
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._lock = threading.Lock()
        self.history: list[dict[str, Any]] = []
        # app.py mirrors progress into the scan state; the hub itself stays
        # ignorant of what a scan is.
        self.on_progress: Callable[[str, int], None] | None = None

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
        if self.on_progress:
            self.on_progress(stage, percent)
        self._emit({"kind": "progress", "stage": stage, "progress": percent, **extra})

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

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
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                raise TooManySubscribers
            self._subscribers.append(subscriber)
            history = self.history.copy()
        started = time.monotonic()
        try:
            for event in history:
                yield f"data: {json.dumps(event)}\n\n"
            while time.monotonic() - started < MAX_STREAM_SECONDS:
                try:
                    event = subscriber.get(timeout=HEARTBEAT_SECONDS)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)


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
        self._extra: dict[str, Any] = {}

    def note(self, **extra: Any) -> None:
        """Attach detail (e.g. the page count) to the ticks from here on."""
        self._extra = extra

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
            self.log.progress(self.stage, round(min(current, self.end)), eta=eta, **self._extra)

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        if self._thread:
            self._thread.join(timeout=1)

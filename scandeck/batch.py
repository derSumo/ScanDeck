"""Collecting single sheets into one document, page by page."""

from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path
from typing import Any

from scandeck.documents import merge_pages, normalise_rotation, split_pages


class BatchCollector:
    """The pages gathered for a multi-page document, and their order.

    Everything here runs under one lock. Pages arrive from the scan thread while
    the interface reorders, rotates and deletes them from request threads.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()
        self._active = False
        self._pages: list[dict[str, Any]] = []
        self._replace_index: int | None = None
        self._counter = itertools.count()

    # --- state ------------------------------------------------------------ #

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "replace_index": self._replace_index,
                "pages": [
                    {
                        "index": index,
                        "name": page["name"],
                        "kind": page["kind"],
                        "rotation": page.get("rotation", 0),
                    }
                    for index, page in enumerate(self._pages)
                ],
            }

    def active(self) -> bool:
        with self._lock:
            return self._active

    def count(self) -> int:
        with self._lock:
            return len(self._pages)

    def pages(self) -> list[dict[str, Any]]:
        with self._lock:
            return [page.copy() for page in self._pages]

    def page(self, index: int) -> dict[str, Any] | None:
        with self._lock:
            return self._pages[index].copy() if 0 <= index < len(self._pages) else None

    # --- lifecycle -------------------------------------------------------- #

    def begin(self) -> dict[str, Any]:
        """Discard whatever was collected before and open a fresh batch."""
        self.clear()
        with self._lock:
            self._active = True
        return self.public()

    def clear(self) -> None:
        """Drop every collected page and its file."""
        with self._lock:
            doomed = [page["path"] for page in self._pages]
            self._active = False
            self._pages = []
            self._replace_index = None
        for path in doomed:
            _unlink(Path(path))

    # --- adding pages ----------------------------------------------------- #

    def add(self, sources: list[Path]) -> tuple[int, int, bool, int]:
        """Take everything one scan run produced into the batch.

        A feeder run hands over several sheets at once, and a scanner may answer
        the whole run with one multi-page PDF — both end up as one tile per
        sheet, so every page can be reordered, turned or dropped on its own.
        Returns the first page's position, the new total, whether it replaced an
        existing page, and how many pages arrived.
        """
        entries: list[dict[str, Any]] = []
        for source in sources:
            for path in self._ingest(source):
                entries.append({
                    "name": path.name,
                    "path": str(path),
                    "kind": "pdf" if path.suffix.lower() == ".pdf" else "image",
                    "rotation": 0,
                })
        if not entries:
            raise RuntimeError("Der Scan hat keine Seite ergeben.")

        with self._lock:
            index = self._replace_index
            self._replace_index = None
            if index is not None and 0 <= index < len(self._pages):
                # The armed sheet is swapped for the first new one; anything
                # else the feeder pulled in follows directly behind it.
                previous = self._pages[index]
                self._pages[index:index + 1] = entries
                _unlink(Path(previous["path"]))
                return index, len(self._pages), True, len(entries)
            start = len(self._pages)
            self._pages.extend(entries)
            return start, len(self._pages), False, len(entries)

    def _ingest(self, source: Path) -> list[Path]:
        """Move one scanned file into the batch directory, one file per page."""
        self.directory.mkdir(parents=True, exist_ok=True)
        staged = self.directory / f"stage_{time.time_ns()}{source.suffix.lower()}"
        staged.write_bytes(source.read_bytes())
        _unlink(source)
        stored = []
        for part in split_pages(staged, self.directory):
            target = self.directory / f"page_{next(self._counter):04d}_{time.time_ns()}{part.suffix.lower()}"
            part.rename(target)
            stored.append(target)
        return stored

    # --- editing ---------------------------------------------------------- #

    def arm(self, index: int | None) -> bool:
        """Point the next scan at one page instead of the end of the stack."""
        with self._lock:
            if index is None:
                self._replace_index = None
                return True
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self._pages):
                return False
            self._replace_index = index
            return True

    def remove(self, index: int) -> bool:
        with self._lock:
            if not 0 <= index < len(self._pages):
                return False
            page = self._pages.pop(index)
            self._replace_index = None
        _unlink(Path(page["path"]))
        return True

    def rotate(self, index: int, degrees: int) -> bool:
        with self._lock:
            if not 0 <= index < len(self._pages):
                return False
            page = self._pages[index]
            page["rotation"] = (page.get("rotation", 0) + normalise_rotation(degrees)) % 360
            return True

    def rotate_all(self, degrees: int) -> int:
        """Turn every page at once — a feeder stack is usually wrong as a whole."""
        step = normalise_rotation(degrees)
        with self._lock:
            for page in self._pages:
                page["rotation"] = (page.get("rotation", 0) + step) % 360
            return len(self._pages)

    def reverse(self) -> int:
        """Flip the order. Many feeders deliver the last sheet first."""
        with self._lock:
            self._pages.reverse()
            if self._replace_index is not None:
                self._replace_index = len(self._pages) - 1 - self._replace_index
            return len(self._pages)

    def reorder(self, order: Any) -> bool:
        # Every position exactly once, and integers only: sorting a mixed list
        # would raise instead of answering with a readable error.
        valid = isinstance(order, list) and all(
            isinstance(position, int) and not isinstance(position, bool) for position in order
        )
        with self._lock:
            if not valid or sorted(order) != list(range(len(self._pages))):
                return False
            armed = self._replace_index
            self._pages = [self._pages[position] for position in order]
            if armed is not None:
                # Keep the armed page armed, even though it sits somewhere else now.
                self._replace_index = order.index(armed) if armed in order else None
            return True

    # --- finishing -------------------------------------------------------- #

    def merge_into(self, target: Path) -> int:
        pages = self.pages()
        if not pages:
            raise RuntimeError("Der Stapel enthält keine Seiten.")
        return merge_pages(pages, target)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # a locked or already removed file must not break the batch

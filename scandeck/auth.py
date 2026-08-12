"""Optional password protection for the interface.

Off by default: ScanDeck is built for a trusted home network, and a lock on the
door of a device that only answers on the LAN would be in the way more often
than it helps. Switched on, it covers the whole interface and every API route
except the ones that must stay reachable — the health probe the container
itself uses, the login, and the Home Assistant endpoints, which carry their own
key.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 8
# Guessing protection sized for a household, not for the open internet: a wrong
# password a few times in a row is a typo, a hundred times is not.
MAX_FAILURES = 10
LOCKOUT_SECONDS = 300
FAILURE_WINDOW = 300

# Reachable without a session: the app shell (so the login can be drawn), the
# login itself, and the health probe the container runs against itself.
# Everything else is closed once protection is on — including /api/ha/key,
# which hands out the Home Assistant key and checks nothing of its own.
OPEN_ENDPOINTS = frozenset({
    "static",
    "health",
    "index",
    "manifest",
    "service_worker",
    "auth_state",
    "auth_login",
})


def hash_password(password: str) -> str:
    return generate_password_hash(str(password))


def verify_password(stored_hash: str, password: str) -> bool:
    if not stored_hash or not password:
        return False
    try:
        return check_password_hash(stored_hash, str(password))
    except (ValueError, TypeError):
        return False  # a corrupted hash must not raise, only refuse


def check_password_rules(password: str) -> str:
    """Return the password to store, or raise ValueError with the reason."""
    password = str(password or "")
    if len(password.strip()) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Das Passwort braucht mindestens {MIN_PASSWORD_LENGTH} Zeichen.")
    return password


class LoginThrottle:
    """Delays repeated wrong passwords without locking anyone out for long."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: list[float] = []
        self._blocked_until = 0.0

    def blocked_seconds(self) -> int:
        with self._lock:
            return max(0, round(self._blocked_until - time.time()))

    def record_failure(self) -> None:
        now = time.time()
        with self._lock:
            self._failures = [stamp for stamp in self._failures if now - stamp < FAILURE_WINDOW]
            self._failures.append(now)
            if len(self._failures) >= MAX_FAILURES:
                self._blocked_until = now + LOCKOUT_SECONDS
                self._failures = []

    def reset(self) -> None:
        with self._lock:
            self._failures = []
            self._blocked_until = 0.0


def is_open(endpoint: str | None) -> bool:
    """May this request be served without a session?"""
    return endpoint in OPEN_ENDPOINTS


def public_state(config: dict[str, Any], authenticated: bool) -> dict[str, Any]:
    return {
        "enabled": bool(config.get("auth_enabled")),
        "authenticated": bool(authenticated or not config.get("auth_enabled")),
        "min_length": MIN_PASSWORD_LENGTH,
    }

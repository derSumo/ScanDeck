"""Is there a newer ScanDeck? Asked rarely, cached, never blocking."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

from scandeck.version import APP_VERSION, GITHUB_REPO, RELEASES_URL, USER_AGENT

UPDATE_CACHE_SECONDS = 6 * 3600

update_cache: dict[str, Any] = {"checked_at": 0.0, "latest": "", "url": RELEASES_URL, "error": ""}
update_lock = threading.Lock()


def parse_version(value: str) -> tuple[int, ...]:
    """Turn "v1.2.3" into (1, 2, 3); anything unparsable sorts lowest."""
    core = re.split(r"[-+]", str(value or "").strip().lstrip("vV"), maxsplit=1)[0]
    parts = [int(part) for part in re.findall(r"\d+", core)[:3]]
    return tuple(parts + [0] * (3 - len(parts))) if parts else (0, 0, 0)


def fetch_latest_release() -> dict[str, str]:
    """The newest published release, or the newest semver tag if none exists."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    response = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers=headers,
        timeout=8,
    )
    if response.status_code != 404:
        response.raise_for_status()
        body = response.json()
        return {
            "latest": str(body.get("tag_name") or body.get("name") or "").lstrip("vV"),
            "url": body.get("html_url") or RELEASES_URL,
            "error": "",
        }

    tags = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/tags?per_page=100",
                        headers=headers, timeout=8)
    tags.raise_for_status()
    newest = max((str(tag.get("name", "")) for tag in tags.json()), key=parse_version, default="")
    return {
        "latest": newest.lstrip("vV"),
        "url": f"{RELEASES_URL}/tag/{newest}" if newest else RELEASES_URL,
        "error": "",
    }


def check_for_update(force: bool = False) -> dict[str, Any]:
    """Ask GitHub for the newest release, at most once every few hours.

    The lock only guards the cache. Holding it across the request would queue
    every other caller behind a GitHub round trip — and each waiting caller
    occupies one of the worker threads the container has.
    """
    with update_lock:
        refresh = force or time.time() - update_cache["checked_at"] >= UPDATE_CACHE_SECONDS
        # Claim the refresh right away so parallel callers serve the cache
        # instead of all firing their own request.
        if refresh:
            update_cache["checked_at"] = time.time()

    if refresh:
        try:
            result = fetch_latest_release()
        except (requests.RequestException, ValueError) as error:
            result = {"error": str(error)}
        with update_lock:
            update_cache.update(result)

    with update_lock:
        latest = update_cache["latest"]
        return {
            "current": APP_VERSION,
            "latest": latest,
            "update_available": bool(latest) and parse_version(latest) > parse_version(APP_VERSION),
            "url": update_cache["url"],
            "checked_at": update_cache["checked_at"],
            "error": update_cache["error"],
        }

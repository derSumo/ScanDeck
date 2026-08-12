"""Web Push: a note on the phone when a scan is done and filed.

One puts the phone down while the scanner works, so the interesting moment —
Paperless confirmed the document — happens when nobody is looking. Web Push
reaches an installed app even when it is closed; the keys for it are generated
once and stay in config.json.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

TTL = 600  # after ten minutes a "scan finished" note is only noise


def generate_keys() -> tuple[str, str]:
    """A VAPID key pair: the public half identifies this server to the browser."""
    from py_vapid import Vapid01

    vapid = Vapid01()
    vapid.generate_keys()
    private_key = vapid.private_pem().decode("utf-8")
    # The browser expects the raw public key, base64url without padding.
    from cryptography.hazmat.primitives import serialization
    import base64

    raw = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("="), private_key


class PushService:
    """Sends notifications and keeps the subscription list tidy.

    Endpoints go stale when an app is uninstalled or the browser rotates them;
    the service drops those instead of retrying them forever.
    """

    def __init__(self, log: Any, load: Callable[[], dict[str, Any]],
                 save_subscriptions: Callable[[list[dict[str, Any]]], None]) -> None:
        self.log = log
        self.load = load
        self.save_subscriptions = save_subscriptions
        self._lock = threading.Lock()

    def subscribe(self, subscription: dict[str, Any]) -> int:
        """Remember one browser. Returns how many are subscribed now."""
        endpoint = str(subscription.get("endpoint", "")).strip()
        if not endpoint.startswith("https://"):
            raise ValueError("Ungültiges Push-Abonnement.")
        with self._lock:
            current = [entry for entry in self.load().get("push_subscriptions", [])
                       if entry.get("endpoint") != endpoint]
            current.append({"endpoint": endpoint, "keys": subscription.get("keys") or {}})
            self.save_subscriptions(current)
            return len(current)

    def unsubscribe(self, endpoint: str) -> int:
        with self._lock:
            current = [entry for entry in self.load().get("push_subscriptions", [])
                       if entry.get("endpoint") != endpoint]
            self.save_subscriptions(current)
            return len(current)

    def notify(self, title: str, body: str, tag: str = "scandeck") -> None:
        """Fire and forget — a failed note must never hold up a scan."""
        config = self.load()
        if not config.get("push_enabled") or not config.get("push_private_key"):
            return
        subscriptions = config.get("push_subscriptions") or []
        if not subscriptions:
            return
        threading.Thread(
            target=self._send_all,
            args=(config, subscriptions, {"title": title, "body": body, "tag": tag}),
            daemon=True,
        ).start()

    def _send_all(self, config: dict[str, Any], subscriptions: list[dict[str, Any]],
                  payload: dict[str, str]) -> None:
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:
            self.log.publish("Push-Bibliothek fehlt; Benachrichtigung entfällt.", "warning")
            return

        alive: list[dict[str, Any]] = []
        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info=subscription,
                    data=json.dumps(payload),
                    vapid_private_key=config["push_private_key"],
                    vapid_claims={"sub": "mailto:scandeck@localhost"},
                    ttl=TTL,
                )
                alive.append(subscription)
            except WebPushException as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                if status in (404, 410):
                    # The browser dropped this subscription for good.
                    self.log.publish("Ein Gerät empfängt keine Benachrichtigungen mehr.", "info")
                    continue
                self.log.publish(f"Push fehlgeschlagen: {error}", "warning")
                alive.append(subscription)
            except Exception as error:
                self.log.publish(f"Push fehlgeschlagen: {error}", "warning")
                alive.append(subscription)

        if len(alive) != len(subscriptions):
            with self._lock:
                self.save_subscriptions(alive)

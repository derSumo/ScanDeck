"""Home Assistant via MQTT: the scanner appears as a device, on its own.

The REST route needs a hand-written sensor in configuration.yaml. Over MQTT the
same information arrives as entities Home Assistant creates itself — a button
to start a scan, a sensor for what the scanner is doing, one for the queue and
one for the sheet feeder. Nothing to configure on the Home Assistant side.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from scandeck.version import APP_VERSION

DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "scandeck"
DEVICE_ID = "scandeck"

# Entities Home Assistant should create. Everything reads from one state topic,
# so a single publish keeps the whole device up to date.
ENTITIES: tuple[dict[str, Any], ...] = (
    {
        "kind": "sensor", "key": "state", "name": "Status",
        "icon": "mdi:scanner", "value": "{{ value_json.state }}",
    },
    {
        "kind": "sensor", "key": "progress", "name": "Fortschritt",
        "icon": "mdi:progress-clock", "unit": "%", "value": "{{ value_json.progress }}",
    },
    {
        "kind": "sensor", "key": "queue", "name": "Warteschlange",
        "icon": "mdi:tray-full", "value": "{{ value_json.queue }}",
    },
    {
        "kind": "sensor", "key": "last_file", "name": "Letzter Scan",
        "icon": "mdi:file-document", "value": "{{ value_json.last_file }}",
    },
    {
        "kind": "sensor", "key": "feeder", "name": "Einzug",
        "icon": "mdi:paper-roll", "value": "{{ value_json.feeder }}",
    },
    {
        "kind": "binary_sensor", "key": "running", "name": "Scannt",
        "device_class": "running", "value": "{{ 'ON' if value_json.running else 'OFF' }}",
    },
    {
        "kind": "binary_sensor", "key": "batch_active", "name": "Stapel offen",
        "icon": "mdi:layers", "value": "{{ 'ON' if value_json.batch_active else 'OFF' }}",
    },
)

# Buttons an automation can press. The payload is what arrives on the command
# topic; app.py maps it to the same actions the interface uses.
BUTTONS: tuple[dict[str, str], ...] = (
    {"key": "scan", "name": "Scan starten", "icon": "mdi:scanner"},
    {"key": "cancel", "name": "Scan abbrechen", "icon": "mdi:cancel"},
    {"key": "batch_start", "name": "Stapel starten", "icon": "mdi:layers-plus"},
    {"key": "batch_finish", "name": "Stapel ablegen", "icon": "mdi:content-save"},
    {"key": "batch_cancel", "name": "Stapel verwerfen", "icon": "mdi:layers-remove"},
)


def device_block(name: str) -> dict[str, Any]:
    return {
        "identifiers": [DEVICE_ID],
        "name": name,
        "manufacturer": "ScanDeck",
        "model": "eSCL Scan Hub",
        "sw_version": APP_VERSION,
    }


class MqttBridge:
    """Publishes state and listens for button presses. Never fatal.

    A broker that is down, misconfigured or gone must never stop a scan, so
    every failure here is logged and swallowed; the interface keeps working.
    """

    def __init__(self, log: Any, on_command: Callable[[str], None]) -> None:
        self.log = log
        self.on_command = on_command
        self._lock = threading.Lock()
        self._client: Any = None
        self._config: dict[str, Any] = {}
        self._last_state: dict[str, Any] = {}

    # --- lifecycle -------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        client = self._client
        return bool(client and client.is_connected())

    def apply(self, config: dict[str, Any]) -> None:
        """Start, stop or restart the bridge to match the settings."""
        relevant = {key: config.get(key) for key in
                    ("mqtt_enabled", "mqtt_host", "mqtt_port", "mqtt_user",
                     "mqtt_password", "mqtt_device_name")}
        with self._lock:
            if relevant == self._config and (self.connected or not relevant["mqtt_enabled"]):
                return
            self._config = relevant
        self.stop()
        if relevant["mqtt_enabled"] and relevant["mqtt_host"]:
            self.start(config)

    def start(self, config: dict[str, Any]) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.log.publish("MQTT-Bibliothek fehlt; Anbindung bleibt aus.", "warning")
            return

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"scandeck-{APP_VERSION}",
        )
        if config.get("mqtt_user"):
            client.username_pw_set(config["mqtt_user"], config.get("mqtt_password") or None)
        client.will_set(f"{BASE_TOPIC}/availability", "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message

        try:
            client.connect_async(config["mqtt_host"], int(config.get("mqtt_port") or 1883), keepalive=45)
            client.loop_start()
        except (OSError, ValueError) as error:
            self.log.publish(f"MQTT-Verbindung fehlgeschlagen: {error}", "warning")
            return
        self._client = client

    def stop(self) -> None:
        client, self._client = self._client, None
        if not client:
            return
        try:
            client.publish(f"{BASE_TOPIC}/availability", "offline", retain=True)
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    # --- callbacks -------------------------------------------------------- #

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, reason: Any, *_args: Any) -> None:
        if getattr(reason, "value", reason) != 0:
            self.log.publish(f"MQTT-Broker lehnt die Anmeldung ab ({reason}).", "warning")
            return
        self.log.publish("MQTT verbunden; Home Assistant legt die Geräte an.", "success")
        client.subscribe(f"{BASE_TOPIC}/command")
        client.publish(f"{BASE_TOPIC}/availability", "online", retain=True)
        self.announce()
        if self._last_state:
            self.publish_state(self._last_state)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        command = message.payload.decode("utf-8", "replace").strip().lower()
        try:
            self.on_command(command)
        except Exception as error:  # a bad command must not kill the loop
            self.log.publish(f"MQTT-Befehl „{command}“ fehlgeschlagen: {error}", "warning")

    # --- publishing ------------------------------------------------------- #

    def announce(self) -> None:
        """Tell Home Assistant which entities belong to this device."""
        client = self._client
        if not client:
            return
        device = device_block(self._config.get("mqtt_device_name") or "ScanDeck")
        common = {
            "device": device,
            "availability_topic": f"{BASE_TOPIC}/availability",
            "state_topic": f"{BASE_TOPIC}/state",
        }
        for entity in ENTITIES:
            payload = {
                **common,
                "name": entity["name"],
                "unique_id": f"{DEVICE_ID}_{entity['key']}",
                "object_id": f"{DEVICE_ID}_{entity['key']}",
                "value_template": entity["value"],
            }
            for extra in ("icon", "device_class"):
                if entity.get(extra):
                    payload[extra] = entity[extra]
            if entity.get("unit"):
                payload["unit_of_measurement"] = entity["unit"]
            client.publish(
                f"{DISCOVERY_PREFIX}/{entity['kind']}/{DEVICE_ID}/{entity['key']}/config",
                json.dumps(payload), retain=True,
            )
        for button in BUTTONS:
            client.publish(
                f"{DISCOVERY_PREFIX}/button/{DEVICE_ID}/{button['key']}/config",
                json.dumps({
                    "device": device,
                    "availability_topic": f"{BASE_TOPIC}/availability",
                    "name": button["name"],
                    "unique_id": f"{DEVICE_ID}_btn_{button['key']}",
                    "object_id": f"{DEVICE_ID}_{button['key']}",
                    "command_topic": f"{BASE_TOPIC}/command",
                    "payload_press": button["key"],
                    "icon": button["icon"],
                }), retain=True,
            )

    def publish_state(self, state: dict[str, Any]) -> None:
        self._last_state = state
        client = self._client
        if client:
            try:
                client.publish(f"{BASE_TOPIC}/state", json.dumps(state), retain=True)
            except Exception:
                pass

    def forget(self) -> None:
        """Remove the entities from Home Assistant again."""
        client = self._client
        if not client:
            return
        for entity in ENTITIES:
            client.publish(f"{DISCOVERY_PREFIX}/{entity['kind']}/{DEVICE_ID}/{entity['key']}/config",
                           "", retain=True)
        for button in BUTTONS:
            client.publish(f"{DISCOVERY_PREFIX}/button/{DEVICE_ID}/{button['key']}/config",
                           "", retain=True)

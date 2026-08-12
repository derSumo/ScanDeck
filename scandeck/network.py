"""Finding the scanner: which networks are worth a look, and which ports."""

from __future__ import annotations

import socket
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3

from scandeck.escl import parse_capabilities, scanner_session
from scandeck.events import LogHub

# eSCL is not only served over TLS on 443: Brother, Canon and Epson commonly
# answer in the clear on 80 or 8080, which the HTTPS-only probe never saw.
PROBE_PORTS: tuple[tuple[str, int], ...] = (
    ("https", 443),
    ("http", 80),
    ("http", 8080),
)
CONNECT_TIMEOUT = 0.35  # just enough to tell "nothing there" from "busy"
READ_TIMEOUT = 7  # slower HP firmware needs room to answer capabilities

# Typical home router defaults, tried when nothing better is known.
COMMON_SUBNETS = (
    "192.168.0.0/24",
    "192.168.1.0/24",
    "192.168.178.0/24",  # AVM Fritz!Box
    "192.168.2.0/24",
    "10.0.0.0/24",
)

CONTAINER_POOLS = ("172.17.0.0/16", "172.18.0.0/15", "10.88.0.0/16")


def guess_local_subnet() -> str:
    """Best guess for the /24 the container lives in, used to prefill the wizard."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()
    return subnet_of(address)


def subnet_of(address: str) -> str:
    """The /24 an IPv4 address belongs to, or "" if it is not a usable private one."""
    try:
        host = ip_address(str(address or "").strip())
    except ValueError:
        return ""
    if not isinstance(host, IPv4Address) or not host.is_private or host.is_loopback or host.is_link_local:
        return ""
    return str(ip_network(f"{host}/24", strict=False))


def is_container_network(subnet: str) -> bool:
    """Docker's own bridge ranges never hold the scanner, so they rank last."""
    try:
        network = ip_network(subnet, strict=False)
    except ValueError:
        return True
    return any(network.subnet_of(ip_network(pool)) for pool in CONTAINER_POOLS)


def candidate_subnets(config: dict[str, Any], client_address: str | None = None) -> list[str]:
    """Rank the networks worth probing, best hint first.

    The device that opens the interface sits on the same LAN as the scanner, so
    its address beats everything the container can see about itself: in Docker's
    default bridge mode the container only knows its 172.x network.
    """
    candidates: list[str] = []
    fallbacks: list[str] = []

    def add(subnet: str) -> None:
        # Container networks never hold the scanner, so they go to the very end
        # instead of wasting the first search round.
        target = fallbacks if is_container_network(subnet) else candidates
        if subnet and subnet not in candidates and subnet not in fallbacks:
            target.append(subnet)

    add(subnet_of(client_address or ""))
    for url in (config.get("paperless_url", ""), config.get("scanner_url", "")):
        host = urlparse(url).hostname if url else None
        if host:
            add(subnet_of(host))
    add(guess_local_subnet())
    for subnet in COMMON_SUBNETS:
        add(subnet)
    return candidates + fallbacks


def probe_address(address: str, verify_ssl: bool) -> dict[str, str] | None:
    """Ask one host on every known eSCL port; the first answer wins."""
    for scheme, port in PROBE_PORTS:
        base_url = f"{scheme}://{address}:{port}"
        try:
            # Avoid waiting for a full timeout on every unused address. The
            # actual capability request gets enough time for slower firmware.
            with socket.create_connection((address, port), timeout=CONNECT_TIMEOUT):
                pass
            response = scanner_session.get(
                f"{base_url}/eSCL/ScannerCapabilities",
                verify=verify_ssl,
                timeout=READ_TIMEOUT,
            )
            if response.status_code != 200:
                continue
            capabilities = parse_capabilities(response.text)
        except (OSError, requests.RequestException, ET.ParseError):
            continue
        return {
            "url": base_url,
            "model": capabilities["model"],
            "version": capabilities["version"],
            "sources": ", ".join(capabilities["sources"]),
        }
    return None


def discover_escl_scanners(config: dict[str, Any], log: LogHub) -> list[dict[str, str]]:
    """Probe a bounded private IPv4 subnet for eSCL ScannerCapabilities."""
    subnet = config.get("discovery_subnet") or guess_local_subnet()
    if not subnet:
        raise ValueError("Kein Netzwerk für die Suche angegeben.")
    network = ip_network(subnet, strict=False)
    if not isinstance(network, IPv4Network):
        raise ValueError("Die Scanner-Suche akzeptiert nur IPv4-Netze.")
    verify_ssl = config["verify_scanner_ssl"]
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log.publish(f"Suche eSCL-Scanner in {network} …")

    devices: list[dict[str, str]] = []
    addresses = [str(host) for host in network.hosts()]
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(probe_address, address, verify_ssl) for address in addresses]
        for future in as_completed(futures):
            device = future.result()
            if device:
                devices.append(device)
    devices.sort(key=lambda device: device["url"])
    log.publish(
        f"Scanner-Suche abgeschlossen · {len(devices)} Gerät(e) gefunden.",
        "success" if devices else "warning",
    )
    return devices


def autodiscover_scanners(
    config: dict[str, Any],
    log: LogHub,
    client_address: str | None = None,
    limit: int = 4,
) -> tuple[list[dict[str, str]], str]:
    """Probe the likeliest networks in order and stop at the first hit."""
    candidates = candidate_subnets(config, client_address)[:limit]
    log.publish(f"Automatische Suche in: {', '.join(candidates)}")
    for subnet in candidates:
        devices = discover_escl_scanners({**config, "discovery_subnet": subnet}, log)
        if devices:
            return devices, subnet
    return [], ""

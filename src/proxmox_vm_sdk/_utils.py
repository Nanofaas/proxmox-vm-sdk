from __future__ import annotations

from urllib.parse import urlparse


def parse_proxmox_url(api_url: str) -> tuple[str, int]:
    """Extract host and port from a Proxmox API URL.

    Handles forms like:
      https://192.168.1.100:8006/api2/json
      http://proxmox-host:8006
      proxmox-host

    Returns (host, port).
    """
    if "://" not in api_url:
        api_url = f"https://{api_url}"
    parsed = urlparse(api_url)
    return parsed.hostname or "localhost", parsed.port or 8006

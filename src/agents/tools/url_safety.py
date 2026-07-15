"""Public-network URL checks shared by web-facing agent tools.

Search normalization uses the syntax check before returning citations. The web
crawler also resolves every requested hostname so literal and DNS-resolved
private addresses are rejected before the browser can reach internal services.
"""

from __future__ import annotations

import asyncio
import socket
from ipaddress import ip_address
from urllib.parse import urlsplit

MAX_PUBLIC_URL_CHARS = 2_048
_PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".internal")
# DNS is a preflight guard, not the research task itself; fail closed quickly
# when a resolver is unavailable instead of pinning an agent turn.
_DNS_TIMEOUT_SECONDS = 3.0


def is_safe_public_http_url(url: str) -> bool:
    """Accept a bounded absolute HTTP(S) URL with a plausibly public host."""
    if (
        not isinstance(url, str)
        or len(url) > MAX_PUBLIC_URL_CHARS
        or any(character.isspace() for character in url)
    ):
        return False

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        # Accessing port validates malformed and out-of-range explicit ports.
        _ = parsed.port
        if not (
            parsed.scheme.lower() in {"http", "https"}
            and hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return False
        lowered_hostname = hostname.lower().rstrip(".")
        if lowered_hostname == "localhost" or lowered_hostname.endswith(_PRIVATE_HOST_SUFFIXES):
            return False
        try:
            return bool(ip_address(lowered_hostname).is_global)
        except ValueError:
            return True
    except ValueError:
        return False


async def resolves_to_public_network(url: str) -> bool:
    """Return whether every current DNS answer for a safe URL is globally routable."""
    if not is_safe_public_http_url(url):
        return False

    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            ),
            timeout=_DNS_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError, UnicodeError, ValueError):
        return False

    resolved_ips = {entry[4][0] for entry in addresses if entry[4]}
    if not resolved_ips:
        return False
    try:
        return all(ip_address(address).is_global for address in resolved_ips)
    except ValueError:
        return False


__all__ = ["MAX_PUBLIC_URL_CHARS", "is_safe_public_http_url", "resolves_to_public_network"]

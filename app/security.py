from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for the browser worker to open."""


def validate_configured_url(raw_url: str) -> str:
    allow_private = os.getenv("ALLOW_PRIVATE_URLS", "false").lower() == "true"
    allowlist = {item.strip() for item in os.getenv("PRIVATE_URL_ALLOWLIST", "").split(",") if item.strip()}
    return validate_public_url(raw_url, allow_private=allow_private, allowlist=allowlist)


def validate_public_url(raw_url: str, *, allow_private: bool = False, allowlist: set[str] | None = None) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only http:// and https:// URLs are allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise UnsafeUrlError(" URLs with embedded credentials are not allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    allowed_hosts = {item.rstrip(".").lower() for item in (allowlist or set()) if item.strip()}
    host_is_allowlisted = hostname in allowed_hosts or any(hostname.endswith(f".{item}") for item in allowed_hosts if not _is_ip(item))
    if (hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local")) and not (allow_private and host_is_allowlisted):
        raise UnsafeUrlError("Local hostnames are not allowed")

    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"Could not resolve hostname: {hostname}") from exc

    for address in addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ) and not (allow_private and host_is_allowlisted):
            raise UnsafeUrlError("Private or non-public network addresses are not allowed")

    return raw_url


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False

"""SSRF validation and DNS pinning for HTTP operations in K8s Autopilot."""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import threading
from typing import Any, Iterator
from urllib.parse import urlparse

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_dns_pin_lock = threading.Lock()


class _UrlValidationError(ValueError):
    """Raised for scheme, DNS, or SSRF-blocked URL validation errors."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if ip is in private, loopback, link-local, or non-global ranges."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_url(url: str) -> list[str]:
    """Resolve and validate that the URL targets only safe, public IP addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        msg = f"URL scheme not allowed: {parsed.scheme!r} (must be http or https)"
        raise _UrlValidationError(msg)

    hostname = parsed.hostname
    if not hostname:
        msg = "URL is missing a hostname"
        raise _UrlValidationError(msg)

    try:
        encoded_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        msg = f"Could not encode hostname {hostname!r} as IDNA: {exc}"
        raise _UrlValidationError(msg) from exc

    try:
        infos = socket.getaddrinfo(
            encoded_hostname,
            None,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        msg = f"Could not resolve hostname {hostname!r}: {exc}"
        raise _UrlValidationError(msg) from exc

    validated_ips: list[str] = []
    for info in infos:
        raw_ip = str(info[4][0]).split("%", 1)[0]
        ip = ipaddress.ip_address(raw_ip)
        if _is_blocked_ip(ip):
            logger.warning(
                "SSRF guard blocked URL %r: hostname %r resolves to %s",
                url,
                hostname,
                ip,
            )
            msg = (
                f"URL hostname {hostname!r} resolves to blocked address {ip} "
                "(private, loopback, link-local, reserved, or non-global range)"
            )
            raise _UrlValidationError(msg)
        validated_ips.append(raw_ip)

    if not validated_ips:
        msg = f"Hostname {hostname!r} resolved to no addresses"
        raise _UrlValidationError(msg)

    return validated_ips


@contextlib.contextmanager
def _pinned_dns(hostname: str, allowed_ips: list[str]) -> Iterator[None]:
    """Force outgoing urllib3 connections to use pre-validated IPs to prevent TOCTOU DNS rebinding."""
    from urllib3.util import connection as urllib3_connection

    with _dns_pin_lock:
        original = urllib3_connection.create_connection

        def patched(
            address: tuple[str, int], *args: Any, **kwargs: Any
        ) -> socket.socket:
            host, port = address[0], address[1]
            if host != hostname:
                return original(address, *args, **kwargs)
            last_exc: OSError | None = None
            for ip in allowed_ips:
                try:
                    return original((ip, port), *args, **kwargs)
                except OSError as exc:
                    last_exc = exc
            assert last_exc is not None
            raise last_exc

        urllib3_connection.create_connection = patched
        try:
            yield
        finally:
            urllib3_connection.create_connection = original

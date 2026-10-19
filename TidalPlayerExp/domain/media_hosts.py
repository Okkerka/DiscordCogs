"""One host policy for every outbound media request.

The resolver and the FFmpeg boundary must agree on what a media host is: a
host that can reach the local network, a cloud metadata endpoint, or a
multicast or reserved range is never a media host. Non-canonical IPv4
spellings are rejected as well, because ``inet_aton`` still accepts forms
such as ``2852039166`` (169.254.169.254) that would otherwise hide a
link-local address behind a public-looking host.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Final

PUBLIC: Final = "public"
LOOPBACK: Final = "loopback"
BLOCKED: Final = "blocked"

_LOCAL_SUFFIXES: Final = (".local", ".internal")
_HOSTNAME: Final = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


def _legacy_number(part: str) -> int | None:
    """Parse one component the way ``inet_aton`` does: decimal, hex, or octal."""
    if not part:
        return None
    try:
        if part.lower().startswith("0x"):
            return int(part, 16)
        if part.startswith("0") and len(part) > 1:
            return int(part, 8)
        return int(part, 10)
    except ValueError:
        return None


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse the IPv4 spellings ``inet_aton`` still accepts.

    ``2852039166``, ``0xa9fea9fe`` and ``169.254.169.254.`` are all valid
    addresses for a resolver, so they must be classified, not treated as
    unknown public hostnames.
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers: list[int] = []
    for part in parts:
        value = _legacy_number(part)
        if value is None or value < 0:
            return None
        numbers.append(value)
    if len(numbers) == 1:
        if numbers[0] > 0xFFFFFFFF:
            return None
        octets = (
            numbers[0] >> 24,
            (numbers[0] >> 16) & 0xFF,
            (numbers[0] >> 8) & 0xFF,
            numbers[0] & 0xFF,
        )
    elif len(numbers) == 2:
        if numbers[1] > 0xFFFFFF:
            return None
        octets = (
            numbers[0],
            (numbers[1] >> 16) & 0xFF,
            (numbers[1] >> 8) & 0xFF,
            numbers[1] & 0xFF,
        )
    elif len(numbers) == 3:
        if numbers[2] > 0xFFFF:
            return None
        octets = (numbers[0], numbers[1], (numbers[2] >> 8) & 0xFF, numbers[2] & 0xFF)
    else:
        if any(number > 0xFF for number in numbers):
            return None
        octets = tuple(numbers)  # type: ignore[assignment]
    return ipaddress.IPv4Address(
        (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
    )


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _legacy_ipv4(host)


def classify_media_host(hostname: str) -> str:
    """Classify a hostname as public, loopback, or blocked.

    Loopback is reported separately because a local media source is a
    legitimate pattern for the FFmpeg boundary, while the provider resolver
    must never talk to the local network.
    """
    host = hostname.strip("[]").casefold().rstrip(".")
    if not host:
        return BLOCKED
    if host == "localhost" or host.endswith(".localhost"):
        return LOOPBACK
    if host.endswith(_LOCAL_SUFFIXES):
        return BLOCKED
    address = _ip_literal(host)
    if address is not None:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address.is_loopback:
            return LOOPBACK
        if (
            address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return BLOCKED
        return PUBLIC
    if _HOSTNAME.fullmatch(host) is None:
        return BLOCKED
    return PUBLIC

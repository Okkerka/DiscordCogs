"""Validation for timestamps and Discord message references."""

from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_timestamp(value: str, zone: str) -> int:
    """Parse ISO dates, rejecting ambiguous/nonexistent local wall times."""
    try:
        dt = datetime.fromisoformat(value)
        tz = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Use YYYY-MM-DD HH:MM and an IANA timezone such as Europe/Budapest.") from exc
    if dt.tzinfo is None:
        first, second = dt.replace(tzinfo=tz, fold=0), dt.replace(tzinfo=tz, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("This local time is ambiguous or does not exist. Supply an explicit UTC offset.")
        if first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != dt:
            raise ValueError("This local time does not exist. Supply an explicit UTC offset.")
        dt = first
    return int(dt.timestamp())


def parse_message_link(value: str) -> tuple[int, int, int]:
    """Accept only a full Discord guild message link."""
    match = re.fullmatch(r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?", value.strip())
    if not match:
        raise ValueError("Provide a Discord server message link.")
    return tuple(int(part) for part in match.groups())

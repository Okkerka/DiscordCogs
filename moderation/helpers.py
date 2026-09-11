"""Pure validation and filtering shared by moderation commands."""

import re
from typing import Optional

import discord


def dehoisted_name(name: str) -> Optional[str]:
    """Strip conservative ASCII hoisting characters without changing other scripts."""
    cleaned = name.lstrip(" \t\r\n!#$%&()*+,-./:;<=>?@[\\]^_`{|}~\"'")
    return cleaned if cleaned and cleaned != name else None


def matches_purge(message: discord.Message, mode: str, *, member_id: Optional[int] = None,
                  text: str = "", include_pinned: bool = False) -> bool:
    """Match a validated filter, protecting pins unless explicitly requested."""
    if message.pinned and not include_pinned:
        return False
    if member_id is not None and message.author.id != member_id:
        return False
    if mode == "bots":
        return message.author.bot
    if mode == "humans":
        return not message.author.bot
    if mode == "embeds":
        return bool(message.embeds or message.attachments)
    if mode == "attachments":
        return bool(message.attachments)
    if mode == "links":
        return bool(re.search(r"https?://\S+", message.content, re.IGNORECASE))
    if mode == "contains":
        return bool(text) and text.casefold() in message.content.casefold()
    return mode in {"all", "member"}

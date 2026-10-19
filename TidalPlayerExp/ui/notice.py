"""Short-lived Components V2 confirmations for skip and stop actions."""
from __future__ import annotations

import discord

from .display import clamp_text, escape_display

_NOTICE_TIMEOUT = 120.0


class NoticeView(discord.ui.LayoutView):
    """A one-statement Components V2 card matching the player panel style."""

    def __init__(self, text: str, *, accent: discord.Colour) -> None:
        super().__init__(timeout=_NOTICE_TIMEOUT)
        self.text = clamp_text(text, 4_000)
        container = discord.ui.Container(accent_colour=accent)
        container.add_item(discord.ui.TextDisplay(self.text))
        self.add_item(container)


def skip_notice(actor: str, title: str | None) -> NoticeView:
    """Announce who skipped which track; provider titles stay escaped."""
    who = clamp_text(actor, 64)
    body = (
        f"skipped **{escape_display(title, 512)}**."
        if title
        else "skipped the current song."
    )
    return NoticeView(f"### ⏭ Skipped\n{who} {body}", accent=discord.Colour.blurple())


def stop_notice(actor: str, detail: str = "Queue cleared.") -> NoticeView:
    """Announce who stopped playback and what was released with it."""
    who = clamp_text(actor, 64)
    return NoticeView(
        f"### ⏹ Playback stopped\n{who} stopped the playback. {clamp_text(detail, 500)}",
        accent=discord.Colour.red(),
    )

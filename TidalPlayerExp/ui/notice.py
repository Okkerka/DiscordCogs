"""Short-lived Components V2 confirmations for skip and stop actions."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .display import clamp_text, escape_display, safe_display_url
from .embeds import display_duration, source_link_label

if TYPE_CHECKING:
    from ..domain.models import TrackMeta

_NOTICE_TIMEOUT = 120.0


class NoticeView(discord.ui.LayoutView):
    """A compact Components V2 card matching the queue and player panels."""

    def __init__(
        self,
        text: str,
        *,
        accent: discord.Colour,
        image: str | None = None,
        image_alt: str = "Album art",
    ) -> None:
        super().__init__(timeout=_NOTICE_TIMEOUT)
        self.text = clamp_text(text, 4_000)
        container = discord.ui.Container(accent_colour=accent)
        display = discord.ui.TextDisplay(self.text)
        if image:
            container.add_item(
                discord.ui.Section(
                    display,
                    accessory=discord.ui.Thumbnail(
                        media=image, description=clamp_text(image_alt, 1_024)
                    ),
                )
            )
        else:
            container.add_item(display)
        self.add_item(container)


def skip_notice(actor: str, meta: TrackMeta | None) -> NoticeView:
    """Describe the skipped track with the queue card's own vocabulary."""
    who = clamp_text(actor, 64)
    track = meta or {}
    raw_title = " ".join(str(track.get("title") or "").split())
    title = escape_display(raw_title, 512)
    if not title:
        return NoticeView(
            f"### Skipped\n{who} skipped the current song.",
            accent=discord.Colour.blurple(),
        )
    lines = [
        "### Skipped",
        f"**{title}**",
        escape_display(track.get("artist") or "Unknown artist"),
    ]
    if album := escape_display(track.get("album") or ""):
        lines.append(f"*{album}*")
    if url := safe_display_url(track.get("share_url")):
        lines.append(f"[Open in {escape_display(source_link_label(track), 64)}]({url})")
    lines.append(f"-# Duration: {display_duration(track)} · Skipped by {who}")
    return NoticeView(
        "\n".join(lines),
        accent=discord.Colour.blurple(),
        image=safe_display_url(track.get("image"), 2048),
        image_alt=f"Album art for {clamp_text(raw_title, 200)}",
    )


def stop_notice(actor: str, detail: str = "Queue has been cleared.") -> NoticeView:
    """Say what stopped and what was released, without repeating the heading."""
    who = clamp_text(actor, 64)
    return NoticeView(
        f"### Playback stopped\n{clamp_text(detail, 500)}\n-# Stopped by {who}",
        accent=discord.Colour.red(),
    )

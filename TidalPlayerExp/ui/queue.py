"""A short-lived Components V2 view of the native playback queue."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord

from ..domain.normalization import format_duration
from ..playback.models import PlaybackEntry, PlaybackSnapshot
from .display import clamp_text, escape_display
from .embeds import display_duration

if TYPE_CHECKING:
    from ..tidalplayer import TidalPlayerExp


_PAGE_SIZE = 10
_MENTIONS_NONE = discord.AllowedMentions.none()


def _display(value: object, limit: int) -> str:
    """Escape provider text, then re-clamp after Markdown escaping expands it."""
    return clamp_text(escape_display(value, limit), limit)


def _requester(entry: PlaybackEntry) -> str:
    """Render an ID as plain text so queue panels can never ping requesters."""
    return clamp_text(str(entry.requester_id), 32) if entry.requester_id is not None else "Unknown"


class QueueView(discord.ui.LayoutView):
    """Public, read-only, 120-second queue panel backed by fresh snapshots."""

    def __init__(
        self,
        cog: TidalPlayerExp,
        guild_id: int,
        snapshot: PlaybackSnapshot,
        *,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=120.0)
        self.cog = cog
        self.guild_id = guild_id
        self.snapshot = snapshot
        self.page = max(0, page)
        self.message: discord.Message | None = None
        self._update_lock = asyncio.Lock()
        self._build_layout()

    @staticmethod
    def _page_count(snapshot: PlaybackSnapshot) -> int:
        return max(1, (len(snapshot.queued) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def _clamp_page(self) -> None:
        self.page = min(self.page, self._page_count(self.snapshot) - 1)

    def _status(self) -> str:
        states: list[str] = ["Paused" if self.snapshot.paused else "Active" if self.snapshot.current else "Idle"]
        for field, label in (("volume", "Volume"), ("repeat", "Repeat"), ("halted", "Halted")):
            value = getattr(self.snapshot, field, None)
            if value is not None:
                if isinstance(value, bool):
                    value = "On" if value else "Off"
                states.append(f"{label}: {_display(value, 48)}")
        return " · ".join(states)

    def _remaining(self) -> str:
        seconds = 0.0
        unknown = 0
        for entry in self.snapshot.queued:
            duration = entry.meta["duration"]
            seconds += max(0, duration)
            unknown += duration <= 0
        if self.snapshot.current is not None:
            duration = self.snapshot.current.meta["duration"]
            seconds += max(0, duration - self.snapshot.position)
            unknown += duration <= 0
        suffix = f" + {unknown} unknown-length" if unknown else ""
        if self.snapshot.repeat != "off":
            suffix += " · Repeat enabled"
        return f"Remaining: {format_duration(int(seconds))}{suffix}"

    @staticmethod
    def _track_line(number: int, entry: PlaybackEntry) -> str:
        title = _display(entry.meta.get("title") or "Unknown track", 120)
        artist = _display(entry.meta.get("artist") or "Unknown artist", 80)
        duration = _display(display_duration(entry.meta), 24)
        return f"**{number}.** {title} — {artist}\n`{duration}` · Requester: {_requester(entry)}"

    def _panel_text(self) -> str:
        waiting = self.snapshot.queued
        page_count = self._page_count(self.snapshot)
        lines = ["## Queue", f"{len(waiting)} waiting · Page {self.page + 1}/{page_count} · {self._status()}"]
        lines.append(self._remaining())

        if self.snapshot.current is not None:
            current = self.snapshot.current
            title = _display(current.meta.get("title") or "Unknown track", 120)
            artist = _display(current.meta.get("artist") or "Unknown artist", 80)
            duration = _display(display_duration(current.meta), 24)
            lines.extend((
                "", "### Now playing", f"**{title}** — {artist}",
                f"`{duration}` · Requester: {_requester(current)}",
            ))

        if not waiting:
            lines.extend(("", "### Up next", "No songs are waiting. Add one to keep the music going."))
            return "\n".join(lines)

        start = self.page * _PAGE_SIZE
        page_entries = waiting[start:start + _PAGE_SIZE]
        lines.extend(("", "### Up next"))
        lines.extend(self._track_line(start + index + 1, entry) for index, entry in enumerate(page_entries))
        return "\n".join(lines)

    def _build_layout(self) -> None:
        self._clamp_page()
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(clamp_text(self._panel_text(), 4_000)))
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

        page_count = self._page_count(self.snapshot)
        back = discord.ui.Button(
            label="Back", style=discord.ButtonStyle.secondary, disabled=self.page == 0,
            custom_id="tidalplayer:v2:queue:back",
        )
        back.callback = self._back
        next_page = discord.ui.Button(
            label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= page_count - 1,
            custom_id="tidalplayer:v2:queue:next",
        )
        next_page.callback = self._next
        refresh = discord.ui.Button(
            label="Refresh", style=discord.ButtonStyle.primary,
            custom_id="tidalplayer:v2:queue:refresh",
        )
        refresh.callback = self._refresh
        container.add_item(discord.ui.ActionRow(back, next_page, refresh))
        self.add_item(container)

    async def _load_snapshot(self) -> PlaybackSnapshot:
        session = await self.cog.backend.get(self.guild_id)
        if session is None:
            return PlaybackSnapshot(None, (), False, None)
        return session.snapshot()

    async def _update(self, interaction: discord.Interaction, *, page: int | None = None) -> None:
        """Serialize button clicks so each edit reflects one authoritative snapshot."""
        await interaction.response.defer()
        async with self._update_lock:
            if self.is_finished():
                return
            self.snapshot = await self._load_snapshot()
            if page is not None:
                self.page = max(0, page)
            self._build_layout()
            await interaction.edit_original_response(view=self, allowed_mentions=_MENTIONS_NONE)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self._update(interaction, page=self.page - 1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._update(interaction, page=self.page + 1)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await self._update(interaction)

    async def on_timeout(self) -> None:
        """Leave an inert panel behind, then release this short-lived view."""
        async with self._update_lock:
            for child in self.walk_children():
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self, allowed_mentions=_MENTIONS_NONE)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass
            self.message = None
            self.snapshot = PlaybackSnapshot(None, (), False, None)
            self.stop()

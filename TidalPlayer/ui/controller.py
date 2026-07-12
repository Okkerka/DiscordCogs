"""Shared same-voice-channel controller and related-track browser."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence
import discord

if TYPE_CHECKING:
    from ..tidalplayer import TidalPlayer


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class PlayerControllerView(discord.ui.View):
    """Persistent now-playing controller.

    Row 0: [Autoplay]  [Pause]  [Stop]
    Row 1: <Suggested songs select — full width>
    """

    def __init__(
        self,
        cog: "TidalPlayer",
        recommendations: Sequence[Any] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self._recommendations: list[Any] = list(recommendations) if recommendations else []
        self._build_suggestions_select()

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _build_suggestions_select(self) -> None:
        """Append the Suggested-songs Select to row 1."""
        tracks = self._recommendations[:25]  # Discord maximum
        if not tracks:
            select: discord.ui.Select[PlayerControllerView] = discord.ui.Select(
                placeholder="No suggestions available",
                options=[discord.SelectOption(label="\u2014", value="__none__")],
                disabled=True,
                row=1,
            )
            select.callback = self._on_suggestion_select
            self.add_item(select)
            return

        options: list[discord.SelectOption] = []
        for idx, track in enumerate(tracks):
            title = str(
                getattr(track, "full_name", None) or getattr(track, "name", "Unknown")
            )
            artist_obj = getattr(track, "artist", None)
            artist = str(getattr(artist_obj, "name", "Unknown") if artist_obj else "Unknown")
            album_obj = getattr(track, "album", None)
            album = str(getattr(album_obj, "name", "") if album_obj else "")

            description = f"{artist} \u2014 {album}" if album else artist
            options.append(
                discord.SelectOption(
                    label=_truncate(title, 100),
                    description=_truncate(description, 100),
                    value=str(idx),
                )
            )

        select = discord.ui.Select(
            placeholder="Suggested songs",
            options=options,
            row=1,
        )
        select.callback = self._on_suggestion_select
        self.add_item(select)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.can_control_player(interaction):
            return True
        await interaction.response.send_message(
            "Join the bot\u2019s voice channel to use playback controls.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Autoplay",
        style=discord.ButtonStyle.secondary,
        custom_id="tidalplayer:autoplay",
        row=0,
    )
    async def autoplay(
        self, interaction: discord.Interaction, _: discord.ui.Button["PlayerControllerView"]
    ) -> None:
        await self.cog.controller_toggle_autoplay(interaction)

    @discord.ui.button(
        label="Pause",
        style=discord.ButtonStyle.primary,
        custom_id="tidalplayer:pause",
        row=0,
    )
    async def pause(
        self, interaction: discord.Interaction, _: discord.ui.Button["PlayerControllerView"]
    ) -> None:
        await self.cog.controller_toggle_pause(interaction)

    @discord.ui.button(
        label="Stop",
        style=discord.ButtonStyle.danger,
        custom_id="tidalplayer:stop",
        row=0,
    )
    async def stop(
        self, interaction: discord.Interaction, _: discord.ui.Button["PlayerControllerView"]
    ) -> None:
        await self.cog.controller_stop(interaction)

    async def _on_suggestion_select(self, interaction: discord.Interaction) -> None:
        select = next(
            (item for item in self.children if isinstance(item, discord.ui.Select)),
            None,
        )
        if select is None or not select.values:
            await interaction.response.send_message(
                "Could not read selection.", ephemeral=True
            )
            return

        value = select.values[0]
        if value == "__none__":
            # Disabled placeholder sentinel — should not be reachable but guard anyway.
            await interaction.response.defer()
            return

        idx = int(value)
        if idx >= len(self._recommendations):
            await interaction.response.send_message(
                "That recommendation is no longer available.", ephemeral=True
            )
            return

        track = self._recommendations[idx]
        queued = await self.cog.queue_recommendation(interaction, track)
        message = "Added to the queue." if queued else "That recommendation could not be queued."
        await interaction.response.send_message(message, ephemeral=True)

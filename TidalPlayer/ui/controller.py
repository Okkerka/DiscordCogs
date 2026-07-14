"""Same-voice-channel player controls and Tidal Track Radio suggestions."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence
import discord

if TYPE_CHECKING:
    from ..tidalplayer import TidalPlayer


def _short(value: str, limit: int = 100) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


class PlayerControllerView(discord.ui.View):
    def __init__(self, cog: "TidalPlayer", recommendations: Sequence[Any] = ()) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.recommendations = list(recommendations)[:25]
        self._add_suggestions()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.can_control_player(interaction):
            return True
        await interaction.response.send_message("Join the bot's voice channel to use playback controls.", ephemeral=True)
        return False

    @discord.ui.button(label="Autoplay", style=discord.ButtonStyle.secondary, custom_id="tidalplayer:autoplay", row=0)
    async def autoplay(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.controller_toggle_autoplay(interaction)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, custom_id="tidalplayer:pause", row=0)
    async def pause(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.controller_toggle_pause(interaction)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="tidalplayer:stop", row=0)
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.controller_stop(interaction)

    def _add_suggestions(self) -> None:
        if not self.recommendations:
            select = discord.ui.Select(placeholder="No suggestions available", options=[discord.SelectOption(label="No suggestions available", value="none")], disabled=True, row=1, custom_id="tidalplayer:suggestions")
        else:
            options = []
            for index, track in enumerate(self.recommendations):
                title = str(getattr(track, "full_name", None) or getattr(track, "name", "Unknown"))
                artist = str(getattr(getattr(track, "artist", None), "name", "Unknown"))
                options.append(discord.SelectOption(label=_short(title), description=_short(artist), value=str(index)))
            select = discord.ui.Select(placeholder="Suggested songs", options=options, row=1, custom_id="tidalplayer:suggestions")
        select.callback = self._choose_suggestion
        self.add_item(select)

    async def _choose_suggestion(self, interaction: discord.Interaction) -> None:
        select = next(item for item in self.children if isinstance(item, discord.ui.Select))
        if not select.values or select.values[0] == "none":
            await interaction.response.defer()
            return
        index = int(select.values[0])
        if index >= len(self.recommendations):
            await interaction.response.send_message(
                "These suggestions expired. Play a track again to refresh them.",
                ephemeral=True,
            )
            return
        track = self.recommendations[index]
        queued = await self.cog.queue_recommendation(interaction, track)
        await interaction.response.send_message("Added to the queue." if queued else "Could not queue that suggestion.", ephemeral=True)

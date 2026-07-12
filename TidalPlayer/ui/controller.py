"""Shared same-voice-channel controller and related-track browser."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
import discord

if TYPE_CHECKING:
    from ..tidalplayer import TidalPlayer


class SimilarSongsView(discord.ui.View):
    def __init__(self, cog: "TidalPlayer", author_id: int, tracks: list[Any], page: int = 0) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.author_id = author_id
        self.tracks = tracks
        self.page = page
        self.page_size = 5
        self._add_picker()
        self.previous.disabled = page == 0
        self.next.disabled = (page + 1) * self.page_size >= len(tracks)

    def _add_picker(self) -> None:
        start = self.page * self.page_size
        options: list[discord.SelectOption] = []
        for offset, track in enumerate(self.tracks[start:start + self.page_size]):
            title = str(getattr(track, "full_name", None) or getattr(track, "name", "Unknown"))[:100]
            artist = str(getattr(getattr(track, "artist", None), "name", "Unknown"))[:100]
            options.append(discord.SelectOption(label=title, description=artist, value=str(start + offset)))
        picker = discord.ui.Select(placeholder="Add a similar song to the queueâ€¦", options=options, row=0)
        picker.callback = self._queue_selected
        self.add_item(picker)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("This recommendation panel belongs to the member who opened it.", ephemeral=True)
        return False

    async def _queue_selected(self, interaction: discord.Interaction) -> None:
        picker = next(item for item in self.children if isinstance(item, discord.ui.Select))
        track = self.tracks[int(picker.values[0])]
        queued = await self.cog.queue_recommendation(interaction, track)
        message = "Added to the queue." if queued else "That recommendation could not be queued."
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=self.cog.similar_songs_embed(self.tracks, self.page - 1), view=SimilarSongsView(self.cog, self.author_id, self.tracks, self.page - 1))

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=self.cog.similar_songs_embed(self.tracks, self.page + 1), view=SimilarSongsView(self.cog, self.author_id, self.tracks, self.page + 1))


class PlayerControllerView(discord.ui.View):
    def __init__(self, cog: "TidalPlayer") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.can_control_player(interaction):
            return True
        await interaction.response.send_message("Join the bot's voice channel to use playback controls.", ephemeral=True)
        return False

    @discord.ui.button(label="Autoplay: Off", style=discord.ButtonStyle.secondary, custom_id="tidalplayer:autoplay", row=0)
    async def autoplay(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.controller_toggle_autoplay(interaction)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, custom_id="tidalplayer:pause", row=0)
    async def pause(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.controller_toggle_pause(interaction)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.danger, custom_id="tidalplayer:stop", row=0)
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.controller_stop(interaction)

    @discord.ui.button(label="Similar songs", style=discord.ButtonStyle.secondary, custom_id="tidalplayer:similar", row=1)
    async def similar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.open_similar_songs(interaction)
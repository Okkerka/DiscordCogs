"""Shared same-voice-channel playback controls."""
from __future__ import annotations

from typing import TYPE_CHECKING
import discord

if TYPE_CHECKING:
    from ..tidalplayer import TidalPlayer


class PlayerControllerView(discord.ui.View):
    def __init__(self, cog: "TidalPlayer") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.can_control_player(interaction):
            return True
        await interaction.response.send_message("Join the bot's voice channel to use playback controls.", ephemeral=True)
        return False

    @discord.ui.button(label="Autoplay: Off", style=discord.ButtonStyle.secondary, custom_id="tidalplayer:autoplay")
    async def autoplay(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.controller_toggle_autoplay(interaction)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, custom_id="tidalplayer:pause")
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.controller_toggle_pause(interaction)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="tidalplayer:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.controller_stop(interaction)
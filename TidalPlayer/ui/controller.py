"""Components V2 now-playing controller for TidalPlayer."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

import discord

if TYPE_CHECKING:
    from ..domain.models import TrackMeta
    from ..tidalplayer import TidalPlayer


def _short(value: str, limit: int = 100) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _duration(seconds: int) -> str:
    minutes, seconds = divmod(max(0, int(seconds or 0)), 60)
    return f"{minutes:02}:{seconds:02}"


class PlayerControllerView(discord.ui.LayoutView):
    """Persistent Components V2 now-playing panel."""

    def __init__(
        self,
        cog: "TidalPlayer",
        meta: "TrackMeta | None" = None,
        recommendations: Sequence[Any] = (),
        autoplay_enabled: bool = False,
        paused: bool = False,
    ) -> None:
        super().__init__(timeout=None)

        self.cog = cog
        self.meta = meta or {}
        self.recommendations = list(recommendations)[:25]
        self.autoplay_enabled = autoplay_enabled
        self.paused = paused

        self._build_layout()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.can_control_player(interaction):
            return True

        await interaction.response.send_message(
            "Join the bot's voice channel to use playback controls.",
            ephemeral=True,
        )
        return False

    def _build_layout(self) -> None:
        title = str(self.meta.get("title") or "Unknown track")
        artist = str(self.meta.get("artist") or "Unknown artist")
        album = str(self.meta.get("album") or "Unknown album")
        quality = str(self.meta.get("quality") or "LOSSLESS").replace("_", " ")
        duration = _duration(int(self.meta.get("duration") or 0))
        image_url = self.meta.get("image")
        tidal_url = self.meta.get("share_url")

        autoplay_state = "On" if self.autoplay_enabled else "Off"

        info = (
            "## Playing from Tidal\n"
            f"### {title}\n"
            f"**{artist}**\n"
            f"*{album}*\n\n"
            f"**Quality:** {quality}\n"
            f"**Autoplay:** {autoplay_state}\n"
            f"**Duration:** {_duration(int(self.meta.get('duration') or 0))}"
        )

        if tidal_url:
            info += f"\n[Open in TIDAL]({tidal_url})"

        container = discord.ui.Container(
            accent_colour=discord.Colour.blue(),
        )

        # Track details on the left, album artwork on the right.
        if image_url:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(info),
                    accessory=discord.ui.Thumbnail(
                        media=image_url,
                        description=f"Album art for {title}",
                    ),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(info))

        # Divider before Suggested songs.
        container.add_item(
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.small,
            )
        )

        container.add_item(discord.ui.TextDisplay("### Suggested songs"))

        # Dropdown is inside the Container, below its label.
        container.add_item(
            discord.ui.ActionRow(self._make_suggestions_select())
        )

        # Divider before player controls.
        container.add_item(
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.small,
            )
        )

        autoplay_button = discord.ui.Button(
            label=f"Autoplay: {autoplay_state}",
            style=(
                discord.ButtonStyle.success
                if self.autoplay_enabled
                else discord.ButtonStyle.secondary
            ),
            custom_id="tidalplayer:v2:autoplay",
        )
        autoplay_button.callback = self._toggle_autoplay

        pause_button = discord.ui.Button(
            label="Resume" if self.paused else "Pause",
            style=discord.ButtonStyle.primary,
            custom_id="tidalplayer:v2:pause",
        )
        pause_button.callback = self._toggle_pause

        stop_button = discord.ui.Button(
            label="Stop",
            style=discord.ButtonStyle.danger,
            custom_id="tidalplayer:v2:stop",
        )
        stop_button.callback = self._stop

        # Buttons at the bottom of the same container.
        container.add_item(
            discord.ui.ActionRow(
                autoplay_button,
                pause_button,
                stop_button,
            )
        )

        self.add_item(container)

    def _make_suggestions_select(self) -> discord.ui.Select:
        if not self.recommendations:
            select = discord.ui.Select(
                placeholder="No Track Radio suggestions available",
                options=[
                    discord.SelectOption(
                        label="No suggestions available",
                        value="none",
                    )
                ],
                disabled=True,
                custom_id="tidalplayer:v2:suggestions",
            )
        else:
            options = []

            for index, track in enumerate(self.recommendations):
                title = str(
                    getattr(track, "full_name", None)
                    or getattr(track, "name", "Unknown")
                )
                artist = str(
                    getattr(getattr(track, "artist", None), "name", "Unknown")
                )

                options.append(
                    discord.SelectOption(
                        label=_short(title),
                        description=_short(artist),
                        value=str(index),
                    )
                )

            select = discord.ui.Select(
                placeholder="Choose a suggested song",
                options=options,
                custom_id="tidalplayer:v2:suggestions",
            )

        select.callback = self._choose_suggestion
        return select

    async def _toggle_autoplay(self, interaction: discord.Interaction) -> None:
        await self.cog.controller_toggle_autoplay(interaction)

    async def _toggle_pause(self, interaction: discord.Interaction) -> None:
        await self.cog.controller_toggle_pause(interaction)

    async def _stop(self, interaction: discord.Interaction) -> None:
        await self.cog.controller_stop(interaction)

    async def _choose_suggestion(
        self,
        interaction: discord.Interaction,
    ) -> None:
        select = next(
            item
            for item in self.walk_children()
            if isinstance(item, discord.ui.Select)
            and item.custom_id == "tidalplayer:v2:suggestions"
        )

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

        queued = await self.cog.queue_recommendation(
            interaction,
            self.recommendations[index],
        )

        await interaction.response.send_message(
            "Added to the queue." if queued else "Could not queue that suggestion.",
            ephemeral=True,
        )
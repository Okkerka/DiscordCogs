"""Invoker-bound Components V2 configuration for RandomText."""

import asyncio
import logging
import random

import discord

log = logging.getLogger("red.randomtexts.ui")
CATEGORIES = ("brainrot", "showerthought", "dadjoke", "fact")


class FrequencyModal(discord.ui.Modal, title="Message frequency"):
    minimum = discord.ui.TextInput(label="Minimum eligible messages", max_length=5)
    maximum = discord.ui.TextInput(label="Maximum eligible messages", max_length=5)

    def __init__(self, panel, settings: dict):
        super().__init__(timeout=180)
        self.panel = panel
        self.minimum.default = str(settings["frequency_min"])
        self.maximum.default = str(settings["frequency_max"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.panel.interaction_check(interaction):
            return
        try:
            low, high = int(self.minimum.value), int(self.maximum.value)
            if not 1 <= low <= high <= 10000:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "Use 1–10,000 messages, minimum first.", ephemeral=True
            )
        await self.panel.apply(
            interaction,
            frequency_min=low,
            frequency_max=high,
            target=random.randint(low, high),
            counter=0,
        )


class RandomTextView(discord.ui.LayoutView):
    """A short-lived settings panel with permission checks on every interaction."""

    def __init__(self, cog, author_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.cog, self.author_id, self.guild_id = cog, author_id, guild_id
        self.message = None
        self._edit_lock = asyncio.Lock()
        self._preview_busy = False
        cog._views.add(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            self.is_finished()
            or self.cog._closed
            or interaction.guild_id != self.guild_id
            or interaction.user.id != self.author_id
        ):
            await interaction.response.send_message(
                "This panel belongs to its opener or has expired. Open /randomtext settings.",
                ephemeral=True,
            )
            return False
        if (
            await self.cog.bot.cog_disabled_in_guild(self.cog, interaction.guild)
            or not await self.cog.bot.allowed_by_whitelist_blacklist(interaction.user)
            or not await self.cog.can_manage(interaction.user)
        ):
            await interaction.response.send_message(
                "You no longer have access to these settings.", ephemeral=True
            )
            return False
        return True

    async def build(self) -> None:
        settings = await self.cog.get_settings(self.guild_id)
        self.clear_items()
        channels = (
            ", ".join(f"<#{cid}>" for cid in settings["channels"])
            or "All text channels"
        )
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(
            discord.ui.TextDisplay(
                "## RandomTexts\n"
                f"**{'Enabled' if settings['enabled'] else 'Disabled'}** · "
                f"{settings['counter']}/{settings['target']} eligible messages\n"
                f"**Frequency:** {settings['frequency_min']}–{settings['frequency_max']} messages\n"
                f"**Channels:** {channels}\n"
                "Channel restrictions include their threads. Clear the picker to allow all channels."
            )
        )
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        for label, callback, style in (
            (
                "Disable" if settings["enabled"] else "Enable",
                self.toggle,
                discord.ButtonStyle.success,
            ),
            ("Frequency", self.frequency, discord.ButtonStyle.primary),
            ("Preview", self.preview, discord.ButtonStyle.secondary),
            ("Refresh", self.refresh, discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = callback
            row.add_item(button)
        container.add_item(row)
        categories = discord.ui.Select(
            placeholder="Enabled categories",
            min_values=1,
            max_values=4,
            options=[
                discord.SelectOption(
                    label=c.title(), value=c, default=c in settings["categories"]
                )
                for c in CATEGORIES
            ],
        )

        async def choose_categories(interaction):
            await self.apply(interaction, categories=list(categories.values))

        categories.callback = choose_categories
        container.add_item(discord.ui.ActionRow(categories))
        channels = discord.ui.ChannelSelect(
            placeholder="Automatic posting channels (empty = all)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=25,
            default_values=[discord.Object(id=cid) for cid in settings["channels"]],
        )

        async def choose_channels(interaction):
            await self.apply(
                interaction, channels=[channel.id for channel in channels.values]
            )

        channels.callback = choose_channels
        container.add_item(discord.ui.ActionRow(channels))
        container.add_item(
            discord.ui.TextDisplay(
                "-# Settings are saved immediately. Controls expire after 3 minutes of inactivity."
            )
        )
        self.add_item(container)

    async def apply(self, interaction: discord.Interaction, **changes) -> None:
        await interaction.response.defer()
        async with self._edit_lock:
            try:
                await self.cog.update_settings(self.guild_id, **changes)
            except ValueError as error:
                return await interaction.followup.send(str(error), ephemeral=True)
            await self.build()
            if self.message:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )

    async def toggle(self, interaction: discord.Interaction) -> None:
        settings = await self.cog.get_settings(self.guild_id)
        await self.apply(interaction, enabled=not settings["enabled"])

    async def frequency(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            FrequencyModal(self, await self.cog.get_settings(self.guild_id))
        )

    async def preview(self, interaction: discord.Interaction) -> None:
        if self._preview_busy:
            return await interaction.response.send_message(
                "A preview is already loading.", ephemeral=True
            )
        self._preview_busy = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            settings = await self.cog.get_settings(self.guild_id)
            text = await self.cog.generate_text(settings["categories"])
            await interaction.followup.send(
                embed=discord.Embed(
                    title="RandomTexts preview",
                    description=(
                        text or "The selected sources are unavailable. Try again later."
                    )[:4000],
                    colour=discord.Colour.blurple(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            self._preview_busy = False

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        async with self._edit_lock:
            await self.build()
            await interaction.edit_original_response(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )

    async def on_timeout(self) -> None:
        self.cog._views.discard(self)
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                log.debug("Could not expire a RandomTexts panel")

    async def on_error(self, interaction, error, item) -> None:
        log.error("RandomTexts panel failed: %s", type(error).__name__)
        send = (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )
        await send(
            "Could not complete that change. Reopen the settings panel and try again.",
            ephemeral=True,
        )

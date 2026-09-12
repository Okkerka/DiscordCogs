"""Components V2 trigger editor, creation wizard and guarded confirmations."""

from __future__ import annotations

import asyncio
import logging

import discord

log = logging.getLogger("red.chattriggers.ui")


def button(label, callback, *, style=discord.ButtonStyle.secondary, disabled=False):
    item = discord.ui.Button(label=label, style=style, disabled=disabled)
    item.callback = callback
    return item


def audio_picker(mode: str) -> discord.ui.Select:
    return discord.ui.Select(
        placeholder="Sound behavior when music is busy",
        options=[
            discord.SelectOption(
                label="Skip trigger sound while music is busy",
                value="skip",
                description="Keep music and its queue; still show the visual alert.",
                default=mode == "skip",
            ),
            discord.SelectOption(
                label="Interrupt music",
                value="interrupt",
                description="Stop music and clear its queue before playing the sound.",
                default=mode == "interrupt",
            ),
        ],
    )


class SecureLayout(discord.ui.LayoutView):
    """Check identity, current permissions and cog lifetime for every action."""

    def __init__(self, cog, author_id: int, guild_id: int, *, read_only: bool = False):
        super().__init__(timeout=180)
        self.cog, self.author_id, self.guild_id = cog, author_id, guild_id
        self.read_only = read_only
        self.message = None
        self._edit_lock = asyncio.Lock()
        cog._views.add(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            self.is_finished()
            or self.cog._closed
            or interaction.guild_id != self.guild_id
            or interaction.user.id != self.author_id
        ):
            await interaction.response.send_message(
                "This panel belongs to its opener or has expired. Open /chattrigger settings.",
                ephemeral=True,
            )
            return False
        if (
            await self.cog.bot.cog_disabled_in_guild(self.cog, interaction.guild)
            or not await self.cog.bot.allowed_by_whitelist_blacklist(interaction.user)
            or (
                not self.read_only
                and not await self.cog.can_manage(interaction.user, self.guild_id)
            )
        ):
            await interaction.response.send_message(
                "You no longer have access to this panel.", ephemeral=True
            )
            return False
        return True

    def stop(self) -> None:
        self.cog._views.discard(self)
        super().stop()

    async def on_timeout(self) -> None:
        self.cog._views.discard(self)
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                log.debug("Could not expire a ChatTriggers panel")

    async def on_error(self, interaction, error, item) -> None:
        log.error("ChatTriggers panel failed (%s)", type(error).__name__)
        send = (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )
        await send(
            "Could not complete that action. Refresh the panel and try again.",
            ephemeral=True,
        )


class TriggerModal(discord.ui.Modal, title="Configure trigger"):
    phrase = discord.ui.TextInput(label="Trigger phrase", max_length=50)
    sound = discord.ui.TextInput(
        label="Sound URL (optional)", required=False, max_length=2048
    )
    gif = discord.ui.TextInput(
        label="Image / GIF URL (optional)", required=False, max_length=2048
    )
    title_text = discord.ui.TextInput(
        label="Alert title (optional)", required=False, max_length=256
    )
    description = discord.ui.TextInput(
        label="Alert message (optional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(
        self,
        panel: SecureLayout,
        *,
        audio_mode: str = "skip",
        key: str | None = None,
        data: dict | None = None,
    ):
        super().__init__(timeout=180)
        self.panel, self.audio_mode, self.key, self.expected = (
            panel,
            audio_mode,
            key,
            data,
        )
        if data is not None:
            self.phrase.default = data.get("phrase_case", key)
            for field, name in (
                (self.sound, "sound"),
                (self.gif, "gif"),
                (self.title_text, "title"),
                (self.description, "desc"),
            ):
                field.default = data.get(name, "")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Modal submissions do not run their parent view's interaction_check.
        if not await self.panel.interaction_check(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = {
            "sound": self.sound.value,
            "gif": self.gif.value,
            "title": self.title_text.value,
            "desc": self.description.value,
        }
        if self.key is None:
            data["audio_mode"] = self.audio_mode
        try:
            key = await self.panel.cog.save_trigger(
                self.panel.guild_id,
                self.phrase.value,
                data,
                old_key=self.key,
                expected=self.expected,
            )
        except ValueError as error:
            return await interaction.followup.send(str(error), ephemeral=True)
        await interaction.followup.send(
            "Trigger saved. Use its settings panel to adjust channels and cooldown.",
            ephemeral=True,
        )
        if isinstance(self.panel, TriggerPanel):
            async with self.panel._edit_lock:
                self.panel.selected = key
                await self.panel.build()
                if self.panel.message:
                    await self.panel.message.edit(
                        view=self.panel, allowed_mentions=discord.AllowedMentions.none()
                    )
        else:
            self.panel.stop()
            new_panel = TriggerPanel(
                self.panel.cog, self.panel.author_id, self.panel.guild_id
            )
            new_panel.selected = key
            await new_panel.build()
            if self.panel.message:
                new_panel.message = await self.panel.message.edit(
                    view=new_panel, allowed_mentions=discord.AllowedMentions.none()
                )

    async def on_error(self, interaction, error) -> None:
        await self.panel.on_error(interaction, error, None)


class CreateTriggerView(SecureLayout):
    """Choose the music policy before opening the trigger content form."""

    def __init__(self, cog, author_id: int, guild_id: int):
        super().__init__(cog, author_id, guild_id)
        self.audio_mode = "skip"
        container = discord.ui.Container(accent_colour=discord.Colour.red())
        container.add_item(
            discord.ui.TextDisplay(
                "## Create a trigger\nChoose what its sound should do when music is playing or queued.\n"
                "**Skip sound** leaves music alone and still posts the visual alert.\n"
                "**Interrupt music** stops the current music and clears its queue.\n"
                "New triggers start with a 30-second cooldown. You can change these options later."
            )
        )
        select = audio_picker("skip")

        async def choose(interaction):
            self.audio_mode = select.values[0]
            for option in select.options:
                option.default = option.value == self.audio_mode
            await interaction.response.edit_message(view=self)

        select.callback = choose
        container.add_item(discord.ui.ActionRow(select))
        container.add_item(
            discord.ui.ActionRow(
                button(
                    "Continue to trigger details",
                    self.create,
                    style=discord.ButtonStyle.success,
                )
            )
        )
        self.add_item(container)

    async def create(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            TriggerModal(self, audio_mode=self.audio_mode)
        )


class CooldownModal(discord.ui.Modal, title="Trigger cooldown"):
    seconds = discord.ui.TextInput(
        label="Seconds between alerts (0–3600)", max_length=4
    )

    def __init__(self, panel, key: str, current: int):
        super().__init__(timeout=180)
        self.panel, self.key = panel, key
        self.seconds.default = str(current)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.panel.interaction_check(interaction):
            return
        try:
            seconds = int(self.seconds.value)
        except ValueError:
            return await interaction.response.send_message(
                "Enter a whole number from 0 to 3600.", ephemeral=True
            )
        await self.panel.change(interaction, self.key, cooldown=seconds)

    async def on_error(self, interaction, error) -> None:
        await self.panel.on_error(interaction, error, None)


class DeleteTriggerView(SecureLayout):
    def __init__(self, panel, key: str, data: dict):
        super().__init__(panel.cog, panel.author_id, panel.guild_id)
        self.panel, self.key, self.expected = panel, key, data
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                f"## Delete trigger?\n{discord.utils.escape_markdown(data.get('phrase_case', key))}\nThis removes its content and settings."
            ),
            accent_colour=discord.Colour.red(),
        )
        container.add_item(
            discord.ui.ActionRow(
                button("Delete", self.confirm, style=discord.ButtonStyle.danger),
                button("Cancel", self.cancel),
            )
        )
        self.add_item(container)

    async def confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            await self.cog.delete_trigger(self.guild_id, self.key, self.expected)
        except ValueError as error:
            return await interaction.followup.send(str(error), ephemeral=True)
        await self.finish(interaction, "Trigger deleted.")
        if not self.panel.is_finished() and self.panel.message:
            async with self.panel._edit_lock:
                await self.panel.build()
                await self.panel.message.edit(
                    view=self.panel, allowed_mentions=discord.AllowedMentions.none()
                )

    async def cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.finish(interaction, "Deletion cancelled.")

    async def finish(self, interaction, text: str) -> None:
        self.clear_items()
        self.add_item(discord.ui.TextDisplay(text))
        await interaction.edit_original_response(view=self)
        self.stop()


class TriggerPanel(SecureLayout):
    """Paginated trigger browser with controls bound to the displayed trigger."""

    def __init__(self, cog, author_id: int, guild_id: int, *, read_only: bool = False):
        super().__init__(cog, author_id, guild_id, read_only=read_only)
        self.page = 0
        self.selected: str | None = None

    async def build(self) -> None:
        triggers = (await self.cog.get_settings(self.guild_id))["triggers"]
        keys = sorted(triggers)
        pages = max(1, (len(keys) + 24) // 25)
        self.page = min(max(self.page, 0), pages - 1)
        visible = keys[self.page * 25 : (self.page + 1) * 25]
        if self.selected not in triggers:
            self.selected = None
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Colour.red())
        lines = [
            f"{'●' if triggers[key]['active'] else '○'} {discord.utils.escape_markdown(triggers[key].get('phrase_case', key))}"
            for key in visible
        ]
        container.add_item(
            discord.ui.TextDisplay(
                f"## ChatTriggers\n**{len(keys)} triggers** · Page {self.page + 1}/{pages}\n"
                + (
                    "\n".join(lines)
                    if lines
                    else "No triggers yet. Choose New to create one."
                )
            )
        )
        row = discord.ui.ActionRow(
            button("Previous", self.previous, disabled=self.page == 0),
            button("Next", self.next_page, disabled=self.page == pages - 1),
            button("Refresh", self.refresh),
        )
        if not self.read_only:
            row.add_item(button("New", self.new, style=discord.ButtonStyle.success))
        container.add_item(row)
        if visible and not self.read_only:
            select = discord.ui.Select(
                placeholder="Select a trigger to manage",
                options=[
                    discord.SelectOption(
                        label=triggers[key].get("phrase_case", key)[:100],
                        value=key,
                        default=key == self.selected,
                    )
                    for key in visible
                ],
            )

            async def select_trigger(interaction):
                self.selected = select.values[0]
                await self.refresh(interaction)

            select.callback = select_trigger
            container.add_item(discord.ui.ActionRow(select))
        if self.selected is not None and not self.read_only:
            key, data = self.selected, triggers[self.selected]
            container.add_item(discord.ui.Separator())
            channels = (
                ", ".join(f"<#{cid}>" for cid in data["channels"])
                or "All text channels"
            )
            container.add_item(
                discord.ui.TextDisplay(
                    f"**Selected:** {discord.utils.escape_markdown(data.get('phrase_case', key))}\n"
                    f"**Cooldown:** {data['cooldown']} seconds · **Channels:** {channels}\n"
                    "Test sends a real alert, including sound. Interrupt mode stops music and clears the queue."
                )
            )

            async def edit(interaction):
                latest = (await self.cog.get_settings(self.guild_id))["triggers"].get(
                    key
                )
                if latest is None:
                    return await interaction.response.send_message(
                        "Trigger was deleted. Refresh the panel.", ephemeral=True
                    )
                await interaction.response.send_modal(
                    TriggerModal(self, key=key, data=latest)
                )

            async def toggle(interaction):
                await self.change(interaction, key, active=not data["active"])

            async def cooldown(interaction):
                await interaction.response.send_modal(
                    CooldownModal(self, key, data["cooldown"])
                )

            async def test(interaction):
                await interaction.response.defer(ephemeral=True, thinking=True)
                result = await self.cog.fire(
                    interaction.channel, interaction.user, key, testing=True
                )
                await interaction.followup.send(result, ephemeral=True)

            async def delete(interaction):
                view = DeleteTriggerView(self, key, data)
                await interaction.response.send_message(
                    view=view,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                view.message = await interaction.original_response()

            container.add_item(
                discord.ui.ActionRow(
                    button("Edit", edit, style=discord.ButtonStyle.primary),
                    button("Disable" if data["active"] else "Enable", toggle),
                    button("Cooldown", cooldown),
                    button("Test", test),
                    button("Delete", delete, style=discord.ButtonStyle.danger),
                )
            )
            audio = audio_picker(data["audio_mode"])

            async def choose_audio(interaction):
                await self.change(interaction, key, audio_mode=audio.values[0])

            audio.callback = choose_audio
            container.add_item(discord.ui.ActionRow(audio))
            channels = discord.ui.ChannelSelect(
                placeholder="Allowed channels (empty = all)",
                channel_types=[discord.ChannelType.text],
                min_values=0,
                max_values=25,
                default_values=[discord.Object(id=cid) for cid in data["channels"]],
            )

            async def choose_channels(interaction):
                await self.change(
                    interaction,
                    key,
                    channels=[channel.id for channel in channels.values],
                )

            channels.callback = choose_channels
            container.add_item(discord.ui.ActionRow(channels))
        container.add_item(
            discord.ui.TextDisplay(
                "-# Controls expire after 3 minutes of inactivity. Settings survive restarts."
            )
        )
        self.add_item(container)

    async def change(
        self, interaction: discord.Interaction, key: str, **changes
    ) -> None:
        await interaction.response.defer()
        async with self._edit_lock:
            try:
                await self.cog.update_trigger(self.guild_id, key, **changes)
            except ValueError as error:
                return await interaction.followup.send(str(error), ephemeral=True)
            await self.build()
            if self.message:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        async with self._edit_lock:
            await self.build()
            await interaction.edit_original_response(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )

    async def previous(self, interaction: discord.Interaction) -> None:
        self.page -= 1
        await self.refresh(interaction)

    async def next_page(self, interaction: discord.Interaction) -> None:
        self.page += 1
        await self.refresh(interaction)

    async def new(self, interaction: discord.Interaction) -> None:
        view = CreateTriggerView(self.cog, self.author_id, self.guild_id)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

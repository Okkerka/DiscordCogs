"""
ChatTriggers v5.6 - Security Patch
"""

import asyncio
import logging

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

try:
    import lavalink

    LAVALINK_AVAILABLE = True
except ImportError:
    LAVALINK_AVAILABLE = False

log = logging.getLogger("red.chattriggers")

EDIT_MODE = "edit"
TOGGLE_MODE = "toggle"
DELETE_MODE = "delete"


class TriggerModal(discord.ui.Modal, title="Configure Trigger"):
    phrase = discord.ui.TextInput(
        label="Trigger Phrase",
        placeholder="e.g. !Containment Breach!",
        required=True,
        max_length=50,
        custom_id="phrase",
    )
    sound = discord.ui.TextInput(
        label="Sound URL (Optional)",
        placeholder="Use a Youtube link",
        required=False,
        custom_id="sound",
    )
    gif = discord.ui.TextInput(
        label="GIF URL (Optional)",
        placeholder="Use a discord attachment link",
        required=False,
        custom_id="gif",
    )
    embed_title = discord.ui.TextInput(
        label="Embed Title (Optional)",
        placeholder="e.g. 🚨 ALERT TRIGGERED 🚨",
        default="",
        required=False,
        custom_id="title",
    )
    embed_desc = discord.ui.TextInput(
        label="Embed Message (Optional)",
        placeholder="e.g. CONTAINMENT BREACH DETECTED",
        default="",
        style=discord.TextStyle.paragraph,
        required=False,
        custom_id="desc",
    )

    def __init__(self, cog, view_message=None, trigger_name=None, defaults=None):
        super().__init__()
        self.cog = cog
        self.view_message = view_message
        self.trigger_name = trigger_name
        if trigger_name:
            self.phrase.default = trigger_name
        if defaults:
            self.sound.default = defaults.get("sound", "")
            self.gif.default = defaults.get("gif", "")
            self.embed_title.default = defaults.get("title", "")
            self.embed_desc.default = defaults.get("desc", "")

    async def on_submit(self, interaction: discord.Interaction):
        sound_val, gif_val, title_val, desc_val = (
            self.sound.value.strip(),
            self.gif.value.strip(),
            self.embed_title.value.strip(),
            self.embed_desc.value.strip(),
        )
        if not any([sound_val, gif_val, title_val, desc_val]):
            await interaction.response.send_message(
                "❌ Invalid Trigger: Must have at least a Sound, GIF, or Text.",
                ephemeral=True,
            )
            if self.view_message:
                await self._try_delete(self.view_message)
            return

        phrase_key = self.phrase.value.lower().strip()
        if self.trigger_name is None:
            triggers = await self.cog.config.guild(interaction.guild).triggers()
            if phrase_key in triggers:
                await interaction.response.send_message(
                    "❌ A trigger with this phrase already exists.", ephemeral=True
                )
                if self.view_message:
                    await self._try_delete(self.view_message)
                return

        new_data = {
            "phrase_case": self.phrase.value.strip(),
            "sound": sound_val,
            "gif": gif_val,
            "title": title_val,
            "desc": desc_val,
            "active": True,
        }
        async with self.cog.config.guild(interaction.guild).triggers() as triggers:
            if (
                self.trigger_name
                and self.trigger_name.lower() != phrase_key
                and self.trigger_name.lower() in triggers
            ):
                del triggers[self.trigger_name.lower()]
            if phrase_key in triggers:
                new_data["active"] = triggers[phrase_key].get("active", True)
            triggers[phrase_key] = new_data

        await interaction.response.send_message(
            f"✅ Trigger `{self.phrase.value}` saved!", ephemeral=True
        )
        if self.view_message:
            await self._try_delete(self.view_message)

    async def _try_delete(self, message):
        try:
            await message.delete()
        except:
            pass


class TriggerSelect(discord.ui.Select):
    def __init__(self, triggers, origin_message=None, mode=EDIT_MODE):
        self.mode, self.origin_message = mode, origin_message
        options = []
        for t, data in list(triggers.items())[:25]:
            active_status = "✅" if data.get("active", True) else "❌"
            label = f"{active_status} {data['phrase_case']}"
            if mode == DELETE_MODE:
                label = f"🗑️ {data['phrase_case']}"
            elif mode == TOGGLE_MODE:
                label = f"{'Disable' if data.get('active', True) else 'Enable'} {data['phrase_case']}"
            options.append(discord.SelectOption(label=label, value=t))

        placeholders = {
            EDIT_MODE: "Select to EDIT...",
            DELETE_MODE: "Select to DELETE...",
            TOGGLE_MODE: "Select to TOGGLE...",
        }
        super().__init__(
            placeholder=placeholders.get(mode, "Select trigger..."), options=options
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        triggers = await self.view.cog.config.guild(interaction.guild).triggers()
        if key not in triggers:
            return await interaction.response.send_message(
                "❌ Not found.", ephemeral=True
            )

        if self.mode == EDIT_MODE:
            data = triggers[key]
            defaults = {
                "sound": data.get("sound", ""),
                "gif": data.get("gif", ""),
                "title": data.get("title", ""),
                "desc": data.get("desc", ""),
            }
            await interaction.response.send_modal(
                TriggerModal(
                    self.view.cog,
                    self.origin_message,
                    triggers[key]["phrase_case"],
                    defaults,
                )
            )
        else:
            async with self.view.cog.config.guild(interaction.guild).triggers() as t:
                if self.mode == DELETE_MODE:
                    del t[key]
                    await interaction.response.send_message(
                        "🗑️ Deleted.", ephemeral=True
                    )
                elif self.mode == TOGGLE_MODE:
                    current = t[key].get("active", True)
                    t[key]["active"] = not current
                    await interaction.response.send_message(
                        f"✅ Trigger {'Disabled' if current else 'Enabled'}.",
                        ephemeral=True,
                    )
            if self.origin_message:
                await self._try_delete(self.origin_message)

    async def _try_delete(self, message):
        try:
            await message.delete()
        except:
            pass


# Base class for all views in this cog to handle security
class SecureView(discord.ui.View):
    def __init__(self, author: discord.User, timeout=60):
        super().__init__(timeout=timeout)
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "You are not authorized to use this menu.", ephemeral=True
            )
            return False
        return True


class MainView(SecureView):
    def __init__(self, cog, triggers, author, message=None):
        super().__init__(author=author, timeout=60)
        self.cog, self.triggers, self.message = cog, triggers, message

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except:
                pass

    @discord.ui.button(label="New", style=discord.ButtonStyle.success, emoji="➕")
    async def new_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(TriggerModal(self.cog, self.message))

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def edit_btn(self, i: discord.Interaction, button: discord.ui.Button):
        if not self.triggers:
            return await i.response.send_message("No triggers.", ephemeral=True)
        view = SecureView(self.author)
        view.add_item(TriggerSelect(self.triggers, self.message, EDIT_MODE))
        await i.response.send_message(
            "Select a trigger to edit:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.secondary, emoji="🔘")
    async def toggle_btn(self, i: discord.Interaction, button: discord.ui.Button):
        if not self.triggers:
            return await i.response.send_message("No triggers.", ephemeral=True)
        view = SecureView(self.author)
        view.add_item(TriggerSelect(self.triggers, self.message, TOGGLE_MODE))
        await i.response.send_message(
            "Select a trigger to toggle:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def del_btn(self, i: discord.Interaction, button: discord.ui.Button):
        if not self.triggers:
            return await i.response.send_message("No triggers.", ephemeral=True)
        view = SecureView(self.author)
        view.add_item(TriggerSelect(self.triggers, self.message, DELETE_MODE))
        await i.response.send_message(
            "Select a trigger to delete:", view=view, ephemeral=True
        )


class ChatTriggers(commands.Cog):
    """Multi-Trigger Alert System."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=999888777, force_registration=True
        )
        self.config.register_guild(triggers={}, allowed_users=[], admin_users=[])

    async def play_trigger(self, channel, user, data):
        sound_url = data.get("sound")
        if sound_url and LAVALINK_AVAILABLE:
            try:
                guild = channel.guild
                target_vc = user.voice.channel or (
                    guild.voice_client and guild.voice_client.channel
                )
                if target_vc:
                    player = lavalink.get_player(guild.id)
                    if not player:
                        player = await lavalink.connect(target_vc)
                    if player.is_playing:
                        await player.stop()
                    if player.channel.id != target_vc.id:
                        await player.move_to(target_vc)
                    results = await player.load_tracks(sound_url)
                    if results.tracks:
                        player.add(user, results.tracks[0])
                        await player.play()
            except Exception as e:
                log.error(f"ChatTrigger Audio Error: {e}")

        title, desc, gif = (
            data.get("title", ""),
            data.get("desc", ""),
            data.get("gif", ""),
        )
        if not any([title, desc, gif]):
            return

        try:
            embed = discord.Embed(
                title=title or None, description=desc or None, color=discord.Color.red()
            )
            embed.set_footer(
                text=f"Triggered by: {user.display_name}",
                icon_url=user.display_avatar.url,
            )
            if gif:
                embed.set_image(url=gif)
            await channel.send(embed=embed)
        except Exception as e:
            log.error(f"ChatTrigger Visual Error: {e}")

    @commands.group(name="chattrigger", aliases=["alert"], invoke_without_command=True)
    async def chattrigger(self, ctx):
        """Manage ChatTriggers."""
        try:
            await ctx.message.delete()
        except:
            pass

        is_admin = await self.is_admin_or_manager(ctx)
        if not is_admin:
            return await ctx.send("⛔ Denied.", delete_after=5)

        triggers = await self.config.guild(ctx.guild).triggers()
        active = sum(1 for t in triggers.values() if t.get("active", True))
        desc = f"**Total:** {len(triggers)} | **Active:** {active} | **Disabled:** {len(triggers) - active}"
        embed = discord.Embed(
            title="   ChatTriggers Config   ",
            description=desc,
            color=discord.Color.red(),
        )

        view = MainView(self, triggers, author=ctx.author)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

    async def is_admin_or_manager(self, ctx_or_author):
        author = (
            ctx_or_author.author
            if isinstance(ctx_or_author, commands.Context)
            else ctx_or_author
        )
        if await self.bot.is_owner(author):
            return True
        if isinstance(author, discord.Member) and author.guild_permissions.manage_guild:
            return True
        admin_users = await self.config.guild(author.guild).admin_users()
        return author.id in admin_users

    @commands.Cog.listener()
    async def on_message(self, message):
        if (
            message.author.bot
            or not message.guild
            or not message.channel.permissions_for(message.guild.me).send_messages
        ):
            return
        guild_config = await self.config.guild(message.guild).all()
        triggers = guild_config.get("triggers")
        if not triggers:
            return

        content = message.content.lower()
        for phrase_key, data in triggers.items():
            if phrase_key in content and data.get("active", True):
                if (
                    await self.is_admin_or_manager(message.author)
                    or message.author.id in guild_config["allowed_users"]
                ):
                    await self.play_trigger(message.channel, message.author, data)
                    break


async def setup(bot):
    await bot.add_cog(ChatTriggers(bot))

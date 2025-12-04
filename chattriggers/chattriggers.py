"""
ChatTriggers v5.2 - Clean & Optimized (Auto-Cleanup)
"""

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
        label="Sound URL",
        placeholder="Use a Youtube link",
        required=True,
        custom_id="sound",
    )
    gif = discord.ui.TextInput(
        label="GIF URL",
        placeholder="Use discord attachment links or direct links",
        required=False,
        custom_id="gif",
    )
    embed_title = discord.ui.TextInput(
        label="Embed Title",
        placeholder="e.g. 🚨 ALERT TRIGGERED 🚨",
        default="",
        required=False,
        custom_id="title",
    )
    embed_desc = discord.ui.TextInput(
        label="Embed Message",
        placeholder="e.g. CONTAINMENT BREACH DETECTED",
        default="",
        style=discord.TextStyle.paragraph,
        required=False,
        custom_id="desc",
    )

    def __init__(self, cog, view_message=None, trigger_name=None, defaults=None):
        super().__init__()
        self.cog = cog
        self.view_message = view_message  # Reference to the menu message
        self.trigger_name = trigger_name

        if trigger_name:
            self.phrase.default = trigger_name

        if defaults:
            self.sound.default = defaults.get("sound", "")
            self.gif.default = defaults.get("gif", "")
            self.embed_title.default = defaults.get("title", "")
            self.embed_desc.default = defaults.get("desc", "")

    async def on_submit(self, interaction: discord.Interaction):
        phrase_key = self.phrase.value.lower().strip()

        new_data = {
            "phrase_case": self.phrase.value.strip(),
            "sound": self.sound.value.strip(),
            "gif": self.gif.value.strip(),
            "title": self.embed_title.value.strip(),
            "desc": self.embed_desc.value.strip(),
            "active": True,
        }

        async with self.cog.config.guild(interaction.guild).triggers() as triggers:
            if self.trigger_name and self.trigger_name.lower() != phrase_key:
                if self.trigger_name.lower() in triggers:
                    del triggers[self.trigger_name.lower()]

            if phrase_key in triggers:
                new_data["active"] = triggers[phrase_key].get("active", True)

            triggers[phrase_key] = new_data

        await interaction.response.send_message(
            f"✅ Trigger `{self.phrase.value}` saved!", ephemeral=True
        )

        # Cleanup: Delete the original menu message if possible
        if self.view_message:
            try:
                await self.view_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass


class TriggerSelect(discord.ui.Select):
    def __init__(self, triggers, origin_message=None, mode=EDIT_MODE):
        self.mode = mode
        self.origin_message = origin_message
        options = []
        keys = sorted(list(triggers.keys()))[:25]
        for t in keys:
            data = triggers[t]
            active_status = "✅" if data.get("active", True) else "❌"
            label = f"{active_status} {data['phrase_case']}"

            if mode == DELETE_MODE:
                label = f"🗑️ {data['phrase_case']}"
            elif mode == TOGGLE_MODE:
                label = f"{'Disable' if data.get('active', True) else 'Enable'} {data['phrase_case']}"

            options.append(discord.SelectOption(label=label, value=t))

        placeholder = "Select trigger..."
        if mode == DELETE_MODE:
            placeholder = "Select to DELETE..."
        elif mode == TOGGLE_MODE:
            placeholder = "Select to TOGGLE..."

        super().__init__(
            placeholder=placeholder, min_values=1, max_values=1, options=options
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
                "sound": data["sound"],
                "gif": data["gif"],
                "title": data.get("title", ""),
                "desc": data.get("desc", ""),
            }
            # Pass origin_message to modal so it can delete it on submit
            modal = TriggerModal(
                self.view.cog,
                view_message=self.origin_message,
                trigger_name=data["phrase_case"],
                defaults=defaults,
            )
            await interaction.response.send_modal(modal)

        elif self.mode == DELETE_MODE:
            async with self.view.cog.config.guild(interaction.guild).triggers() as t:
                del t[key]
            await interaction.response.send_message("🗑️ Deleted.", ephemeral=True)
            if self.origin_message:
                try:
                    await self.origin_message.delete()
                except:
                    pass

        elif self.mode == TOGGLE_MODE:
            async with self.view.cog.config.guild(interaction.guild).triggers() as t:
                current = t[key].get("active", True)
                t[key]["active"] = not current
                state = "Disabled" if current else "Enabled"
            await interaction.response.send_message(
                f"✅ Trigger {state}.", ephemeral=True
            )
            if self.origin_message:
                try:
                    await self.origin_message.delete()
                except:
                    pass


class MainView(discord.ui.View):
    def __init__(self, cog, triggers, message=None):
        super().__init__(timeout=60)  # 60 second timeout
        self.cog = cog
        self.triggers = triggers
        self.message = message

    async def on_timeout(self):
        # Delete message on timeout
        if self.message:
            try:
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

    @discord.ui.button(label="New", style=discord.ButtonStyle.success, emoji="➕")
    async def new_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # Pass self.message to modal for cleanup
        await interaction.response.send_modal(
            TriggerModal(self.cog, view_message=self.message)
        )

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def edit_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.triggers:
            return await interaction.response.send_message(
                "No triggers.", ephemeral=True
            )
        view = discord.ui.View()
        view.cog = self.cog
        view.add_item(
            TriggerSelect(self.triggers, origin_message=self.message, mode=EDIT_MODE)
        )
        await interaction.response.send_message("Edit:", view=view, ephemeral=True)

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.secondary, emoji="🔘")
    async def toggle_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.triggers:
            return await interaction.response.send_message(
                "No triggers.", ephemeral=True
            )
        view = discord.ui.View()
        view.cog = self.cog
        view.add_item(
            TriggerSelect(self.triggers, origin_message=self.message, mode=TOGGLE_MODE)
        )
        await interaction.response.send_message("Toggle:", view=view, ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def del_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.triggers:
            return await interaction.response.send_message(
                "No triggers.", ephemeral=True
            )
        view = discord.ui.View()
        view.cog = self.cog
        view.add_item(
            TriggerSelect(self.triggers, origin_message=self.message, mode=DELETE_MODE)
        )
        await interaction.response.send_message("Delete:", view=view, ephemeral=True)


class ChatTriggers(commands.Cog):
    """Multi-Trigger Alert System."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=999888777, force_registration=True
        )
        self.config.register_guild(triggers={}, allowed_users=[], admin_users=[])

    async def is_admin_or_manager(self, ctx):
        if ctx.author.guild_permissions.manage_guild:
            return True
        if await ctx.bot.is_owner(ctx.author):
            return True
        admins = await self.config.guild(ctx.guild).admin_users()
        return ctx.author.id in admins

    async def play_trigger(self, channel, user, data):
        if not LAVALINK_AVAILABLE:
            return

        guild = channel.guild
        sound_url = data["sound"]

        try:
            target_vc = user.voice.channel if user.voice else None
            if not target_vc and guild.voice_client:
                target_vc = guild.voice_client.channel
            if not target_vc:
                return await channel.send("❌ User not in VC.", delete_after=10)

            try:
                player = lavalink.get_player(guild.id)
                if not player:
                    await lavalink.connect(target_vc)
                    player = lavalink.get_player(guild.id)
                else:
                    if player.is_playing:
                        await player.stop()
                    await player.move_to(target_vc)

                results = await player.load_tracks(sound_url)
                if results.tracks:
                    player.queue.clear()
                    player.add(user, results.tracks[0])
                    await player.play()
            except Exception as e:
                return await channel.send(f"❌ Audio Error: {e}", delete_after=10)

            title = data.get("title", "")
            desc = data.get("desc", "")
            gif = data.get("gif", "")

            if not title and not desc and not gif:
                title = "ALERT"

            embed = discord.Embed(
                title=title if title else None,
                description=desc if desc else None,
                color=discord.Color.red(),
            )
            embed.set_footer(
                text=f"Triggered by: {user.display_name}",
                icon_url=user.display_avatar.url,
            )

            if gif:
                embed.set_image(url=gif)

            await channel.send(embed=embed)

        except Exception as e:
            log.error(f"Trigger failed: {e}")

    @commands.group(name="chattrigger", aliases=["alert"], invoke_without_command=True)
    async def chattrigger(self, ctx):
        """Manage ChatTriggers."""
        if not await self.is_admin_or_manager(ctx):
            return await ctx.send("⛔ Denied.")

        triggers = await self.config.guild(ctx.guild).triggers()

        total = len(triggers)
        active = sum(1 for t in triggers.values() if t.get("active", True))
        disabled = total - active

        desc = f"**Total:** {total} | **Active:** {active} | **Disabled:** {disabled}"

        embed = discord.Embed(
            title="   ChatTriggers Config   ",
            description=desc,
            color=discord.Color.red(),
        )

        # Create view first, then send message, then assign message to view
        view = MainView(self, triggers)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

    @chattrigger.command(name="add_perm")
    async def add_perm(self, ctx, user: discord.User):
        """Allow user to trigger alerts."""
        if not await self.is_admin_or_manager(ctx):
            return
        async with self.config.guild(ctx.guild).allowed_users() as l:
            if user.id not in l:
                l.append(user.id)
        await ctx.tick()

    @chattrigger.command(name="list")
    async def ct_list(self, ctx):
        """List all triggers."""
        triggers = await self.config.guild(ctx.guild).triggers()
        if not triggers:
            return await ctx.send("No triggers.")

        msg = "**Triggers:**\n"
        for t in triggers.values():
            status = "✅" if t.get("active", True) else "❌"
            msg += f"{status} `{t['phrase_case']}`\n"
        await ctx.send(msg)

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
        matched_data = None
        for phrase_key, data in triggers.items():
            if phrase_key in content and data.get("active", True):
                matched_data = data
                break

        if matched_data:
            is_owner = await self.bot.is_owner(message.author)
            is_admin = message.author.id in guild_config["admin_users"]
            is_allowed = message.author.id in guild_config["allowed_users"]

            if is_owner or is_admin or is_allowed:
                await self.play_trigger(message.channel, message.author, matched_data)


async def setup(bot):
    await bot.add_cog(ChatTriggers(bot))

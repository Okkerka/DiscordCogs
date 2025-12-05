"""
ChatTriggers v5.8 - Optimized
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
MENU_TIMEOUT = 10


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
        sound_val = self.sound.value.strip()
        gif_val = self.gif.value.strip()
        title_val = self.embed_title.value.strip()
        desc_val = self.embed_desc.value.strip()

        if not any([sound_val, gif_val, title_val, desc_val]):
            await interaction.response.send_message(
                "❌ Invalid Trigger: You must provide at least a Sound, GIF, or Embed Title/Message.",
                ephemeral=True,
            )
            if self.view_message:
                try:
                    await self.view_message.delete()
                except:
                    pass
            return

        phrase_key = self.phrase.value.lower().strip()

        if self.trigger_name is None:
            triggers = await self.cog.config.guild(interaction.guild).triggers()
            if phrase_key in triggers:
                await interaction.response.send_message(
                    "❌ A trigger with this phrase already exists. Please edit it instead.",
                    ephemeral=True,
                )
                if self.view_message:
                    try:
                        await self.view_message.delete()
                    except:
                        pass
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
            if self.trigger_name and self.trigger_name.lower() != phrase_key:
                if self.trigger_name.lower() in triggers:
                    del triggers[self.trigger_name.lower()]

            if phrase_key in triggers:
                new_data["active"] = triggers[phrase_key].get("active", True)

            triggers[phrase_key] = new_data

        await interaction.response.send_message(
            f"✅ Trigger `{self.phrase.value}` saved!", ephemeral=True
        )

        if self.view_message:
            try:
                await self.view_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass


class TriggerSelect(discord.ui.Select):
    def __init__(self, cog, triggers, origin_message=None, mode=EDIT_MODE):
        self.cog = cog
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
        triggers = await self.cog.config.guild(interaction.guild).triggers()

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
            modal = TriggerModal(
                self.cog,
                view_message=self.origin_message,
                trigger_name=data["phrase_case"],
                defaults=defaults,
            )
            await interaction.response.send_modal(modal)

        elif self.mode == DELETE_MODE:
            async with self.cog.config.guild(interaction.guild).triggers() as t:
                del t[key]
            await interaction.response.send_message("🗑️ Deleted.", ephemeral=True)
            if self.origin_message:
                try:
                    await self.origin_message.delete()
                except:
                    pass

        elif self.mode == TOGGLE_MODE:
            async with self.cog.config.guild(interaction.guild).triggers() as t:
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


class SecureView(discord.ui.View):
    def __init__(self, cog, author: discord.User, timeout=MENU_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ You are not authorized to use this menu.", ephemeral=True
            )
            return False
        return True


class MainView(SecureView):
    def __init__(self, cog, triggers, author, message=None):
        super().__init__(cog=cog, author=author, timeout=MENU_TIMEOUT)
        self.triggers = triggers
        self.message = message

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

    @discord.ui.button(label="New", style=discord.ButtonStyle.success, emoji="➕")
    async def new_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
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
        view = SecureView(cog=self.cog, author=self.author)
        view.add_item(
            TriggerSelect(
                self.cog, self.triggers, origin_message=self.message, mode=EDIT_MODE
            )
        )
        await interaction.response.send_message(
            "Select a trigger to edit:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.secondary, emoji="🔘")
    async def toggle_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.triggers:
            return await interaction.response.send_message(
                "No triggers.", ephemeral=True
            )
        view = SecureView(cog=self.cog, author=self.author)
        view.add_item(
            TriggerSelect(
                self.cog, self.triggers, origin_message=self.message, mode=TOGGLE_MODE
            )
        )
        await interaction.response.send_message(
            "Select a trigger to toggle:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def del_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.triggers:
            return await interaction.response.send_message(
                "No triggers.", ephemeral=True
            )
        view = SecureView(cog=self.cog, author=self.author)
        view.add_item(
            TriggerSelect(
                self.cog, self.triggers, origin_message=self.message, mode=DELETE_MODE
            )
        )
        await interaction.response.send_message(
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

    async def is_admin_or_manager(self, ctx):
        if ctx.author.guild_permissions.manage_guild:
            return True
        if await ctx.bot.is_owner(ctx.author):
            return True
        admins = await self.config.guild(ctx.guild).admin_users()
        return ctx.author.id in admins

    async def play_trigger(self, channel, user, data):
        sound_url = data.get("sound")

        if sound_url and LAVALINK_AVAILABLE:
            try:
                guild = channel.guild
                target_vc = user.voice.channel if user.voice else None
                if not target_vc and guild.voice_client:
                    target_vc = guild.voice_client.channel
                if not target_vc:
                    log.warning(f"ChatTrigger: User {user.name} not in VC.")
                else:
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
                log.error(f"ChatTrigger Audio Error: {e}")

        try:
            title = data.get("title", "")
            desc = data.get("desc", "")
            gif = data.get("gif", "")

            if not title and not desc and not gif:
                return

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
            log.error(f"ChatTrigger Visual Error: {e}")

    @commands.group(name="chattrigger", aliases=["alert"], invoke_without_command=True)
    async def chattrigger(self, ctx):
        """Manage ChatTriggers."""
        try:
            await ctx.message.delete()
        except:
            pass

        if not await self.is_admin_or_manager(ctx):
            return await ctx.send("⛔ Denied.", delete_after=5)

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

        view = MainView(cog=self, triggers=triggers, author=ctx.author)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

    @chattrigger.command(name="add_perm")
    async def add_perm(self, ctx, user: discord.User):
        """Allow user to trigger alerts."""
        try:
            await ctx.message.delete()
        except:
            pass

        if not await self.is_admin_or_manager(ctx):
            return
        async with self.config.guild(ctx.guild).allowed_users() as l:
            if user.id not in l:
                l.append(user.id)
        await ctx.tick()

    @chattrigger.command(name="list")
    async def ct_list(self, ctx):
        """List all triggers."""
        try:
            await ctx.message.delete()
        except:
            pass

        triggers = await self.config.guild(ctx.guild).triggers()
        if not triggers:
            return await ctx.send("No triggers.", delete_after=10)

        msg = "**Triggers:**\n"
        for t in triggers.values():
            status = "✅" if t.get("active", True) else "❌"
            msg += f"{status} `{t['phrase_case']}`\n"
        await ctx.send(msg, delete_after=30)

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

        # Optimization: Calculate min/max trigger phrase lengths
        active_triggers = {k: v for k, v in triggers.items() if v.get("active", True)}
        if not active_triggers:
            return

        trigger_lengths = [len(phrase) for phrase in active_triggers.keys()]
        min_trigger_len = min(trigger_lengths)
        max_trigger_len = max(trigger_lengths)

        # Skip if message is too short or too long to match any trigger
        content_len = len(content)
        if content_len < min_trigger_len or content_len > max_trigger_len * 10:
            return

        matched_data = None
        for phrase_key, data in active_triggers.items():
            if phrase_key in content:
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

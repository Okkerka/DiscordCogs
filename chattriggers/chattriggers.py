"""
ChatTriggers - The Ultimate Panic Button Cog
"""

import logging

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

# Optional Lavalink import
try:
    import lavalink

    LAVALINK_AVAILABLE = True
except ImportError:
    LAVALINK_AVAILABLE = False

log = logging.getLogger("red.chattriggers")


class ConfigModal(discord.ui.Modal, title="ChatTriggers Configuration"):
    trigger_phrase = discord.ui.TextInput(
        label="Trigger Phrase",
        placeholder="e.g. !Containment Breach!",
        required=True,
        max_length=100,
    )
    gif_url = discord.ui.TextInput(
        label="GIF URL",
        placeholder="https://media.giphy.com/...",
        required=True,
        style=discord.TextStyle.short,
    )
    sound_url = discord.ui.TextInput(
        label="Sound URL / Path",
        placeholder="https://example.com/alarm.mp3 or local path",
        required=True,
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.config.guild(interaction.guild).trigger_phrase.set(
            self.trigger_phrase.value
        )
        await self.cog.config.guild(interaction.guild).gif_url.set(self.gif_url.value)
        await self.cog.config.guild(interaction.guild).sound_url.set(
            self.sound_url.value
        )
        await interaction.response.send_message(
            "✅ Configuration saved!", ephemeral=True
        )


class UserSelectView(discord.ui.View):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Select allowed users...",
        min_values=0,
        max_values=25,
    )
    async def select_users(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ):
        current = await self.cog.config.guild(interaction.guild).allowed_users()
        new_ids = [user.id for user in select.values]
        # Merge lists unique
        updated = list(set(current + new_ids))
        await self.cog.config.guild(interaction.guild).allowed_users.set(updated)
        await interaction.response.send_message(
            f"✅ Added {len(new_ids)} users to allowlist.", ephemeral=True
        )


class MainView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Config Text/Media", style=discord.ButtonStyle.primary, emoji="⚙️"
    )
    async def config_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(ConfigModal(self.cog))

    @discord.ui.button(
        label="Add Users (GUI)", style=discord.ButtonStyle.secondary, emoji="👥"
    )
    async def users_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Select users to ADD:", view=UserSelectView(self.cog), ephemeral=True
        )

    @discord.ui.button(label="TEST ALERT", style=discord.ButtonStyle.danger, emoji="🚨")
    async def test_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.cog.trigger_alert(interaction.channel, interaction.user, manual=True)
        await interaction.response.defer()


class ChatTriggers(commands.Cog):
    """Emergency Alert System with Discord UI."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=999888777, force_registration=True
        )
        self.config.register_guild(
            trigger_phrase="!Containment Breach!",
            gif_url="",
            sound_url="",
            allowed_users=[],
        )

        async def trigger_alert(self, channel, user, manual=False):
            guild = channel.guild
            settings = await self.config.guild(guild).all()

            if not settings["sound_url"]:
                return await channel.send("❌ No sound configured!")

            if not LAVALINK_AVAILABLE:
                return await channel.send(
                    "❌ Lavalink not available (Audio cog needed)."
                )

            try:
                # 1. Find Target Voice Channel
                target_vc = user.voice.channel if user.voice else None
                if not target_vc and guild.voice_client:
                    target_vc = guild.voice_client.channel

                if not target_vc:
                    return await channel.send("❌ You need to be in a Voice Channel!")

                # 2. Hijack Player
                try:
                    player = lavalink.get_player(guild.id)
                    if player:
                        await player.stop()  # KILL THE MUSIC
                        # Force move to user's channel (avoids channel_id attribute error)
                        await player.move_to(target_vc)
                    else:
                        # Connect if no player
                        await lavalink.connect(target_vc)
                        player = lavalink.get_player(guild.id)
                except Exception as e:
                    return await channel.send(f"❌ Audio Connection Error: {e}")

                # 3. Force Play Alarm
                # Load the track directly
                results = await player.load_tracks(settings["sound_url"])
                if results.tracks:
                    track = results.tracks[0]
                    # Clear queue to prevent other songs from playing
                    player.queue.clear()
                    player.add(user, track)
                    await player.play()
                else:
                    await channel.send("❌ Could not load alarm sound URL.")

                # 4. Visual Nuke
                if settings["gif_url"]:
                    embed = discord.Embed(
                        title="🚨 ALERT TRIGGERED 🚨", color=discord.Color.red()
                    )
                    embed.set_image(url=settings["gif_url"])
                    await channel.send(
                        content=f"## 🚨 ALERT TRIGGERED BY {user.mention} 🚨",
                        embed=embed,
                    )

            except Exception as e:
                log.error(f"Alert failed: {e}")
                await channel.send(f"⚠️ Alert malfunction: {e}")

    @commands.group(name="chattrigger", aliases=["alert"])
    @commands.admin_or_permissions(manage_guild=True)
    async def chattrigger(self, ctx):
        """Manage the ChatTriggers system."""
        if ctx.invoked_subcommand is None:
            embed = discord.Embed(
                title="🚨 ChatTriggers Control Panel",
                description="Configure your emergency broadcast system.",
                color=discord.Color.dark_red(),
            )
            await ctx.send(embed=embed, view=MainView(self))

    @chattrigger.command(name="add")
    async def ct_add(self, ctx, user: discord.User):
        """Add a user to the allowed list."""
        async with self.config.guild(ctx.guild).allowed_users() as allowed:
            if user.id not in allowed:
                allowed.append(user.id)
                await ctx.send(f"✅ Added {user.mention} to alert allowlist.")
            else:
                await ctx.send(f"⚠️ {user.mention} is already allowed.")

    @chattrigger.command(name="remove")
    async def ct_remove(self, ctx, user: discord.User):
        """Remove a user from the allowed list."""
        async with self.config.guild(ctx.guild).allowed_users() as allowed:
            if user.id in allowed:
                allowed.remove(user.id)
                await ctx.send(f"✅ Removed {user.mention}.")
            else:
                await ctx.send(f"⚠️ {user.mention} is not in the list.")

    @chattrigger.command(name="list")
    async def ct_list(self, ctx):
        """List allowed users."""
        allowed = await self.config.guild(ctx.guild).allowed_users()
        if not allowed:
            return await ctx.send("No allowed users.")

        mentions = [f"<@{uid}>" for uid in allowed]
        await ctx.send(f"**Allowed Users:**\n" + ", ".join(mentions))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return

        phrase = await self.config.guild(message.guild).trigger_phrase()
        # Simple case-insensitive check
        if phrase and phrase.lower() in message.content.lower():
            # Check Allowlist
            allowed = await self.config.guild(message.guild).allowed_users()
            if message.author.id in allowed:
                await self.trigger_alert(message.channel, message.author)


async def setup(bot):
    await bot.add_cog(ChatTriggers(bot))

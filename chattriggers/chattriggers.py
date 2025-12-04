"""
ChatTriggers v3.0 - Fully Optimized Panic Button
"""

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
import logging

# Optional Lavalink import
try:
    import lavalink
    LAVALINK_AVAILABLE = True
except ImportError:
    LAVALINK_AVAILABLE = False

log = logging.getLogger("red.chattriggers")

class ConfigModal(discord.ui.Modal, title="ChatTriggers Config"):
    trigger_phrase = discord.ui.TextInput(
        label="Trigger Phrase",
        placeholder="e.g. !Containment Breach!",
        required=True,
        max_length=100
    )
    gif_url = discord.ui.TextInput(
        label="GIF URL (Tenor/Direct)",
        placeholder="https://media.tenor.com/...",
        required=False,
        style=discord.TextStyle.short
    )
    sound_url = discord.ui.TextInput(
        label="Sound URL",
        placeholder="https://example.com/alarm.mp3",
        required=True
    )
    embed_title = discord.ui.TextInput(
        label="Embed Title",
        placeholder="Title...",
        default="🚨 ALERT TRIGGERED 🚨",
        required=False,
        max_length=256
    )
    embed_desc = discord.ui.TextInput(
        label="Embed Message",
        placeholder="Description...",
        default="CONTAINMENT BREACH DETECTED",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=1000
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        # Batch update for efficiency
        async with self.cog.config.guild(interaction.guild).all() as settings:
            settings['trigger_phrase'] = self.trigger_phrase.value
            settings['gif_url'] = self.gif_url.value
            settings['sound_url'] = self.sound_url.value
            settings['embed_title'] = self.embed_title.value
            settings['embed_desc'] = self.embed_desc.value

        await interaction.response.send_message("✅ Configuration updated!", ephemeral=True)

class UserSelectView(discord.ui.View):
    def __init__(self, cog, mode="trigger"):
        super().__init__()
        self.cog = cog
        self.mode = mode # "trigger" or "admin"

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select users...", min_values=0, max_values=25)
    async def select_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        guild = interaction.guild
        new_ids = [user.id for user in select.values]

        key = "allowed_users" if self.mode == "trigger" else "admin_users"

        async with getattr(self.cog.config.guild(guild), key)() as current_list:
            # Optimized merge
            updated = list(set(current_list + new_ids))
            current_list.clear()
            current_list.extend(updated)

        await interaction.response.send_message(f"✅ Added {len(new_ids)} users to {self.mode} list.", ephemeral=True)

class MainView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Edit Config", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def config_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ConfigModal(self.cog))

    @discord.ui.button(label="View Settings", style=discord.ButtonStyle.secondary, emoji="📄")
    async def view_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = await self.cog.config.guild(interaction.guild).all()
        desc = (
            f"**Trigger:** `{s['trigger_phrase']}`
"
            f"**Sound:** [Link]({s['sound_url']})
"
            f"**GIF:** [Link]({s['gif_url']}) " if s['gif_url'] else "None"
        )
        desc += f"
**Title:** {s['embed_title']}
**Msg:** {s['embed_desc']}"

        embed = discord.Embed(title="Current Config", description=desc, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Add Users", style=discord.ButtonStyle.secondary, emoji="👥")
    async def users_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Add TRIGGER users:", view=UserSelectView(self.cog, "trigger"), ephemeral=True)

    @discord.ui.button(label="TEST ALERT", style=discord.ButtonStyle.danger, emoji="🚨")
    async def test_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.trigger_alert(interaction.channel, interaction.user, manual=True)
        await interaction.response.defer()

class ChatTriggers(commands.Cog):
    """Emergency Alert System with Granular Permissions."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=999888777, force_registration=True)
        self.config.register_guild(
            trigger_phrase="!Containment Breach!",
            gif_url="",
            sound_url="",
            embed_title="🚨 ALERT TRIGGERED 🚨",
            embed_desc="CONTAINMENT BREACH DETECTED",
            allowed_users=[], # Can trigger
            admin_users=[]    # Can configure
        )

    async def trigger_alert(self, channel, user, manual=False):
        if not LAVALINK_AVAILABLE:
            return await channel.send("❌ Lavalink not available.")

        guild = channel.guild
        settings = await self.config.guild(guild).all()

        if not settings['sound_url']:
            return await channel.send("❌ No sound configured!")

        try:
            # 1. Optimization: Check VC first before touching audio
            target_vc = user.voice.channel if user.voice else None
            if not target_vc and guild.voice_client:
                 target_vc = guild.voice_client.channel

            if not target_vc:
                return await channel.send("❌ You need to be in a Voice Channel!")

            # 2. Audio Hijack (Optimized)
            try:
                player = lavalink.get_player(guild.id)
                if not player:
                    await lavalink.connect(target_vc)
                    player = lavalink.get_player(guild.id)
                else:
                    if player.is_playing:
                        await player.stop()
                    if player.channel_id != target_vc.id: # Might be unreliable on some versions, but harmless
                         await player.move_to(target_vc)
            except Exception as e:
                return await channel.send(f"❌ Audio Error: {e}")

            # 3. Playback
            try:
                results = await player.load_tracks(settings['sound_url'])
                if results.tracks:
                    player.queue.clear()
                    player.add(user, results.tracks[0])
                    await player.play()
            except Exception as e:
                 await channel.send(f"❌ Playback Error: {e}")

            # 4. Visuals (Robust Hybrid Mode)
            embed = discord.Embed(
                title=settings.get("embed_title", "🚨 ALERT"),
                description=settings.get("embed_desc", "ALERT"),
                color=discord.Color.red()
            )

            msg_content = f"## 🚨 ALERT TRIGGERED BY {user.mention} 🚨"
            gif = settings.get('gif_url', "")

            if gif:
                # Tenor/Giphy usually need plain text links to expand
                if any(x in gif.lower() for x in ["tenor.com", "giphy.com"]):
                    msg_content += f"
{gif}"
                else:
                    # Direct files work in embed
                    embed.set_image(url=gif)

            await channel.send(content=msg_content, embed=embed)

        except Exception as e:
            log.error(f"Alert failed: {e}")

    async def is_admin_or_manager(self, ctx):
        # Check 1: Server Permission
        if ctx.author.guild_permissions.manage_guild:
            return True
        # Check 2: Owner
        if await ctx.bot.is_owner(ctx.author):
            return True
        # Check 3: Custom Admin List
        admins = await self.config.guild(ctx.guild).admin_users()
        return ctx.author.id in admins

    @commands.group(name="chattrigger", aliases=["alert"])
    async def chattrigger(self, ctx):
        """Manage the ChatTriggers system."""
        if ctx.invoked_subcommand is None:
            if not await self.is_admin_or_manager(ctx):
                return await ctx.send("⛔ You do not have permission to configure alerts.")

            embed = discord.Embed(
                title="🚨 ChatTriggers Panel",
                description="Configure your emergency broadcast system.",
                color=discord.Color.dark_red()
            )
            await ctx.send(embed=embed, view=MainView(self))

    # --- TRIGGER USER COMMANDS ---
    @chattrigger.command(name="add")
    async def ct_add(self, ctx, user: discord.User):
        """Authorize a user to TRIGGER the alert."""
        if not await self.is_admin_or_manager(ctx): return await ctx.send("⛔ Permission Denied.")

        async with self.config.guild(ctx.guild).allowed_users() as allowed:
            if user.id not in allowed:
                allowed.append(user.id)
                await ctx.send(f"✅ Added {user.mention} to Trigger list.")
            else:
                await ctx.send("⚠️ Already authorized.")

    @chattrigger.command(name="remove")
    async def ct_remove(self, ctx, user: discord.User):
        """Revoke trigger permission."""
        if not await self.is_admin_or_manager(ctx): return await ctx.send("⛔ Permission Denied.")

        async with self.config.guild(ctx.guild).allowed_users() as allowed:
            if user.id in allowed:
                allowed.remove(user.id)
                await ctx.send(f"✅ Removed {user.mention}.")
            else:
                await ctx.send("⚠️ Not found.")

    # --- ADMIN USER COMMANDS ---
    @chattrigger.group(name="permission", aliases=["admin"])
    async def ct_perm(self, ctx):
        """Manage Admin permissions."""
        pass

    @ct_perm.command(name="add")
    async def ct_perm_add(self, ctx, user: discord.User):
        """Authorize a user to CONFIGURE settings."""
        if not await self.is_admin_or_manager(ctx): return await ctx.send("⛔ Permission Denied.")

        async with self.config.guild(ctx.guild).admin_users() as admins:
            if user.id not in admins:
                admins.append(user.id)
                await ctx.send(f"🛡️ Added {user.mention} as ChatTrigger Admin.")
            else:
                await ctx.send("⚠️ Already admin.")

    @ct_perm.command(name="remove")
    async def ct_perm_remove(self, ctx, user: discord.User):
        """Revoke admin permission."""
        if not await self.is_admin_or_manager(ctx): return await ctx.send("⛔ Permission Denied.")

        async with self.config.guild(ctx.guild).admin_users() as admins:
            if user.id in admins:
                admins.remove(user.id)
                await ctx.send(f"🛡️ Removed {user.mention} from Admins.")
            else:
                await ctx.send("⚠️ Not found.")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild: return

        phrase = await self.config.guild(message.guild).trigger_phrase()
        if phrase and phrase.lower() in message.content.lower():
            settings = await self.config.guild(message.guild).all()

            # Logic: Owner OR Admin List OR Trigger List
            is_owner = await self.bot.is_owner(message.author)
            is_admin = message.author.id in settings['admin_users']
            is_allowed = message.author.id in settings['allowed_users']

            if is_owner or is_admin or is_allowed:
                await self.trigger_alert(message.channel, message.author)

async def setup(bot):
    await bot.add_cog(ChatTriggers(bot))

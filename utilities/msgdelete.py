"""MessageDelete - Automatically deletes messages from specific users and provides fun commands."""

from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import box
import discord
import random
import asyncio
import logging
from typing import Optional, Union

# Configure logging
log = logging.getLogger("red.messagedelete")

# Constants
DEFAULT_PING_AMOUNT = 5
MAX_PING_AMOUNT = 20
PING_DELAY = 0.5  # seconds between pings to avoid rate limits
GAY_PERCENTAGE_MIN_NORMAL = 0
GAY_PERCENTAGE_MAX_NORMAL = 100
GAY_PERCENTAGE_MIN_HAWK = 51
GAY_PERCENTAGE_MAX_HAWK = 150


class MessageDelete(commands.Cog):
    """Automatically deletes messages from specific users and provides fun commands."""

    __author__ = ["YourName"]
    __version__ = "1.0.1"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)
        
        default_guild = {
            "blocked_users": [],
            "hawk_users": [
                786624423721041941, 500641384835842049, 275549294969356288,
                685961799518257175, 871044256800854078, 332176051914539010
            ],
            "hawk_enabled": True,
            "gay_enabled": True
        }
        self.config.register_guild(**default_guild)
        
        # Runtime state (not persisted)
        self.awaiting_hawk_response = {}
        self.last_hawk_user = {}

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Show the cog version in help."""
        return f"{super().format_help_for_context(ctx)}\n\nVersion: {self.__version__}"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Delete messages from blocked users and handle hawk responses."""
        # Early returns for efficiency
        if not message.guild:
            return
        
        if message.author.bot:
            return
        
        guild_id = message.guild.id
        
        # Check for hawk response first (more specific)
        if guild_id in self.awaiting_hawk_response:
            user_id = self.awaiting_hawk_response[guild_id]
            if message.author.id == user_id:
                content_lower = message.content.lower().strip()
                if content_lower == "yes":
                    await message.channel.send("I'm a hawk too")
                    del self.awaiting_hawk_response[guild_id]
                    return
                elif content_lower == "no":
                    await message.channel.send("Fuck you then")
                    del self.awaiting_hawk_response[guild_id]
                    return
        
        # Check blocked users
        blocked_users = await self.config.guild(message.guild).blocked_users()
        if message.author.id in blocked_users:
            try:
                await message.delete()
                log.info(
                    f"Deleted message from user {message.author.id} ({message.author}) "
                    f"in guild {message.guild.name} ({message.guild.id})"
                )
            except discord.Forbidden:
                log.warning(
                    f"Missing permissions to delete message in {message.channel.name} "
                    f"({message.channel.id}) in guild {message.guild.name}"
                )
            except discord.HTTPException as e:
                log.error(f"Failed to delete message: {e}")

    # ==================== Message Block Commands ====================
    
    @commands.group(name="msgblock", invoke_without_command=True)
    @commands.is_owner()
    @commands.guild_only()
    async def msgblock(self, ctx: commands.Context):
        """Manage users whose messages are automatically deleted in this server."""
        await ctx.send_help(ctx.command)

    @msgblock.command(name="add")
    async def msgblock_add(self, ctx: commands.Context, user_id: int):
        """Add a user to the message deletion list for this server.
        
        **Arguments:**
        - `<user_id>` - The Discord user ID to block
        
        **Example:**
        - `[p]msgblock add 123456789012345678`
        """
        if user_id <= 0:
            await ctx.send("❌ Invalid user ID. User IDs must be positive numbers.")
            return
        
        async with self.config.guild(ctx.guild).blocked_users() as blocked_users:
            if user_id in blocked_users:
                await ctx.send(f"❌ User ID `{user_id}` is already in the blocked list.")
                return
            
            blocked_users.append(user_id)
        
        await ctx.send(f"✅ Added user ID `{user_id}` to the message deletion list.")
        log.info(f"User {user_id} added to blocked list in guild {ctx.guild.id} by {ctx.author.id}")

    @msgblock.command(name="remove")
    async def msgblock_remove(self, ctx: commands.Context, user_id: int):
        """Remove a user from the message deletion list for this server.
        
        **Arguments:**
        - `<user_id>` - The Discord user ID to unblock
        
        **Example:**
        - `[p]msgblock remove 123456789012345678`
        """
        async with self.config.guild(ctx.guild).blocked_users() as blocked_users:
            if user_id not in blocked_users:
                await ctx.send(f"❌ User ID `{user_id}` is not in the blocked list.")
                return
            
            blocked_users.remove(user_id)
        
        await ctx.send(f"✅ Removed user ID `{user_id}` from the message deletion list.")
        log.info(f"User {user_id} removed from blocked list in guild {ctx.guild.id} by {ctx.author.id}")

    @msgblock.command(name="list")
    async def msgblock_list(self, ctx: commands.Context):
        """Show all users in the message deletion list for this server."""
        blocked_users = await self.config.guild(ctx.guild).blocked_users()
        
        if not blocked_users:
            await ctx.send("📝 The message deletion list is empty for this server.")
            return
        
        user_list = []
        for user_id in blocked_users:
            member = ctx.guild.get_member(user_id)
            if member:
                user_list.append(f"• {member.mention} (`{member}` - `{user_id}`)")
            else:
                user_list.append(f"• `{user_id}` (Not in server)")
        
        embed = discord.Embed(
            title=f"🚫 Blocked Users - {ctx.guild.name}",
            description="\n".join(user_list),
            color=await ctx.embed_color()
        )
        embed.set_footer(text=f"Total: {len(blocked_users)} user(s)")
        await ctx.send(embed=embed)

    # ==================== Fun Commands ====================

    @commands.command(hidden=True)
    async def thanos(self, ctx: commands.Context):
        """Display Thanos image."""
        embed = discord.Embed(color=discord.Color.purple())
        embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425583704532848721/6LpanIV.png")
        await ctx.send(embed=embed)

    @commands.command(hidden=True)
    @commands.guild_only()
    async def hawk(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """Ask a user if they're a hawk.
        
        **Arguments:**
        - `[user]` - Optional: Specific user to ask (defaults to random from hawk list)
        """
        hawk_enabled = await self.config.guild(ctx.guild).hawk_enabled()
        if not hawk_enabled:
            embed = discord.Embed(color=discord.Color.red())
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831928644501624/4rMETw3.gif")
            await ctx.send("❌ The hawk command is currently disabled.", embed=embed)
            return
        
        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        
        if user is None:
            if not hawk_users:
                await ctx.send("❌ No users in the hawk list! Add some with `addhawk <user_id>`")
                return
            
            # Get available users (exclude last pinged if multiple users exist)
            available_users = hawk_users.copy()
            if len(hawk_users) > 1 and ctx.guild.id in self.last_hawk_user:
                last_user = self.last_hawk_user[ctx.guild.id]
                if last_user in available_users:
                    available_users.remove(last_user)
            
            random_user_id = random.choice(available_users)
            user = ctx.guild.get_member(random_user_id)
            
            if not user:
                await ctx.send(f"❌ User ID `{random_user_id}` is not in this server.")
                return
            
            self.last_hawk_user[ctx.guild.id] = random_user_id
        
        self.awaiting_hawk_response[ctx.guild.id] = user.id
        
        # Enable user mentions for the ping
        allowed_mentions = discord.AllowedMentions(users=True)
        await ctx.send(f"{user.mention} Are you a hawk?", allowed_mentions=allowed_mentions)

    @commands.command(hidden=True)
    @commands.guild_only()
    async def gay(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """Check how gay someone is.
        
        **Arguments:**
        - `<user>` - User to check
        """
        gay_enabled = await self.config.guild(ctx.guild).gay_enabled()
        if not gay_enabled:
            embed = discord.Embed(color=discord.Color.red())
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831928644501624/4rMETw3.gif")
            await ctx.send("❌ The gay command is currently disabled.", embed=embed)
            return
        
        if user is None:
            await ctx.send("❌ Please mention a user!")
            return
        
        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        
        # Hawks get higher percentages
        if user.id in hawk_users:
            percentage = random.randint(GAY_PERCENTAGE_MIN_HAWK, GAY_PERCENTAGE_MAX_HAWK)
        else:
            percentage = random.randint(GAY_PERCENTAGE_MIN_NORMAL, GAY_PERCENTAGE_MAX_NORMAL)
        
        # Enable user mentions for the ping
        allowed_mentions = discord.AllowedMentions(users=True)
        await ctx.send(f"{user.mention} is {percentage}% gay", allowed_mentions=allowed_mentions)

    # ==================== Hawk Management Commands ====================

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def addhawk(self, ctx: commands.Context, user_id: int):
        """Add a user to the hawk list.
        
        **Arguments:**
        - `<user_id>` - Discord user ID to add
        """
        if user_id <= 0:
            await ctx.send("❌ Invalid user ID.")
            return
        
        async with self.config.guild(ctx.guild).hawk_users() as hawk_users:
            if user_id in hawk_users:
                await ctx.send(f"❌ User ID `{user_id}` is already in the hawk list.")
                return
            
            hawk_users.append(user_id)
        
        await ctx.send(f"✅ Added user ID `{user_id}` to the hawk list.")

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def removehawk(self, ctx: commands.Context, user_id: int):
        """Remove a user from the hawk list.
        
        **Arguments:**
        - `<user_id>` - Discord user ID to remove
        """
        async with self.config.guild(ctx.guild).hawk_users() as hawk_users:
            if user_id not in hawk_users:
                await ctx.send(f"❌ User ID `{user_id}` is not in the hawk list.")
                return
            
            hawk_users.remove(user_id)
        
        await ctx.send(f"✅ Removed user ID `{user_id}` from the hawk list.")

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def listhawk(self, ctx: commands.Context):
        """List all users in the hawk list."""
        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        
        if not hawk_users:
            await ctx.send("📝 The hawk list is empty for this server.")
            return
        
        user_list = []
        for user_id in hawk_users:
            member = ctx.guild.get_member(user_id)
            if member:
                user_list.append(f"• {member.mention} (`{member}` - `{user_id}`)")
            else:
                user_list.append(f"• `{user_id}` (Not in server)")
        
        embed = discord.Embed(
            title=f"🦅 Hawk Users - {ctx.guild.name}",
            description="\n".join(user_list),
            color=await ctx.embed_color()
        )
        embed.set_footer(text=f"Total: {len(hawk_users)} user(s)")
        await ctx.send(embed=embed)

    # ==================== Toggle Commands ====================

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def disablehawk(self, ctx: commands.Context):
        """Toggle the hawk command on/off."""
        hawk_enabled = await self.config.guild(ctx.guild).hawk_enabled()
        new_status = not hawk_enabled
        await self.config.guild(ctx.guild).hawk_enabled.set(new_status)
        
        status_text = "enabled" if new_status else "disabled"
        
        if new_status:
            embed = discord.Embed(color=discord.Color.green())
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831721160540281/NzusuSn.png")
            await ctx.send(f"✅ Hawk command is now **{status_text}**.", embed=embed)
        else:
            await ctx.send(f"✅ Hawk command is now **{status_text}**.")

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def disablegay(self, ctx: commands.Context):
        """Toggle the gay command on/off."""
        gay_enabled = await self.config.guild(ctx.guild).gay_enabled()
        new_status = not gay_enabled
        await self.config.guild(ctx.guild).gay_enabled.set(new_status)
        
        status_text = "enabled" if new_status else "disabled"
        
        if new_status:
            embed = discord.Embed(color=discord.Color.green())
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831721160540281/NzusuSn.png")
            await ctx.send(f"✅ Gay command is now **{status_text}**.", embed=embed)
        else:
            await ctx.send(f"✅ Gay command is now **{status_text}**.")

    # ==================== Spam Ping Command ====================

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    @commands.bot_has_permissions(send_messages=True)
    async def spamping(
        self,
        ctx: commands.Context,
        user: discord.Member,
        amount: Optional[int] = DEFAULT_PING_AMOUNT
    ):
        """Ping a user multiple times.
        
        **Arguments:**
        - `<user>` - User to ping (mention or ID)
        - `[amount]` - Number of pings (default: 5, max: 20)
        
        **Example:**
        - `[p]spamping @User 10`
        """
        # Validate amount
        if amount < 1:
            await ctx.send("❌ Amount must be at least 1.")
            return
        
        if amount > MAX_PING_AMOUNT:
            await ctx.send(f"❌ Amount cannot exceed {MAX_PING_AMOUNT} to prevent rate limits.")
            return
        
        # Configure allowed mentions to actually ping the user
        allowed_mentions = discord.AllowedMentions(users=True)
        
        # Send initial confirmation
        await ctx.send(f"🔔 Pinging {user.mention} {amount} time(s)...", allowed_mentions=allowed_mentions)
        
        # Perform pings with rate limit protection
        successful_pings = 0
        for i in range(amount):
            try:
                await ctx.send(user.mention, allowed_mentions=allowed_mentions)
                successful_pings += 1
                
                # Delay between pings (except last one)
                if i < amount - 1:
                    await asyncio.sleep(PING_DELAY)
                    
            except discord.HTTPException as e:
                log.error(f"Error during spamping in guild {ctx.guild.id}: {e}")
                await ctx.send(f"❌ Error after {successful_pings} pings: {e}")
                break
            except asyncio.CancelledError:
                log.info(f"Spamping cancelled in guild {ctx.guild.id}")
                await ctx.send(f"⚠️ Cancelled after {successful_pings} pings.")
                raise
        
        # Send completion message
        await ctx.send(f"✅ Finished pinging {user.mention} ({successful_pings}/{amount} successful).", allowed_mentions=allowed_mentions)


async def setup(bot):
    """Load the MessageDelete cog."""
    await bot.add_cog(MessageDelete(bot))

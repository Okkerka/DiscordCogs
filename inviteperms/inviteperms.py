"""InvitePerms — owner-only, DM-only one-time-use invites with a chosen role.

Red-DiscordBot cog. Requires discord.py >= 2.5 (Components V2).
Bot needs Create Instant Invite + Manage Roles + Manage Guild on the server,
and its top role must be above the role it assigns.
"""

import discord
from discord import ui
from redbot.core import commands, Config
from redbot.core.bot import Red
from typing import Dict, Optional


class RoleSelect(ui.ActionRow):
    def __init__(self, view: "InviteView"):
        guild = view.guild
        options = [
            discord.SelectOption(label=r.name[:100], value=str(r.id))
            for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
            if not r.managed and r != guild.default_role and r < guild.me.top_role
        ][:25]
        self._view = view
        self.select = ui.Select(
            placeholder="Pick the role this invite grants...",
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        self._view.role_id = int(self.select.values[0])
        await interaction.response.defer()


class CreateButton(ui.ActionRow):
    def __init__(self, view: "InviteView"):
        self._view = view
        btn = ui.Button(label="Create invite", style=discord.ButtonStyle.success)
        btn.callback = self._create
        self.add_item(btn)

    async def _create(self, interaction: discord.Interaction):
        await self._view.finish(interaction)


class InviteView(ui.LayoutView):
    def __init__(self, cog: "InvitePerms", guild: discord.Guild):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.role_id: Optional[int] = None

        c = ui.Container(accent_colour=discord.Colour.blurple())
        c.add_item(ui.TextDisplay(
            f"## One-time invite — {guild.name}\n"
            "Pick the role the joiner gets. Invite: 1 use, never expires."
        ))
        c.add_item(ui.Separator())
        c.add_item(RoleSelect(self))
        c.add_item(ui.Separator())
        c.add_item(CreateButton(self))
        self.add_item(c)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)

    async def finish(self, interaction: discord.Interaction):
        if not self.role_id:
            await interaction.response.send_message("Pick a role first.", ephemeral=True)
            return
        channel = (self.guild.system_channel
                   or next((ch for ch in self.guild.text_channels
                            if ch.permissions_for(self.guild.me).create_instant_invite), None))
        if channel is None:
            await interaction.response.send_message(
                "No channel where I can create invites there.", ephemeral=True)
            return
        try:
            invite = await channel.create_invite(
                max_uses=1, unique=True, reason="InvitePerms owner invite")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I lack Create Instant Invite there.", ephemeral=True)
            return

        async with self.cog.config.guild(self.guild).invites() as invites:
            invites[invite.code] = self.role_id
        self.cog._invite_cache.setdefault(self.guild.id, {})[invite.code] = 0

        role = self.guild.get_role(self.role_id)
        c = ui.Container(accent_colour=discord.Colour.green())
        c.add_item(ui.TextDisplay(
            f"### Invite created\n{invite.url}\nGrants: **{role.name if role else '?'}** (1 use)"
        ))
        view = ui.LayoutView(timeout=None)
        view.add_item(c)
        await interaction.response.edit_message(view=view)


class InvitePerms(commands.Cog):
    """Owner-only DM invites: 1 use, grants a chosen role on join."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x1AB0CAFE, force_registration=True)
        self.config.register_guild(invites={})
        self._invite_cache: Dict[int, Dict[str, int]] = {}

    async def cog_load(self):
        for guild in self.bot.guilds:
            await self._refresh_cache(guild)

    async def _refresh_cache(self, guild: discord.Guild):
        if not guild.me.guild_permissions.manage_guild:
            return
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            return
        self._invite_cache[guild.id] = {i.code: i.uses for i in invites}

    @commands.is_owner()
    @commands.dm_only()
    @commands.command(name="invitep")
    async def invite_cmd(self, ctx: commands.Context, guild: discord.Guild):
        """`[p]invite <server_id>` — pick a role, get a one-time invite."""
        if guild not in self.bot.guilds:
            return await ctx.send("I'm not in that server.")
        me = guild.me
        if not me.guild_permissions.create_instant_invite:
            return await ctx.send("I need **Create Instant Invite** there.")
        if not me.guild_permissions.manage_roles:
            return await ctx.send("I need **Manage Roles** there.")
        await ctx.send(view=InviteView(self, guild))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild = member.guild
        data = await self.config.guild(guild).invites()
        if not data:
            return
        old = self._invite_cache.get(guild.id, {})
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            return
        used = next((i.code for i in invites
                     if i.code in data and i.uses > old.get(i.code, 0)), None)
        self._invite_cache[guild.id] = {i.code: i.uses for i in invites}
        if used is None:
            return
        role = guild.get_role(data.pop(used, None) or 0)
        async with self.config.guild(guild).invites() as stored:
            stored.pop(used, None)
        if role and role < guild.me.top_role:
            try:
                await member.add_roles(role, reason=f"InvitePerms invite {used}")
            except discord.Forbidden:
                pass
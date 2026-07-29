"""BotInvite — owner-only, DM-only OAuth2 bot invite-link builder.

Requires discord.py >= 2.5 for Components V2.
This creates an OAuth2 URL that asks for ONLY the permissions selected.
It does not create server/member invite links or change any server settings.
"""

from urllib.parse import urlencode

import discord
from discord import ui
from redbot.core import commands
from redbot.core.bot import Red


PERMISSIONS = [
    ("View Channels", "view_channel"),
    ("Send Messages", "send_messages"),
    ("Embed Links", "embed_links"),
    ("Attach Files", "attach_files"),
    ("Read Message History", "read_message_history"),
    ("Add Reactions", "add_reactions"),
    ("Use External Emojis", "external_emojis"),
    ("Manage Messages", "manage_messages"),
    ("Connect", "connect"),
    ("Speak", "speak"),
    ("Use Voice Activity", "use_voice_activation"),
    ("Mute Members", "mute_members"),
    ("Deafen Members", "deafen_members"),
    ("Move Members", "move_members"),
    ("Manage Roles", "manage_roles"),
    ("Kick Members", "kick_members"),
    ("Ban Members", "ban_members"),
    ("Manage Channels", "manage_channels"),
    ("Manage Webhooks", "manage_webhooks"),
    ("Manage Server", "manage_guild"),
    ("Administrator", "administrator"),
]


class PermissionPicker(ui.ActionRow):
    def __init__(self, view: "BotInviteView"):
        self.view_ref = view
        self.select = ui.Select(
            placeholder="Select requested bot permissions...",
            min_values=0,
            max_values=min(21, len(PERMISSIONS)),
            options=[
                discord.SelectOption(label=label, value=value)
                for label, value in PERMISSIONS
            ],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        self.view_ref.selected = set(self.select.values)
        await interaction.response.defer()


class GenerateButton(ui.ActionRow):
    def __init__(self, view: "BotInviteView"):
        self.view_ref = view
        button = ui.Button(label="Generate OAuth2 invite", style=discord.ButtonStyle.success)
        button.callback = self.generate
        self.add_item(button)

    async def generate(self, interaction: discord.Interaction):
        await self.view_ref.generate(interaction)


class BotInviteView(ui.LayoutView):
    def __init__(self, cog: "BotInvite"):
        super().__init__(timeout=300)
        self.cog = cog
        self.selected = set()

        container = ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(ui.TextDisplay(
            "## Bot OAuth2 invite builder\n"
            "Select the permissions DripBot should request, then generate its install link."
        ))
        container.add_item(ui.Separator())
        container.add_item(PermissionPicker(self))
        container.add_item(ui.Separator())
        container.add_item(GenerateButton(self))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)

    async def generate(self, interaction: discord.Interaction):
        perms = discord.Permissions.none()
        for name in self.selected:
            setattr(perms, name, True)

        query = urlencode({
            "client_id": str(self.cog.bot.user.id),
            "scope": "bot applications.commands",
            "permissions": str(perms.value),
        })
        url = f"https://discord.com/oauth2/authorize?{query}"
        selected_names = [label for label, flag in PERMISSIONS if flag in self.selected]
        chosen = ", ".join(selected_names) if selected_names else "No guild permissions"

        container = ui.Container(accent_colour=discord.Colour.green())
        container.add_item(ui.TextDisplay(
            f"### OAuth2 invite generated\n"
            f"Requested permissions: **{chosen}**\n"
            f"{url}"
        ))
        done = ui.LayoutView(timeout=None)
        done.add_item(container)
        await interaction.response.edit_message(view=done)


class BotInvite(commands.Cog):
    """Build an OAuth2 bot invite URL from a Components V2 permission picker."""

    def __init__(self, bot: Red):
        self.bot = bot

    @commands.is_owner()
    @commands.dm_only()
    @commands.command(name="invitep")
    async def invitep(self, ctx: commands.Context):
        """Open an owner-only DM panel to build DripBot's OAuth2 invite URL."""
        await ctx.send(view=BotInviteView(self))

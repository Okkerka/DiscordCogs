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
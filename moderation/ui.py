"""Invoker-only moderation controls."""

import discord


class ConfirmView(discord.ui.View):
    """Confirm one previewed batch; timeout and cancellation never apply changes."""

    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the requesting moderator can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Apply nickname changes", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled. No nicknames changed.", view=None)
        self.stop()


class HistoryView(discord.ui.View):
    """Paginate an already-authorized history snapshot."""

    def __init__(self, author_id: int, pages: list[discord.Embed], page: int = 0):
        super().__init__(timeout=120)
        self.author_id, self.pages, self.page = author_id, pages, page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Run modhistory to open your own history view.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = (self.page - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = (self.page + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

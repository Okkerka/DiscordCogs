"""Small invoker-owned utility controls."""

import discord


class ReminderView(discord.ui.View):
    """Let the reminder creator cancel a newly scheduled reminder."""

    def __init__(self, cog, author_id: int, reminder_id: str):
        super().__init__(timeout=180)
        self.cog, self.author_id, self.reminder_id = cog, author_id, reminder_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This reminder belongs to another user.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Cancel reminder", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        removed = await self.cog._cancel_reminder(self.author_id, self.reminder_id)
        button.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.followup.send("Reminder cancelled." if removed else "Reminder already delivered or removed.", ephemeral=True)
        self.stop()

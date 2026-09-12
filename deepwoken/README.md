# Deepwoken

Public weapon lookup and sustained-DPS rankings using all supplied build stats.
Examples below use `>` as the bot prefix.

| Action | Prefix | Slash |
| --- | --- | --- |
| Look up a weapon | `>dwweapon Enforcer's Axe` | `/dwweapon query:Enforcer's Axe` |
| Rank eligible weapons | `>dwweapon heavy 100 medium 100 prof 6` | `/dwweapon query:heavy 100 medium 100 prof 6` |
| Update from the wiki (bot owner) | `>dwweapon updatelist` | `/dwweapon query:updatelist` |
| Reload saved data (bot owner) | `>dwreload` | `/dwreload` |

`>dw` and `>weapon` remain prefix aliases. Stats range from 0 to 100 and
proficiency from 0 to 6. Include any required attributes or attunements in the
query. Stat ordering does not affect the ranking; anyone can use the page controls.

After updating and loading the cog, a bot owner can enable slash commands with:

```text
>slash enablecog Deepwoken
>slash sync
```

Red controls registration; the cog does not automatically sync the bot's tree.
See [Red's slash command guide](https://docs.discord.red/en/stable/guide_slash_and_interactions.html).

Updates use the bot's Red cog-data directory: one active `weapons.xlsx` and one
overwritten `weapons.backup.xlsx`. Temporary candidates are cleaned up. Invalid
updates preserve the active list; missing or invalid runtime data uses the bundled
workbook. Conflicting wiki rows are reported and trusted existing rows retained;
new weapons with conflicting statistics are excluded until their source is consistent.

Rankings enforce numeric stat requirements. Non-stat unlock alternatives are not
modeled. DPS includes listed endlag but excludes bleed, criticals, procs, enchants,
talents, penetration and resistance; lookup-only offhands are not DPS-ranked.

Development checks (Python 3.10+, with Red/discord.py and openpyxl installed):

```text
python -m unittest discover -s deepwoken/tests
```

The suite includes real command registration without connecting to Discord.
Live interaction delivery and server registration still need a running bot.

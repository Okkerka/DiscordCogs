# Deepwoken

Public weapon lookup and sustained-DPS rankings using all supplied build stats.
Examples below use `>` as the bot prefix.

| Action | Prefix | Slash |
| --- | --- | --- |
| Look up a weapon | `>dwweapon Enforcer's Axe` | `/dwweapon query:Enforcer's Axe` |
| Rank eligible weapons | `>dwweapon heavy 100 medium 100 prof 6` | `/dwweapon query:heavy 100 medium 100 prof 6` |
| Rank against resistance | `>dwweapon heavy 100 medium 100 prof 6 res 35 50 sort 50` | `/dwweapon query:heavy 100 medium 100 prof 6 res 35 50 sort 50` |
| Update from the wiki (bot owner) | `>dwweapon updatelist` | `/dwweapon query:updatelist` |
| Reload saved data (bot owner) | `>dwreload` | `/dwreload` |

`>dw` and `>weapon` remain prefix aliases. Lookup is case-insensitive, ignores
harmless name formatting, and suggests the closest match for minor typos. Stats
range from 0 to 100 and proficiency from 0 to 6. Stats not listed in the query
count as 0, so include every required attribute or attunement. Attunements accept
names and short forms such as `flame`/`fire`, `frost`/`ice`, `lightning`/`thunder`,
`wind`/`gale`, `shadow`, `blood`/`bloodrend`, and `ironsing`/`metal`/`iron`.
Stat ordering does not affect the ranking; anyone can use the page controls.

Add `res 35 50` to a stat query to compare damage against 35% and 50%
resistance. Effective resistance is `RES * (1 - PEN/100)`, and weapon PEN caps
at 50%. Each result shows its normal PEN and 3-star PEN (+15%, capped at 50%)
with DPS and M1 at every requested resistance. Results default to the first
requested resistance; add `sort 50` to order by another requested target, for
example `res 35 50 sort 50`. Append `m1` to sort by 3-star M1 damage instead of
normal-PEN DPS, for example `res 35 50 sort 50 m1`. The sort value must match
one of the `res` values.

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
workbook. Individually malformed wiki rows are skipped and logged instead of
rejecting the whole update, while a table that loses a required column still fails
safely. Conflicting wiki rows are reported and trusted existing rows retained;
new weapons with conflicting statistics are excluded until their source is consistent.

Rankings enforce numeric stat requirements. Non-stat unlock alternatives are not
modeled. `M1` is the scaled damage of one hit at the supplied stats and
proficiency. Swing Speed is a multiplier over a 0.5-second baseline swing, not a
duration in seconds, so `DPS = M1 / (0.5 / Swing Speed + Endlag)`. DPS excludes
bleed, criticals, procs, enchants, talents, penetration and resistance unless a
`res` query is used; lookup-only offhands are not DPS-ranked.

Development checks (Python 3.10+, with Red/discord.py and openpyxl installed):

```text
python -m unittest discover -s deepwoken/tests
```

The suite includes real command registration without connecting to Discord.
Live interaction delivery and server registration still need a running bot.

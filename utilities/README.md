# Utilities

All commands have prefix and slash forms **except `thanos`, which is prefix-only**.
Existing aliases remain prefix aliases. No `servericon`, `channelinfo` or
`roleinfo` commands are added. Owner-only controls remain owner-only.

| Prefix example | Slash form | Behavior |
| --- | --- | --- |
| `>remindme 2h take a break` | `/remindme` | One-time personal DM reminder with a cancel button. |
| `>reminders list` | `/reminders list` | Private reminder list, including delivery failures. |
| `>reminders cancel ID` | `/reminders cancel` | Cancel your own reminder by ID. |
| `>quote https://discord.com/channels/GUILD/CHANNEL/MESSAGE` | `/quote` | Author, message text, timestamp and a jump link. |
| `>timestamp "2026-12-25 18:00" Europe/Budapest` | `/timestamp` | Copyable Discord timestamp codes and local-time previews. |
| `>membercount` | `/membercount` | Human, bot and total member counts. |

The default timestamp timezone is UTC. Use an IANA timezone such as
`Europe/Budapest`, or an explicit offset in the date/time. Ambiguous or nonexistent
daylight-saving transition times require an explicit offset. `tzdata` supplies
the standard library's timezone database on systems without an OS copy, including
Windows; no external date service is used.

Reminders support 10 seconds through 365 days and up to 1500 characters, with a
maximum of 20 saved reminders per user. One scheduler checks persistent reminders
every 15 seconds; overdue reminders are delivered after a restart. Closed DMs
produce a saved failure visible in the reminder list, not a public fallback.
Transient delivery errors retry up to five times. Delivery and cancellation are
serialized, so cancellation reports if delivery already finished. A process
crash after Discord accepts a DM but before the saved record is removed can
result in a duplicate after restart. Reminder text is removed after successful
delivery; failed reminders can be cancelled. Red user-data deletion clears personal
reminders. Prefix reminder creation includes your text in the invoking channel;
use slash commands or DMs for private creation.

Prefix quotes stay in their source channel. Slash quotes are private and may
reference another channel in the same server only when both the caller and bot
can read its history. Private threads additionally require caller membership or
Manage Threads permission. Attachments remain behind the original message link.

`choose` accepts space-separated options, quoted multiword options, or `|`
separators. `addhawk` accepts space-separated mentions/IDs or quoted names in a
single slash field. Existing polls, dice, avatar, user/server info, joke commands
and owner-only timed-ping controls also have slash forms.

## Slash activation

Reload, then use Red's owner commands `slash enablecog Utilities` and `slash sync`.
Utilities registers 25 top-level slash commands. Thanos is not registered.
See [Red's slash-command guide](https://docs.discord.red/en/stable/guide_slash_and_interactions.html).

Tests and verification commands are documented in the Moderation README.

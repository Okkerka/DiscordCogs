# RandomTexts

Open `>randomtext` or `/randomtext settings` for the Components V2 settings panel.
Choose channels and categories, enable/disable automatic posting, set the message
frequency, or preview a result privately. Controls are restricted to the opener
and recheck administrator access. They expire after three minutes of inactivity;
settings persist across reloads and restarts.

Requires Red 3.5.24+, Python 3.11+, and Red's discord.py 2.6+ for
[Components V2](https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.ui.LayoutView).
Update Red if necessary; do not install a different Discord library over Red's.
No additional dependencies are needed.

| Prefix example | Slash equivalent | Purpose |
| --- | --- | --- |
| `>randomtext` | `/randomtext settings` | Open settings and preview UI |
| `>randomtext toggle` | `/randomtext toggle` | Enable/disable automatic posts |
| `>randomtext frequency 30 80` | `/randomtext frequency` | Set a saved range and reset the counter |
| `>randomtext settarget 50` | `/randomtext settarget` | Set only the next threshold, as before |
| `>randomtext category brainrot false` | `/randomtext category` | Toggle one content category |
| `>randomtext channel add #general` | `/randomtext channel` | Add an automatic posting channel |
| `>randomtext channel remove #general` | `/randomtext channel` | Remove a channel from a multi-channel allowlist |
| `>randomtext channel all` | `/randomtext channel` | Restore server-wide posting |
| `>copypasta` | `/copypasta` | Public manual command; 15-second per-user cooldown |

Settings require Manage Server, Red administrator, or bot-owner access. Copypasta
stays public. Automatic content requires the Message Content intent, and respects
Red's disabled-cog and allow/block-list settings. Slash registration is managed by
Red: `>slash enablecog RandomText`, then `>slash sync`.

Existing enabled state, counter, and next target are retained. Until configured,
all channels and all four categories are eligible, with the previous 10–100 range.
Channel allowlists also include those channels' threads. Empty means all channels;
removing the last channel through the command requires explicitly using `all` or
disabling automatic posts. At least one category must remain enabled.

Only eligible human non-command messages count. A server has one counter, and the
message reaching the threshold determines the destination. Counters are saved
under a per-server lock, with one persistence write per counted message. Settings
reads are cached and simultaneous cache misses share one read. Messages arriving
while an automatic post is being generated are ignored to prevent overlapping
posts. Failed posts consume the threshold; they do not retry on every new message.

External requests have an 8-second network timeout, a 10-second overall deadline
including capacity waits, a 512 KiB response cap and four shared request slots.
RSS results are cached for five minutes. Sources can be unavailable
or repetitive; only enabled categories are tried, and the bot stays quiet if none
produce content. Preview reports that failure privately. Long results are bounded
to three embed pages (or smaller plaintext pages without Embed Links), with
mentions disabled. Existing brainrot content remains unchanged.

Verification:

```text
python -m pytest randomtexts/tests chattriggers/tests --import-mode=importlib -q -o asyncio_default_fixture_loop_scope=function
```

The tests use real Discord command registration and component serialization with
fake storage/network boundaries. They do not connect to Discord. After reload,
verify the panel, private slash responses, category/channel choices and an
automatic post in a test channel.

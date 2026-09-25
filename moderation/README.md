# Moderation

Existing prefix commands and aliases remain available. All moderation commands
also have slash forms. Custom moderators still need the relevant Discord
permission; bot/server-owner exceptions remain as before. Administrative and
bot-owner-only configuration remains restricted on both command surfaces.

| Prefix example (`>` is your configured prefix) | Slash form | Behavior |
| --- | --- | --- |
| `>purge 100 bots` | `/purge recent` | Preserved amount-first syntax and filters. |
| `>purge bots 100 @MusicBot` | `/purge bots` | Bot-only cleanup, optionally restricted to one bot. |
| `>purge user @Member 100` | `/purge user` | Member-specific cleanup. |
| `>purge contains 100 spam` | `/purge contains` | Case-insensitive text matching. |
| `>purge embeds 100` | `/purge embeds` | Embeds or uploaded files, matching the existing filter. |
| `>purge attachments 100` | `/purge attachments` | Uploaded files only. |
| `>purge links 100` | `/purge links` | HTTP/HTTPS links in message text. |
| `>cleanup 100` | `/cleanup` | This bot's replies and recognized prefix command messages. |
| `>dehoist @Member` | `/dehoist member` | Strip conservative leading punctuation/whitespace. |
| `>dehoist preview` | `/dehoist preview` | Preview eligible members without changing names. |
| `>dehoist all` | `/dehoist all` | Preview, then invoker-only confirm/cancel buttons (60-second timeout). |
| `>nickname reset @Member` | `/nickname reset` | Clear a server nickname; `rename` also still supports this. |
| `>modhistory @Member 1` | `/modhistory` | Five actions per page with navigation buttons; also accepts a user ID. |

Purge amounts are **messages scanned**, between 1 and 1000, before the invocation.
The result reports actual deletions. Pins are protected unless the existing
explicit `purge 100 pins` mode is selected. Bot-only purge preserves human chat;
`cleanup` intentionally deletes recognized human command messages too. Cleanup
resolves command candidates first, then rechecks pins during deletion. Neither
command deletes its own invocation. Discord failures may leave a partial cleanup.

Dehoist preserves non-English names, skips empty results and protected roles,
and rechecks member names and hierarchy after confirmation. It is manual, not an
automatic nickname policy. A complete member cache requires the members intent.

History is independent of the optional modlog channel. It records new actions
from this cog, retaining the latest 500 per target per server. It does not import
old actions from Discord audit logs or old warnings; existing warnings remain
available through `warnings`. Slash history responses are private; prefix history
is posted in the invoking channel, as with existing warning commands.

## Slash activation

Reload the cog, then use Red's owner commands `slash enablecog Moderation` and
`slash sync`. Existing installations may have other slash commands consuming
Discord's command limit. This cog registers 29 top-level slash commands.

See [Red's slash-command guide](https://docs.discord.red/en/stable/guide_slash_and_interactions.html).

## Verification

From the repository root in a development environment with Red-DiscordBot,
pytest and pytest-asyncio installed:

```text
python -m pytest moderation/tests utilities/tests --import-mode=importlib -q -o asyncio_default_fixture_loop_scope=function
```

No tests connect to Discord or perform real moderation actions. Live server
registration and permission checks should also be smoke-tested after reload.

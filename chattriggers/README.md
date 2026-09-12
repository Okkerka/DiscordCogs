# ChatTriggers

Open `>chattrigger` (alias `>alert`) or `/chattrigger settings`. The Components V2
panel supports all triggers through pagination, editing, enable/disable, cooldowns,
channel selection, testing, and confirmed deletion. Controls expire after three
minutes of inactivity and recheck the opener's current permissions. Settings are
saved immediately and survive reloads; reopen the panel after a reload.

Requires Red 3.5.24+, Python 3.11+, and Red's discord.py 2.6+ for
[Components V2](https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.ui.LayoutView).
Update Red if necessary rather than replacing its Discord library separately.

## Creating a trigger

Choose **New**, select sound behavior, then **Continue to trigger details**:

- **Skip trigger sound while music is busy:** preserve playing, paused or queued
  music. The text/image alert still appears. This is the default for new triggers.
- **Interrupt music:** stop current music and clear its queue before playing the
  trigger sound. The sound is resolved first, so an invalid/unavailable result
  does not stop the existing track. Playback can still fail after it starts.

Fill in a phrase (1–50 characters), sound URL, image/GIF URL, title, and message.
At least one output field is required. New triggers start enabled, in all
channels, with a 30-second server-wide cooldown. Select the saved trigger to
change its sound behavior, channels, or cooldown (0–3600 seconds).

Existing triggers keep their previous interrupt behavior and no cooldown until
you change them. Existing IDs, permissions, phrases and disabled states are
retained. Editing a phrase cannot overwrite a different trigger, and stale edits
or deletion confirmations are rejected.

## Commands and permissions

| Prefix example | Slash equivalent | Purpose |
| --- | --- | --- |
| `>chattrigger` | `/chattrigger settings` | Manage settings |
| `>chattrigger list` | `/chattrigger list` | Public paginated trigger list |
| `>chattrigger add_perm @User` | `/chattrigger add_perm` | Allow trigger firing |
| `>chattrigger remove_perm @User` | `/chattrigger remove_perm` | Revoke explicit firing access |
| `>chattrigger add_manager @User` | `/chattrigger add_manager` | Grant management and firing access |
| `>chattrigger remove_manager @User` | `/chattrigger remove_manager` | Revoke explicit manager access |
| `>chattrigger cooldown 60 alert phrase` | `/chattrigger cooldown` | Set per-trigger cooldown |
| `>chattrigger audio skip alert phrase` | `/chattrigger audio` | Skip sound while music is busy |
| `>chattrigger audio interrupt alert phrase` | `/chattrigger audio` | Interrupt music for this sound |
| `>chattrigger test alert phrase` | `/chattrigger test` | Send a real test alert, including sound |

Management retains the existing policy: Manage Server, bot owner, or an explicitly
assigned trigger manager. Managers can grant/revoke permissions as before. Normal
phrase firing is restricted to the bot owner, assigned managers and allowed users;
Manage Server alone does not automatically grant phrase firing. Revoking firing
permission does not override a separately granted manager role or bot ownership.
The public list exposes phrases and enabled state, not stored media URLs.

Matching is case-insensitive substring matching, preserving the first active
match in saved order. Long messages are no longer arbitrarily excluded. Channel
restrictions include threads under selected channels; empty means unrestricted.
The Message Content intent is required. Red's disabled-cog and allow/block lists
are respected. A server dispatches at most one alert at a time; further matches
during delivery are skipped. Cooldowns start at admission, including failed
delivery attempts, and reset on cog reload. Tests can preview a disabled trigger,
but respect channels and cooldowns (at least three seconds between tests).

Slash registration is managed by Red: `>slash enablecog ChatTriggers`, then
`>slash sync`. All commands require a server. Slash management replies are private;
prefix panels and real test alerts appear in the current channel. The bot no
longer deletes your command messages.

## Audio and verification

Optional sounds use the existing **Red Audio/Lavalink** backend. The user must be
in voice, and the bot needs Connect/Speak. An interrupting alert may move that
player to the triggering user's voice channel, preserving the previous behavior.
Sounds are skipped if Audio is unavailable or TidalPlayerExp owns native voice;
the cog does not create a competing native connection. Visual alerts still work.
The Test button/command reports sound failures privately. Automatic failures are
logged without media URLs or tokens. Embed Links is recommended; text fallback
has mentions disabled. No new dependencies are installed.

```text
python -m pytest randomtexts/tests chattriggers/tests --import-mode=importlib -q -o asyncio_default_fixture_loop_scope=function
```

Tests cover registration, component serialization, permissions, concurrent firing,
configuration edits, and audio behavior using fake external boundaries. They do
not connect to Discord/Lavalink. After reload, create one trigger for each audio
mode and test against playing and paused music in a test voice channel; verify
the skip mode preserves the queue and interrupt mode clears it.

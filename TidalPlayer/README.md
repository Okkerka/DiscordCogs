# TidalPlayer

TidalPlayer queues Tidal content through Red Discord Bot's Audio cog and its
Red-managed Lavalink deployment. This cog does not start, configure, or own a
separate Lavalink node.

## Prerequisites

- Red Discord Bot 3.5 or newer.
- Red's Audio cog loaded before TidalPlayer: `[p]load audio`.
- A healthy Audio/Lavalink deployment. In Red's managed Audio mode, Red owns the
  Lavalink process; Java 17 or 21 is required by Red Audio.
- `tidalapi` installed in Red's Python environment.

Optional Spotify imports use Red shared API tokens:

```text
[p]set api spotify client_id,<client-id> client_secret,<client-secret>
```

Optional YouTube playlist imports use:

```text
[p]set api youtube api_key,<api-key>
```

## Installation and update

Install from a Downloader repository using the repository's cog name. Load Audio
first, then TidalPlayer:

```text
[p]load audio
[p]load TidalPlayer
[p]tidalsetup login
```

Update code with:

```text
[p]cog update TidalPlayer
[p]reload TidalPlayer
```

Tidal OAuth state is stored in Red Config, not in the installed cog directory,
so a normal Downloader update preserves it. Do not use `[p]tidalsetup logout`
unless you intend to remove stored authentication.

## Operational boundaries

- Tidal stream URLs are short-lived and must never be logged or persisted.
- Red Audio owns voice connections, Lavalink nodes, and player lifecycle.
- If Audio is unavailable, TidalPlayer must fail closed with a user-safe error.
- Audio compatibility changes require testing against the exact deployed Red
  release before changing the audio gateway implementation.

## Development validation

```text
python -m compileall -q TidalPlayer
python -m pytest -q TidalPlayer/tests
```

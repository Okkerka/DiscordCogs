# TidalPlayerExp

Experimental TIDAL and YouTube playback through native Discord voice. No Java,
Lavalink server, or Red Audio cog is needed. The original `TidalPlayer` folder
is unchanged; this cog has its own configuration and TIDAL login.

## Requirements

- Red 3.5.24 or newer on Python 3.11 or newer. Red owns its `discord.py` version;
  do not install a separate Discord library for this cog.
- The cog requirements from `info.json`, including PyNaCl, davey (Discord DAVE),
  imageio-ffmpeg, yt-dlp with EJS, and Deno. Downloader installs these. Restart
  Red after installing voice dependencies: Discord detects them at startup.
- A host with compatible dependency wheels/binaries, permission to run FFmpeg
  and Deno, and outbound Discord voice and media access. No Lavalink host access
  is required. Some restricted hosting plans cannot run native voice.

FFmpeg is normally supplied by imageio-ffmpeg. An administrator may select a
local executable with `IMAGEIO_FFMPEG_EXE`; otherwise the cog checks its packaged
binary, Conda location, then `PATH`. Playback never installs or updates binaries.

## Install, switch, and test

Replace `[p]` with your bot prefix and `<repo-name>` with the name you gave the
Downloader repository:

```text
[p]cog install <repo-name> TidalPlayerExp
[p]unload TidalPlayer
[p]unload audio
[p]load TidalPlayerExp
[p]tidalsetup doctor
```

Restart Red after dependency installation if doctor says a restart is required.
Both old and experimental cogs use the same commands, so they cannot be loaded
together. This cog also refuses to share voice ownership with Audio or another
cog; it never unloads other cogs for you.

Join a voice channel and try:

```text
[p]tplay <YouTube video URL>
[p]tidalsetup login
[p]tplay <TIDAL URL or song search>
[p]tqueue
```

The bot needs Connect and Speak permissions. Playback controls require you to be
in its voice channel. `tidalsetup doctor` is owner-only and checks local versions,
voice readiness, FFmpeg features, YouTube dependencies, cached TIDAL login state,
and voice ownership. It does not connect, fetch media, or revalidate credentials.

To return to the original cog:

```text
[p]unload TidalPlayerExp
[p]load audio
[p]load TidalPlayer
```

The original cog's saved configuration remains separate. Do not log out merely
to switch cogs. Update this experiment with `[p]cog update TidalPlayerExp`, update
its requirements when needed, and restart/reload as appropriate.

## Sources and controls

- YouTube video links work without a YouTube API key or TIDAL login. With TIDAL
  authentication, the cog tries a confident catalog match; otherwise it plays
  the original video's audio. If the TIDAL source cannot start, it falls back to
  that same YouTube video. A fallback displays the YouTube title and link.
- A video URL containing `list=` still plays that video only. Use an explicit
  YouTube `/playlist?list=...` URL to request playlist import. Keyless imports
  are capped at 100 items; the optional Data API path supports up to 1,000.
- TIDAL links/search and Spotify-to-TIDAL imports require TIDAL authentication.
  Spotify setup is optional: `[p]tidalsetup spotify` and
  `[p]tidalsetup spotifylogin`. An optional YouTube Data API key can be configured
  with `[p]tidalsetup youtube`.
- Use the now-playing panel to pause/resume, skip, stop, choose a suggestion, or
  toggle autoplay. Autoplay generates TIDAL tracks only and needs TIDAL login.
- `[p]tstop` cancels a running playlist **import**, leaving admitted tracks alone.
  The controller's **Stop** button stops playback and clears the queue.

## Reliability and resource use

Queues hold stable track/video identifiers and display metadata, not expiring
media URLs. A source is resolved only when it is about to play. Each guild has
one serial playback worker and one active FFmpeg process; YouTube extraction
runs in at most two isolated child processes across the cog. No media is saved
to disk. This avoids Java/Lavalink overhead but still uses CPU for audio encoding,
RAM for buffers, and network bandwidth per playing guild. It is not a benchmark
claim: test concurrent guilds on your actual host.

Compatible 20 ms stereo 48 kHz Opus packets can be passed through. Other inputs
are encoded to 128 kbps Discord Opus. TIDAL catalog quality labels describe
availability, **not measured source quality or lossless Discord delivery**.
The existing tidalapi source-request default is preserved.

Startup retries use fresh source resolution; a primary gets one retry before
its optional YouTube fallback. Three consecutive failed entries stop automatic
queue progression. Idle empty sessions disconnect after approximately two
minutes. Source startup, extraction, and cleanup have bounded deadlines.

Private, age/login-restricted, removed, region-blocked, or rate-limited videos
can still fail. Provider changes may require owner-driven dependency updates.
There is no cookie/login restriction bypass or DRM decryption. Encrypted or
segmented TIDAL track manifests are unsupported by the direct-stream adapter.
Use sources you are authorized to play and respect provider terms.

## Development checks

Use a dedicated Python 3.11+ environment with Red 3.5.24, the cog requirements,
and `tests/requirements-dev.txt` installed:

```text
python -m pytest TidalPlayerExp/tests -q
python -m compileall -q TidalPlayerExp
```

Tests include deterministic voice/provider doubles and local FFmpeg checks.
They do not establish successful playback on a real Discord hosting machine;
the owner must run the voice smoke test above before wider use.

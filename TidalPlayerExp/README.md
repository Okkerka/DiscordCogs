# TidalPlayerExp

Experimental TIDAL, YouTube, SoundCloud, and Bandcamp playback through native Discord voice. No Java,
Lavalink server, or Red Audio cog is needed. The original `TidalPlayer` folder
is unchanged; this cog has its own configuration and TIDAL login.

## Requirements

- Red 3.5.24 or newer on Python 3.11 or newer. Red owns its `discord.py` version;
  do not install a separate Discord library for this cog.
- The cog requirements from `info.json`, including PyNaCl, davey (Discord DAVE),
  imageio-ffmpeg, yt-dlp with EJS, and Deno. Downloader installs these. Load or
  reload the cog after installation; it recovers voice dependencies that Discord
  missed before Red made Downloader's library directory visible.
- A host with compatible dependency wheels/binaries, permission to run FFmpeg
  and Deno, and outbound Discord voice and media access. No Lavalink host access
  is required. Some restricted hosting plans cannot run native voice.

FFmpeg is initially supplied by imageio-ffmpeg. An administrator may select a
local executable with `IMAGEIO_FFMPEG_EXE`; otherwise an explicitly repaired
cog-local runtime takes precedence over the packaged binary, Conda location,
and `PATH`. Playback never installs or updates binaries.

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

No server restart is needed. If doctor reports a dependency as installed but
unavailable, update/reinstall that dependency and reload the cog. A broken or
incompatible native library still needs a working wheel for the host; the cog
never enables voice by skipping crypto imports or encryption checks.
Both old and experimental cogs use the same commands, so they cannot be loaded
together. This cog also refuses to share voice ownership with Audio or another
cog; it never unloads other cogs for you.

Join a voice channel and try:

```text
[p]tplay <YouTube video URL>
[p]tplay <SoundCloud track/set or Bandcamp track/album URL>
[p]tidalsetup login
[p]tplay <TIDAL URL or song search>
[p]tqueue
```

The bot needs Connect and Speak permissions. Playback controls require you to be
in its voice channel. `tidalsetup doctor` is owner-only and checks local versions,
voice readiness, FFmpeg features, YouTube dependencies, cached TIDAL login state,
and voice ownership. It does not connect, fetch media, or revalidate credentials.

If voice connects but a track is skipped, run `tidalsetup doctor` immediately
after the failure, without reloading. `FFmpeg last failure` gives the most recent
startup failure across the cog's sessions (for example `http_403`, `tls_error`,
or `process_signal_11`) and its exit code. The same safe category is logged;
raw FFmpeg output, signed stream URLs, and request headers are never logged or
saved. Executable availability and encoder support alone do not prove that the
host can reach a media server or decode that particular source.

### Repair without server access

Bot owners can install a persistent FFmpeg/Deno pair entirely through Discord:

```text
[p]tidalsetup repair
```

Wait for the success message, then run:

```text
[p]reload TidalPlayerExp
[p]tidalsetup doctor
[p]tplay <YouTube or TIDAL URL>
```

This is intended for a crashing bundled FFmpeg (such as
`process_signal_11 (exit=-11)`) or a Deno executable that disappears after cog
updates. Red's pip target updates can replace Downloader's shared `bin` folder;
the repaired pair lives separately in this cog's persistent data folder and
survives code updates and reloads. No SSH, root access, server restart, or extra
`pipinstall` command is needed for these two binaries.

Repair uses pinned [BtbN FFmpeg 8.1 builds](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-08-31-13-27)
and [official Deno 2.9.6 builds](https://github.com/denoland/deno/releases/tag/v2.9.6).
It verifies SHA-256 checksums before extraction or execution, checks FFmpeg Opus
encoding and Deno execution, and activates the pair only after validation.
Failed or cancelled repairs leave the previously active pair unchanged. Normal
playback and `doctor` never download, install, or change executable permissions.

Supported repair platforms are Linux x86-64/ARM64 with glibc 2.28 or newer and
Windows x86-64. Other platforms retain normal dependency discovery. Allow
outbound HTTPS to GitHub release downloads, permission to execute files in cog
data, and enough disk space for staging (roughly 150 MB of downloads on Linux,
190 MB on Windows; leave about 1 GB free). A matching healthy runtime is reused.
Previous installed generations are retained so an in-use binary is not deleted.
An explicit `IMAGEIO_FFMPEG_EXE` setting still takes precedence and is not changed
by repair. Local validation does not guarantee the host can reach every media
provider; retry playback after reloading to verify the original problem.

To return to the original cog:

```text
[p]unload TidalPlayerExp
[p]load audio
[p]load TidalPlayer
```

The original cog's saved configuration remains separate. Do not log out merely
to switch cogs. Update this experiment with `[p]cog update TidalPlayerExp`, update
its requirements when needed, then `[p]reload TidalPlayerExp`. The extractor uses
the newest compatible installed yt-dlp, including Downloader's copy, without
reordering the bot's global import path. Doctor reports that selected version,
not an older shadowing global installation. yt-dlp must be 2026.8.19 or newer.

## Sources and controls

The player uses explicit provider adapters, not arbitrary website extraction.
YouTube Music links use the YouTube adapter. Spotify links are catalog imports,
not direct Spotify audio. Apple Music, Deezer, and Amazon Music are not yet
implemented: adding them requires a catalog-link adapter and playable matching,
not treating subscription streams or short previews as full-song audio.

Provider limitations are distinct from the native voice engine. For example,
[SoundCloud documents off-platform streaming restrictions](https://developers.soundcloud.com/docs/api/),
and [Apple distinguishes subscription playback from preview assets](https://developer.apple.com/documentation/applemusicapi/songs/attributes-data.dictionary).
The [yt-dlp supported-sites list](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
is not a guarantee that every site or individual track currently works.

- YouTube video links work without a YouTube API key or TIDAL login. With TIDAL
  authentication, the cog tries a confident catalog match; otherwise it plays
  the original video's audio. If the TIDAL source cannot start, it falls back to
  that same YouTube video. A fallback displays the YouTube title and link.
- TIDAL media links accept `/browse/`, a trailing `/u` share suffix, and ordinary
  trailing slashes. YouTube watch, `youtu.be`, mobile, Music, Shorts, Live and
  embed links are supported, including `youtube-nocookie.com` video embeds.
  Time/tracking parameters do not change which video is queued; they do not
  seek playback. Channel/profile pages and arbitrary redirect links are not media
  links, and unavailable or region-restricted content can still fail.
- YouTube duration comes from `contentDetails` in the existing metadata API
  request, or from the extractor without an API key. If flat playlist metadata
  lacks duration, it is filled in when audio is resolved. Unknown/live duration
  displays `Unknown`, not `00:00`; hour-long recordings use `h:mm:ss`.
- A video URL containing `list=` still plays that video only. Use an explicit
  YouTube `/playlist?list=...` URL to request playlist import. Keyless imports
  are capped at 100 items; the optional Data API path supports up to 1,000.
- Public SoundCloud tracks/sets and Bandcamp tracks/albums play their original
  audio without TIDAL login or additional API keys. Collections are capped at
  100 entries and skip malformed/unavailable metadata. Use full `soundcloud.com`
  or `artist.bandcamp.com` links; shortened SoundCloud links and custom Bandcamp
  domains are not supported. SoundCloud private **track** share links ending in
  `/s-...` are supported when SoundCloud grants access through the supplied token.
  The token stays in the in-memory playback reference, is omitted from logs and
  object representations, and is never included in queue/controller links.
  Your original Discord message still contains whatever link you submitted.
  Private playlists, premium-only, DRM-protected, and identified preview formats
  remain unsupported; the cog cannot recover audio a provider withholds.
  Flat collections can have sparse metadata: SoundCloud set titles may use
  track URL slugs, and entries without public track URLs are skipped.
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
one serial playback worker and one active FFmpeg process; YouTube, SoundCloud,
and Bandcamp extraction share at most two isolated child processes across the cog.
No songs are downloaded or cached to disk: yt-dlp runs with downloads and caching
disabled, and FFmpeg outputs audio through a pipe to Discord. There is no song
download folder to periodically clear. FFmpeg's small diagnostic temporary file
is automatically deleted; repair archives are also removed after installation or
failure. This avoids Java/Lavalink overhead but still uses CPU for audio encoding,
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

Private YouTube, age/login-restricted, removed, region-blocked, or rate-limited videos
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

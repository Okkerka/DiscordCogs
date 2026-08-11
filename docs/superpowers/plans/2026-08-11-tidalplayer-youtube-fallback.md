# TidalPlayer YouTube fallback implementation plan

> **For Codex:** Implement each task test-first. Run the named focused test and confirm it fails for the intended reason before changing production code. Keep Spotify, Tidal album/mix handling, autoplay, signed-stream loading, and node recovery out of scope.

**Goal:** Accept strict single-video YouTube URLs, prefer a confidently matching Tidal track, and queue the original YouTube video through LavaLink when no confident match exists.

**Architecture:** `providers/urls.py` remains the only URL parser. Pure matching stays in `domain/matching.py`; source-specific display data stays in `TrackMeta` and UI factories. `TidalPlayer` coordinates metadata lookup, bounded Tidal search, one optional direct LavaLink load, and one shared loaded-track admission path. Completed fire-and-forget tasks remove themselves from `_tasks`.

**Tech stack:** Python 3.11, asyncio, Red-DiscordBot, Red-LavaLink, tidalapi, google-api-python-client, discord.py, pytest, pytest-asyncio, Ruff.

---

## Task 1: Make provider URL parsing canonical

**Files:**

- Modify: `TidalPlayer/providers/urls.py`
- Modify: `TidalPlayer/domain/normalization.py`
- Modify: `TidalPlayer/tidalplayer.py`
- Modify: `TidalPlayer/tests/test_url_parsing.py`

### Step 1: Add failing parser regressions

Add parameterized tests for:

- `youtube.com/watch?v=`, `m.youtube.com`, and `music.youtube.com`;
- `youtu.be/<id>`;
- `/shorts/<id>`, `/live/<id>`, and `/embed/<id>`;
- one surrounding Discord `<...>` pair and whitespace;
- playlist precedence when both `v` and `list` exist;
- malformed video IDs, HTTP, credentials, unsupported hosts, and unsupported paths.

Assert that a video returns `ProviderURL(ProviderKind.YOUTUBE, "video", video_id)` and a playlist returns `content_type == "playlist"`.

Run: `python -m pytest TidalPlayer/tests/test_url_parsing.py -q`

Expected: FAIL because plain video URLs are rejected and wrapped URLs are not normalized.

### Step 2: Implement strict parsing

In `providers/urls.py`:

- strip whitespace and exactly one surrounding `<...>` pair before `urlsplit`;
- accept only the documented YouTube hosts and HTTPS without credentials;
- validate video IDs with one compiled full-match expression for `[A-Za-z0-9_-]{11}`;
- keep a non-empty `list` query parameter as playlist precedence;
- extract IDs from watch queries, `youtu.be` paths, and the `shorts`, `live`, and `embed` paths;
- reject extra path segments and malformed IDs.

Remove the obsolete `YOUTUBE_PLAYLIST_PATTERN` export and import. Change `_handle_youtube_playlist` to accept the parsed playlist identifier directly. Dispatch the parsed identifier from `tplay` so handlers do not parse URLs again.

### Step 3: Verify and commit

Run: `python -m pytest TidalPlayer/tests/test_url_parsing.py TidalPlayer/tests/test_performance_reliability.py -q`

Expected: PASS.

Commit: `Support strict YouTube video URLs`

---

## Task 2: Add conservative YouTube-to-Tidal matching

**Files:**

- Modify: `TidalPlayer/domain/matching.py`
- Modify: `TidalPlayer/tests/test_playback_resilience.py`

### Step 1: Add failing pure matching tests

Cover:

- exact title and artist/channel match;
- title containing official-audio decorations;
- artist present in the title when the channel uses a label name;
- rejection of covers, remixes, live variants, partial title tokens, and artist mismatches;
- empty or malformed candidate metadata.

Run: `python -m pytest TidalPlayer/tests/test_playback_resilience.py -k youtube_tidal_match -q`

Expected: FAIL because the matching helper does not exist.

### Step 2: Implement the pure matcher

Add `select_confident_youtube_tidal_track(video_title, channel, tracks)` in `domain/matching.py`. Reuse `normalize_identity_text` and the existing bracket-decoration cleanup. Use token-boundary phrase checks, not a best-score fallback. Require both:

- the normalized Tidal title as a complete phrase in the cleaned video title; and
- the normalized Tidal artist as a complete phrase in the cleaned channel or video title.

Return the first confident candidate or `None`. Do not add provider I/O or mutable state to the domain module.

### Step 3: Verify and commit

Run: `python -m pytest TidalPlayer/tests/test_playback_resilience.py -k "youtube_tidal_match or select_best_tidal" -q`

Expected: PASS.

Commit: `Add conservative YouTube Tidal matching`

---

## Task 3: Make playback UI source-aware

**Files:**

- Modify: `TidalPlayer/domain/models.py`
- Modify: `TidalPlayer/ui/embeds.py`
- Modify: `TidalPlayer/ui/controller.py`
- Modify: `TidalPlayer/tests/test_embed_rendering.py`

### Step 1: Add failing UI regressions

Add a YouTube `TrackMeta` fixture and assert:

- now-playing title is `Playing from YouTube`;
- queue and now-playing link field is `Open in YouTube`;
- controller text contains the same source and link labels;
- Tidal fixtures retain their exact existing titles and labels.

Run: `python -m pytest TidalPlayer/tests/test_embed_rendering.py -q`

Expected: FAIL because the UI hardcodes Tidal.

### Step 2: Implement optional source metadata

Make `TrackMeta` non-total only for an optional `source` key by using `NotRequired[str]`; keep all existing fields required. Add small UI helpers that default missing source to `Tidal` and derive the provider link label. Do not duplicate entire embed or controller layouts.

### Step 3: Verify and commit

Run: `python -m pytest TidalPlayer/tests/test_embed_rendering.py -q`

Expected: PASS with unchanged Tidal characterization tests.

Commit: `Render TidalPlayer sources correctly`

---

## Task 4: Implement Tidal-first single-video playback with one fallback load

**Files:**

- Modify: `TidalPlayer/tidalplayer.py`
- Add: `TidalPlayer/tests/test_youtube_fallback.py`

### Step 1: Add failing metadata and fallback tests

Build isolated async tests with fake context, player, YouTube client, and Tidal tracks. Cover:

- API metadata produces a confident Tidal match and never calls `player.load_tracks` with YouTube;
- no confident match loads the canonical `https://www.youtube.com/watch?v=<id>` once;
- missing YouTube client, API exception, malformed response, or private metadata loads YouTube once, uses the loaded title/author for the Tidal match, and reuses the loaded track for fallback;
- loaded duration converts from milliseconds to seconds and thumbnail/URI populate `TrackMeta`;
- empty/failed LavaLink results send a YouTube-specific playback error;
- cancellation from API lookup, Tidal search, or LavaLink propagates without an error message;
- logs contain operation, guild, video ID, result type, and timing but not API keys or raw URLs.

Run: `python -m pytest TidalPlayer/tests/test_youtube_fallback.py -q`

Expected: FAIL because no single-video handler exists.

### Step 2: Extract shared loaded-track admission

Move only the post-load mutation block from `_load_and_queue_track()` into `_admit_loaded_track(ctx, player, loaded_track, meta, *, show_embed=True)`. Preserve:

- title and author override for Tidal metadata;
- idle/queue detection under the existing guild lock;
- rollback when `player.play()` fails;
- `_current_meta` and `_queued_meta` order;
- controller and queue confirmation behavior.

Call this helper from the existing Tidal path. Do not hold the guild lock during provider or LavaLink I/O and do not change batch admission in this task.

Run: `python -m pytest TidalPlayer/tests/test_playback_resilience.py -q`

Expected: PASS with no behavior change.

### Step 3: Add one-shot YouTube loading and metadata conversion

Add private helpers in `TidalPlayer` to:

- call `videos.list(part="snippet", id=video_id, maxResults=1)` through the existing bounded blocking runner;
- validate title, channel title, and thumbnails without assuming response shapes;
- call `player.load_tracks(canonical_url)` once when needed and extract the first track using the existing result-shape helper;
- convert LavaLink title, author, `length`, `thumbnail`, and `uri` into source-aware `TrackMeta` without logging the URL.

The direct load must rely on LavaLink's REST timeout, propagate cancellation, and perform no fresh-URL or retry loop.

### Step 4: Add the single-video coordinator

Implement `_handle_youtube_video(ctx, video_id)`:

1. validate voice/player readiness using the same path as Tidal playback;
2. fetch API metadata when available;
3. if metadata is unavailable, load YouTube once and derive title/author from that track;
4. search Tidal with the available title and channel/author;
5. queue a confident Tidal match through `_load_and_queue_track`;
6. otherwise load YouTube if not already retained and pass it to `_admit_loaded_track`.

Do not require a configured YouTube API key for direct fallback. Playlist imports keep their existing configuration requirement. Add a YouTube-specific failure message so a direct source failure does not claim the Tidal stream failed.

### Step 5: Verify and commit

Run: `python -m pytest TidalPlayer/tests/test_youtube_fallback.py TidalPlayer/tests/test_playback_resilience.py TidalPlayer/tests/test_commands.py -q`

Expected: PASS.

Commit: `Add Tidal first YouTube fallback playback`

---

## Task 5: Bound the generic task registry

**Files:**

- Modify: `TidalPlayer/tidalplayer.py`
- Modify: `TidalPlayer/tests/test_performance_reliability.py`

### Step 1: Add a failing task-lifecycle regression

Create a completed coroutine through the new registration boundary and assert that after one event-loop turn its task is absent from `_tasks`. Also assert that a pending registered task remains cancelable by `cog_unload()`.

Run: `python -m pytest TidalPlayer/tests/test_performance_reliability.py -k task_registry -q`

Expected: FAIL because no shared registration helper exists and queue deletion tasks remain stored.

### Step 2: Implement and adopt the helper

Add `_create_task(coro, *, name=None)` that calls `asyncio.create_task`, adds the task to `_tasks`, and attaches `self._tasks.discard` as a done callback. Use it for:

- queued-message deletion;
- autoplay queue confirmations;
- recommendation queue confirmations;
- Last.fm session closure.

Do not route guild-owned autoplay/recommendation tasks or coalesced LavaLink tasks through this helper because their keyed registries already own lifecycle cleanup.

### Step 3: Verify and commit

Run: `python -m pytest TidalPlayer/tests/test_performance_reliability.py -q`

Expected: PASS.

Commit: `Prune completed TidalPlayer tasks`

---

## Task 6: Full verification and scope audit

**Files:**

- Verify only unless a failing test exposes an in-scope regression.

### Step 1: Run static and syntax checks

Run:

- `python -m ruff check TidalPlayer`
- `python -m compileall -q TidalPlayer`
- `git diff --check`

Expected: all commands exit 0.

### Step 2: Run the complete suite

Run: `python -m pytest TidalPlayer/tests -q`

Expected: all tests pass with no pending-task or unclosed-session warnings.

### Step 3: Review the final diff

Confirm:

- no signed stream URL, YouTube API key, query parameters, or raw provider exception text is logged;
- every changed behavior has a regression test that failed first;
- Spotify and Tidal album/mix code is behaviorally unchanged;
- no provider or LavaLink I/O occurs while `_guild_locks[guild_id]` is held;
- direct YouTube fallback performs at most one LavaLink load per command;
- Tidal playback output remains unchanged.

### Step 4: Integrate

Commit any test-only cleanup needed for the final gate. Merge or fast-forward the isolated branch into `main`, rerun the complete suite on `main`, and push only after the local and remote branch state is confirmed.

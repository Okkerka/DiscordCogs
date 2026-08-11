# TidalPlayer Low-Risk Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct isolated TidalPlayer correctness, validation, logging, interaction, and cleanup defects without changing playback admission, LavaLink ownership, Audio lifecycle reconciliation, task teardown, or Spotify behavior.

**Architecture:** Keep the current cog and provider boundaries. Put pure normalization in `domain`, provider validation in `providers`, Discord rendering in `ui`, and make only narrow call-site changes in `tidalplayer.py`. Every behavior change receives a focused regression test before its implementation.

**Tech Stack:** Python 3.11+, Red-DiscordBot 3.5.24+, discord.py 2.x, tidalapi 0.8.x, pytest, pytest-asyncio, RapidFuzz, Ruff.

## Global Constraints

- Modify only `TidalPlayer` and the approved design/plan documents.
- Keep Spotify behavior, imports, commands, credentials, and dependencies unchanged.
- Preserve all public commands and persisted Config keys.
- Do not change LavaLink loading, node readiness, signed-URL resolution, controller transitions, autoplay admission, or task teardown in this stage.
- Do not log API keys, OAuth tokens, signed URLs, cookies, authorization headers, query strings, or raw provider exception text.
- Commit each independently tested task locally. Push to `origin/main` only after the complete TidalPlayer suite, Ruff, compilation, and diff review pass.

## File map

- `TidalPlayer/domain/normalization.py`: shared Unicode text and recording-signature normalization.
- `TidalPlayer/domain/matching.py`: Unicode-aware catalog scoring.
- `TidalPlayer/providers/urls.py`: strict provider URL identifiers.
- `TidalPlayer/providers/tokens.py`: synchronized OAuth snapshots.
- `TidalPlayer/ui/controller.py`: Components V2 duration rendering.
- `TidalPlayer/tidalplayer.py`: cache keys, interactions, YouTube guards, logging, queue display, and redundant yields.
- `TidalPlayer/info.json`: accurate package description and data statement.
- `TidalPlayer/tests/`: focused behavior and regression coverage.

---

### Task 1: Unicode identities and cache coalescing

**Files:**
- Modify: `TidalPlayer/domain/normalization.py`
- Modify: `TidalPlayer/domain/matching.py`
- Modify: `TidalPlayer/tidalplayer.py`
- Test: `TidalPlayer/tests/test_performance_reliability.py`
- Test: `TidalPlayer/tests/test_commands.py`

**Interfaces:**
- Produces: `normalize_identity_text(value: Any) -> str`
- Produces: `recording_signature(title: Any, artist: Any) -> str`
- Consumes: existing `TidalHandler.search(query, filter_remixes=False)`

- [ ] **Step 1: Add failing Unicode and cache-key tests**

```python
def test_non_latin_catalog_match_is_not_discarded():
    track = SimpleNamespace(name="夜に駆ける", full_name=None, artist=SimpleNamespace(name="YOASOBI"))
    assert select_best_tidal_track("YOASOBI 夜に駆ける", [track]) is track


@pytest.mark.asyncio
async def test_equivalent_search_queries_share_cache_entry(cog):
    cog.tidal.session.search = MagicMock(return_value={"tracks": []})
    await cog.tidal.search("  Artist   Track  ")
    await cog.tidal.search("artist track")
    assert cog.tidal.session.search.call_count == 1
    assert cog.tidal.session.search.call_args.args[0] == "  Artist   Track  "
```

- [ ] **Step 2: Run the tests and confirm the original behavior fails**

Run: `python -m pytest TidalPlayer/tests/test_performance_reliability.py TidalPlayer/tests/test_commands.py -q`

Expected: the non-Latin match returns `None`, and equivalent queries call Tidal twice.

- [ ] **Step 3: Add shared Unicode normalization and use it only for identities/cache keys**

```python
def normalize_identity_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join("".join(char if char.isalnum() else " " for char in normalized).split())


def recording_signature(title: Any, artist: Any) -> str:
    normalized_title = normalize_identity_text(title)
    normalized_artist = normalize_identity_text(artist)
    return f"{normalized_artist}\0{normalized_title}" if normalized_title else ""
```

Use `normalize_identity_text(query)` in the search cache/coalescing key while sending the original `query` to tidalapi. Route `_track_signature()` and catalog matching through these helpers.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest TidalPlayer/tests/test_performance_reliability.py TidalPlayer/tests/test_commands.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
Normalize Tidal search and recording identities
```

---

### Task 2: URL and token snapshot boundaries

**Files:**
- Modify: `TidalPlayer/providers/urls.py`
- Modify: `TidalPlayer/providers/tokens.py`
- Test: `TidalPlayer/tests/test_url_parsing.py`
- Test: `TidalPlayer/tests/test_tokens.py`

**Interfaces:**
- Preserves: `parse_provider_url(value: str) -> ProviderURL | None`
- Preserves: `TokenRepository.load() -> TokenSnapshot | None`

- [ ] **Step 1: Add failing validation and synchronization tests**

```python
class _BlockingField(_FakeField):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    async def set(self, value: Any) -> None:
        self.started.set()
        await self.release.wait()
        self._value = value


@pytest.mark.parametrize("url", [
    "https://tidal.com/track/not-a-number",
    "https://tidal.com/album/12x",
    "https://tidal.com/video/-1",
])
def test_tidal_numeric_media_rejects_non_numeric_ids(url):
    with pytest.raises(MalformedProviderURL):
        parse_provider_url(url)


@pytest.mark.asyncio
async def test_load_waits_for_complete_token_replacement():
    started = asyncio.Event()
    release = asyncio.Event()
    config = _FakeConfigGroup()
    config.token_type = _BlockingField(started, release)
    repository = TokenRepository(config)
    snapshot = TokenSnapshot(**_COMPLETE)
    replace_task = asyncio.create_task(repository.replace(snapshot))
    await started.wait()
    load_task = asyncio.create_task(repository.load())
    assert not load_task.done()
    release.set()
    await replace_task
    assert await load_task == snapshot
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest TidalPlayer/tests/test_url_parsing.py TidalPlayer/tests/test_tokens.py -q`

Expected: malformed numeric IDs parse successfully, and `load()` can complete during replacement.

- [ ] **Step 3: Validate media IDs and synchronize reads**

```python
if path[0] in {"track", "album", "video"} and not path[1].isdigit():
    raise MalformedProviderURL("Tidal media identifiers must be numeric")
```

Wrap `TokenRepository.load()` in `async with self._lock:` and keep the existing complete-snapshot validation.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest TidalPlayer/tests/test_url_parsing.py TidalPlayer/tests/test_tokens.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
Harden Tidal URL and token boundaries
```

---

### Task 3: Controller formatting and timely acknowledgements

**Files:**
- Modify: `TidalPlayer/ui/controller.py`
- Modify: `TidalPlayer/tidalplayer.py`
- Test: `TidalPlayer/tests/test_commands.py`

**Interfaces:**
- Consumes: `domain.normalization.format_duration(seconds: int) -> str`
- Preserves: controller callback method names and user-facing responses.

- [ ] **Step 1: Add failing tests for long durations and response ordering**

```python
def test_controller_uses_shared_hour_aware_duration(cog):
    meta = {
        "title": "Track", "artist": "Artist", "album": None,
        "duration": 3661, "quality": "HIGH", "image": None,
        "share_url": None, "audio_resolution": None, "track_id": 1,
    }
    with patch("TidalPlayer.ui.controller.format_duration", return_value="1:01:01") as formatter:
        PlayerControllerView(cog, meta=meta)
    formatter.assert_called_once_with(3661)


@pytest.mark.asyncio
async def test_controller_stop_defers_before_player_io(cog):
    events = []
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=10),
        response=SimpleNamespace(defer=AsyncMock(side_effect=lambda: events.append("defer"))),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    player = SimpleNamespace(
        queue=[],
        stop=MagicMock(side_effect=lambda: events.append("stop")),
    )
    cog._get_player_for_guild = AsyncMock(return_value=player)
    await cog.controller_stop(interaction)
    assert events[:2] == ["defer", "stop"]


@pytest.mark.asyncio
async def test_controller_pause_defers_before_player_lookup(cog):
    events = []
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=11),
        response=SimpleNamespace(defer=AsyncMock(side_effect=lambda: events.append("defer"))),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    async def get_player(_guild_id):
        events.append("player")
        return None

    cog._get_player_for_guild = get_player
    await cog.controller_toggle_pause(interaction)
    assert events == ["defer", "player"]


@pytest.mark.asyncio
async def test_controller_autoplay_defers_before_config_read(cog):
    events = []

    class Setting:
        async def __call__(self):
            events.append("config")
            return False

        async def set(self, _value):
            events.append("set")

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=12),
        response=SimpleNamespace(defer=AsyncMock(side_effect=lambda: events.append("defer"))),
    )
    cog.can_change_guild_settings = AsyncMock(return_value=True)
    cog.config.guild = MagicMock(return_value=SimpleNamespace(autoplay_enabled=Setting()))
    cog._refresh_controller = AsyncMock()
    await cog.controller_toggle_autoplay(interaction)
    assert events[:2] == ["defer", "config"]
```

Retain the existing permission-denied path and assert the no-player pause path uses one ephemeral followup after deferral.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python -m pytest TidalPlayer/tests/test_commands.py -q`

Expected: duration renders as `61:01`, and stop performs player I/O before deferring.

- [ ] **Step 3: Reuse the duration helper and defer before slow work**

```python
from ..domain.normalization import format_duration

duration = format_duration(int(self.meta.get("duration") or 0))
```

For valid controller interactions, call `interaction.response.defer()` before Config/player/Discord I/O. Use followups for errors or confirmations after deferral. Do not alter guild locks or controller state transitions in this stage.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest TidalPlayer/tests/test_commands.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
Acknowledge Tidal controls before IO
```

---

### Task 4: YouTube response validation

**Files:**
- Modify: `TidalPlayer/tidalplayer.py`
- Test: `TidalPlayer/tests/test_performance_reliability.py`

**Interfaces:**
- Preserves: `_fetch_all_youtube_tracks(playlist_id: str) -> list[Any]`
- Preserves: `_handle_youtube_playlist(ctx, url) -> None`

- [ ] **Step 1: Add failing playlist-metadata tests**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"items": []}, {"items": "invalid"}, {"items": [None]}])
async def test_youtube_playlist_metadata_rejects_malformed_payload(cog, payload):
    request = SimpleNamespace(execute=lambda: payload)
    playlists = SimpleNamespace(list=lambda **_kwargs: request)
    cog.yt = SimpleNamespace(playlists=lambda: playlists)
    ctx = SimpleNamespace(send=AsyncMock())
    cog._process_track_list = AsyncMock()

    async def run_blocking(_handler, operation, **_kwargs):
        return operation()

    with patch.object(type(cog.tidal), "_run_blocking", new=run_blocking):
        await cog._handle_youtube_playlist(ctx, "https://youtube.com/playlist?list=PL123")
    assert ctx.send.await_args.kwargs["embed"].description == Messages.ERROR_FETCH_FAILED
    cog._process_track_list.assert_not_awaited()
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest TidalPlayer/tests/test_performance_reliability.py -q`

Expected: at least one malformed payload raises or reaches `_process_track_list`.

- [ ] **Step 3: Validate the response before indexing**

```python
items = pl_resp.get("items") if isinstance(pl_resp, dict) else None
if not isinstance(items, list) or not items or not isinstance(items[0], dict):
    raise ValueError("YouTube playlist metadata is unavailable")
snippet = items[0].get("snippet")
if not isinstance(snippet, dict):
    raise ValueError("YouTube playlist metadata is malformed")
```

Keep the existing repeated-page-token and malformed-page guards. Do not replace the Google API client in Stage 1.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest TidalPlayer/tests/test_performance_reliability.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
Validate YouTube playlist metadata
```

---

### Task 5: Credential-safe provider logging

**Files:**
- Modify: `TidalPlayer/tidalplayer.py`
- Test: `TidalPlayer/tests/test_performance_reliability.py`
- Test: `TidalPlayer/tests/test_playback_resilience.py`

**Interfaces:**
- Produces: private `_log_provider_failure(provider: str, operation: str, error: BaseException) -> None`.
- Preserves: existing empty-result/error responses.

- [ ] **Step 1: Add failing secret-redaction tests**

```python
@pytest.mark.asyncio
async def test_lastfm_error_log_does_not_contain_api_key(cog, caplog):
    class FailingRequest:
        async def __aenter__(self):
            raise aiohttp.ClientError(f"https://example/?api_key={secret}")

        async def __aexit__(self, *_args):
            return None

    secret = "lastfm-secret-value"
    cog.bot.get_shared_api_tokens = AsyncMock(return_value={"api_key": secret})
    cog._lastfm_session = SimpleNamespace(
        closed=False,
        get=MagicMock(return_value=FailingRequest()),
    )
    assert await cog._lastfm_similar_tracks("artist", "title") == []
    assert secret not in caplog.text
    assert "https://" not in caplog.text
    assert "ClientError" in caplog.text


def test_provider_failure_logger_never_formats_exception_text(caplog):
    module = importlib.import_module("TidalPlayer.tidalplayer")
    secret = "signed-secret"
    module._log_provider_failure(
        "Tidal",
        "stream resolution",
        RuntimeError(f"https://stream.example/file?token={secret}"),
    )
    assert secret not in caplog.text
    assert "https://" not in caplog.text
    assert "RuntimeError" in caplog.text
```

- [ ] **Step 2: Run and confirm secret exposure**

Run: `python -m pytest TidalPlayer/tests/test_performance_reliability.py TidalPlayer/tests/test_playback_resilience.py -q`

Expected: captured logs contain the injected secret or URL.

- [ ] **Step 3: Pass Last.fm query parameters separately and sanitize provider logs**

```python
params = {
    "method": "track.getsimilar",
    "artist": artist,
    "track": title,
    "limit": limit,
    "autocorrect": 1,
    "api_key": api_key,
    "format": "json",
}
async with session.get("https://ws.audioscrobbler.com/2.0/", params=params) as response:
    response.raise_for_status()
    payload = await response.json(content_type=None)

log.warning(
    "%s %s failed (%s)",
    provider,
    operation,
    type(error).__name__,
)
```

Route Last.fm, Tidal SDK/stream-resolution, Spotify SDK, and YouTube SDK failures through `_log_provider_failure`. Preserve tracebacks for internal programming failures where no provider URL or credential can be present.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest TidalPlayer/tests/test_performance_reliability.py TidalPlayer/tests/test_playback_resilience.py -q`

Expected: PASS and no injected secret appears in captured logs.

- [ ] **Step 5: Commit**

```text
Redact provider failures from Tidal logs
```

---

### Task 6: Queue totals, redundant yields, and proven dead helpers

**Files:**
- Modify: `TidalPlayer/tidalplayer.py`
- Modify: `TidalPlayer/tests/test_embed_rendering.py`
- Delete: `TidalPlayer/tests/test_extracted_embed_equivalence.py`
- Test: `TidalPlayer/tests/test_commands.py`

**Interfaces:**
- Preserves: `tqueue(ctx)` command behavior and page size.
- Tests `ui.embeds.make_now_playing_embed()` directly after private wrapper removal.

- [ ] **Step 1: Add a failing truncated-queue title test**

```python
@pytest.mark.asyncio
async def test_queue_title_reports_displayed_and_total_tracks(cog):
    queue = [SimpleNamespace(title=f"Track {i}", author="Artist") for i in range(MAX_ITEMS + 7)]
    ctx = SimpleNamespace(send=AsyncMock())
    cog._get_player = AsyncMock(return_value=SimpleNamespace(queue=queue))
    cog.check_ready = AsyncMock(return_value=True)
    menu = SimpleNamespace(start=AsyncMock())
    module = importlib.import_module(cog.__class__.__module__)
    with patch.object(module, "SimpleMenu", return_value=menu) as menu_factory:
        await cog.tqueue(ctx)
    first_page = menu_factory.call_args.args[0][0]
    assert first_page.title == f"Queue (first {MAX_ITEMS} of {MAX_ITEMS + 7} tracks)"
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest TidalPlayer/tests/test_commands.py -q`

Expected: title reports only `MAX_ITEMS`.

- [ ] **Step 3: Report the real total and remove only search-proven dead work**

Compute `total_count = len(queue)` before slicing and choose `Queue (N tracks)` or `Queue (first N of M tracks)`.

Remove the four `await asyncio.sleep(0)` calls that immediately follow awaited provider/executor operations. Use `rg` to confirm private helpers have no production caller, then remove `_has_playback`, `_format_track_embed`, `_controller_fallback_text`, `_replace_controller_message`, `_controller_embed`, `_autoplay_candidate`, and `_handle_tidal_url`. Move embed tests from `_build_now_playing_embed` to `make_now_playing_embed`, then remove `_build_now_playing_embed` and its duplicate equivalence test. Do not remove the audio-resolution path or any playback method in Stage 1.

- [ ] **Step 4: Run command and embed tests**

Run: `python -m pytest TidalPlayer/tests/test_commands.py TidalPlayer/tests/test_embed_rendering.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
Remove unused TidalPlayer helpers
```

---

### Task 7: Package metadata accuracy

**Files:**
- Modify: `TidalPlayer/info.json`
- Test: `TidalPlayer/tests/test_smoke.py`

**Interfaces:**
- Adds: `end_user_data_statement` package field.
- Preserves: Tidal, Spotify, YouTube, and RapidFuzz requirements.

- [ ] **Step 1: Add failing metadata tests**

```python
def test_info_declares_data_statement_and_does_not_claim_unconfigured_hires():
    info = json.loads((PACKAGE_ROOT / "info.json").read_text(encoding="utf-8"))
    assert "end_user_data_statement" in info
    description = f"{info['short']} {info['description']}".casefold()
    assert "hi-res" not in description
    assert "lossless" not in description
    assert "spotipy" in info["requirements"]
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest TidalPlayer/tests/test_smoke.py -q`

Expected: missing data statement and Hi-Res/lossless claims fail.

- [ ] **Step 3: Correct metadata without changing stream configuration**

Set `end_user_data_statement` to the existing cog statement. Describe Tidal playback and metadata without promising Hi-Res or lossless output. Keep Spotify in `install_msg`, description, and requirements.

- [ ] **Step 4: Run smoke tests**

Run: `python -m pytest TidalPlayer/tests/test_smoke.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
Correct TidalPlayer package metadata
```

---

### Task 8: Full verification and direct main push

**Files:**
- Verify every modified Stage 1 file.
- Do not modify unrelated cogs or Stage 2 code while resolving checks.

**Interfaces:**
- Produces: a tested `origin/main` containing all Stage 1 commits.

- [ ] **Step 1: Install the declared test dependency only if the active test environment lacks it**

Run: `python -c "import rapidfuzz"`

If missing, run: `python -m pip install "rapidfuzz>=3.0"`

- [ ] **Step 2: Run the complete TidalPlayer suite**

Run: `python -m pytest TidalPlayer/tests -q`

Expected: all tests pass with no warnings caused by modified code.

- [ ] **Step 3: Run lint and compilation**

Run: `python -m ruff check TidalPlayer`

Run: `python -m compileall -q TidalPlayer`

Expected: both commands exit 0.

- [ ] **Step 4: Review the final diff and repository state**

Run: `git status -sb`

Run: `git diff origin/main...HEAD -- TidalPlayer docs/superpowers`

Run: `git diff --check origin/main...HEAD`

Confirm that Spotify code and dependencies remain, no Stage 2 changes appear, no credential-like values were introduced, and no unrelated file is modified.

- [ ] **Step 5: Push without force**

Run: `git push origin main`

Expected: `origin/main` advances to the verified local `main` head. If rejected because the remote moved, fetch and inspect the remote changes; do not force-push.

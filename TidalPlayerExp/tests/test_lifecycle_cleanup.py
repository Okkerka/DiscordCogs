import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _raise_async(error: Exception):
    raise error


@pytest.mark.asyncio
async def test_cog_unload_awaits_tasks_closes_session_and_stops_views(cog) -> None:
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(worker())
    cog._tasks.add(task)
    await started.wait()
    session = SimpleNamespace(closed=False, close=AsyncMock())
    cog._lastfm_session = session
    persistent = MagicMock()
    controller = MagicMock()
    cog._persistent_view = persistent
    cog._controller_views[42] = controller
    with patch.object(type(cog.tidal), "unload", new=AsyncMock()) as unload:
        await cog.cog_unload()

    assert cancelled.is_set()
    session.close.assert_awaited_once()
    unload.assert_awaited_once()
    persistent.stop.assert_called_once()
    controller.stop.assert_called_once()


@pytest.mark.asyncio
async def test_activate_controller_stops_old_view_and_records_only_success(cog) -> None:
    old = MagicMock()
    replacement = MagicMock()
    cog._controller_views[7] = old
    send = AsyncMock(return_value=SimpleNamespace(id=99))

    message = await cog._activate_controller_view(
        7, replacement, lambda view: send(view=view)
    )

    old.stop.assert_called_once()
    assert cog._controller_views[7] is replacement
    assert message.id == 99

    failed = MagicMock()
    with pytest.raises(RuntimeError):
        await cog._activate_controller_view(
            7, failed, lambda _view: _raise_async(RuntimeError("send failed"))
        )
    failed.stop.assert_called_once()
    assert 7 not in cog._controller_views


@pytest.mark.asyncio
async def test_tidal_handler_unload_drains_refresh_and_inflight_tasks(cog) -> None:
    cancelled = [asyncio.Event(), asyncio.Event()]

    async def worker(done: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            done.set()

    refresh = asyncio.create_task(worker(cancelled[0]))
    inflight = asyncio.create_task(worker(cancelled[1]))
    await asyncio.sleep(0)
    cog.tidal._refresh_task = refresh
    cog.tidal._inflight[("search", "query")] = inflight
    executor = MagicMock()
    cog.tidal._executor = executor

    await cog.tidal.unload()

    assert all(event.is_set() for event in cancelled)
    assert cog.tidal._refresh_task is None
    assert cog.tidal._inflight == {}
    executor.shutdown.assert_called_once_with(wait=False)


@pytest.mark.asyncio
async def test_own_disconnect_closes_guild_even_when_backend_hides_dead_session(cog, native_session):
    guild = SimpleNamespace(id=42)
    event = cog._claim_batch(guild.id)
    cog._current_meta[guild.id] = {"title": "Old"}
    view = MagicMock()
    cog._controller_views[guild.id] = view
    cog.backend.get.return_value = None
    member = SimpleNamespace(id=cog.bot.user.id, guild=guild)
    await cog.on_voice_state_update(member, SimpleNamespace(channel=object()), SimpleNamespace(channel=None))
    cog.backend.close_guild.assert_awaited_once_with(guild.id)
    assert event.is_set()
    assert guild.id not in cog._current_meta
    view.stop.assert_called_once()


@pytest.mark.asyncio
async def test_audio_add_attempts_every_guild_after_cleanup_failure(cog, native_session, caplog):
    cog.bot.guilds = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    cog.backend.close_guild.side_effect = [RuntimeError("secret"), None]
    await cog.on_cog_add(SimpleNamespace(qualified_name="Audio"))
    assert [call.args[0] for call in cog.backend.close_guild.await_args_list] == [1, 2]
    assert "secret" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_unload_continues_resource_and_view_cleanup_after_backend_failure(cog, native_session, caplog):
    cog.backend.close.side_effect = RuntimeError("secret")
    view = MagicMock()
    cog._controller_views[1] = view
    cog.tidal = SimpleNamespace(unload=AsyncMock())
    await cog.cog_unload()
    cog.tidal.unload.assert_awaited_once()
    view.stop.assert_called_once()
    assert cog._closing
    assert not cog._controller_views
    assert "secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["Audio", "TidalPlayer"])
async def test_load_conflict_is_reported_without_mutating_other_cogs(cog, native_session, conflict):
    cog.bot.get_cog.side_effect = lambda name: object() if name == conflict else None
    cog.tidal = SimpleNamespace(unload=AsyncMock())
    with pytest.raises(Exception, match="Unload Audio and the original TidalPlayer"):
        await cog.cog_load()
    cog.backend.close.assert_awaited_once()
    cog.tidal.unload.assert_awaited_once()
    cog.bot.add_view.assert_not_called()


@pytest.mark.asyncio
async def test_failed_load_closes_resources_and_refresh_worker(cog, monkeypatch):
    """Red does not call cog_unload when cog_load itself raises."""
    import sys

    module = sys.modules[type(cog).__module__]
    monkeypatch.setattr(module, "initialize_voice_runtime", lambda: None)
    monkeypatch.setattr(module, "PlayerControllerView", MagicMock())
    cog.bot.wait_until_ready = asyncio.Event().wait
    cog._migrate_config = AsyncMock()
    cog.tokens.restore = AsyncMock(return_value=None)
    cog.bot.add_view.side_effect = RuntimeError("registration failed")

    with pytest.raises(RuntimeError, match="registration failed"):
        await cog.cog_load()

    try:
        assert cog._closing
        assert not cog._initialized
        assert cog.tidal._refresh_task is None
        assert cog._persistent_view is None
        assert cog.runtime._closed
    finally:
        # Also drain the deliberately reproduced leak on the failing version.
        await cog.cog_unload()


@pytest.mark.asyncio
async def test_stalled_load_times_out_with_stage_and_cleans_up(cog, monkeypatch, caplog):
    import sys

    module = sys.modules[type(cog).__module__]
    monkeypatch.setattr(module, "initialize_voice_runtime", lambda: None)
    monkeypatch.setattr(module, "COG_LOAD_TIMEOUT", 0.05, raising=False)
    cog._migrate_config = AsyncMock()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled_restore():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cog.tokens.restore = stalled_restore
    # The outer deadline is only a test safeguard, not the cog's implementation.
    with pytest.raises(Exception, match="startup timed out during provider initialization"):
        await asyncio.wait_for(cog.cog_load(), timeout=1)

    assert entered.is_set() and cancelled.is_set()
    assert cog._closing and not cog._initialized
    assert cog.tidal._refresh_task is None
    assert "provider initialization" in caplog.text
    cog.bot.add_view.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_load_cleans_up_and_preserves_cancellation(cog, monkeypatch):
    import sys

    module = sys.modules[type(cog).__module__]
    monkeypatch.setattr(module, "initialize_voice_runtime", lambda: None)
    entered = asyncio.Event()

    async def stalled_migration():
        entered.set()
        await asyncio.Event().wait()

    cog._migrate_config = stalled_migration
    loading = asyncio.create_task(cog.cog_load())
    await asyncio.wait_for(entered.wait(), timeout=1)
    loading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loading
    assert cog._closing
    assert cog.runtime._closed
    assert cog.tidal._refresh_task is None


@pytest.mark.asyncio
async def test_successful_load_starts_refresh_only_after_controller_registration(cog, monkeypatch):
    import sys

    module = sys.modules[type(cog).__module__]
    monkeypatch.setattr(module, "initialize_voice_runtime", lambda: None)
    monkeypatch.setattr(module, "PlayerControllerView", MagicMock())
    cog._migrate_config = AsyncMock()
    cog.tokens.restore = AsyncMock(return_value=None)
    cog.bot.wait_until_ready = asyncio.Event().wait

    def register(view):
        assert cog.tidal._refresh_task is None
        assert not cog._initialized

    cog.bot.add_view.side_effect = register
    try:
        await cog.cog_load()
        assert cog._initialized
        assert cog.tidal._refresh_task is not None
        assert not cog.tidal._refresh_task.done()
    finally:
        await cog.cog_unload()
    assert not cog._initialized
    assert cog.tidal._refresh_task is None


@pytest.mark.asyncio
async def test_registration_rejection_releases_constructed_cog(cog, monkeypatch):
    import sys

    module = sys.modules[type(cog).__module__]
    monkeypatch.setattr(module, "TidalPlayerExp", lambda bot: cog)
    cog.bot.add_cog.side_effect = RuntimeError("registration rejected")
    with pytest.raises(RuntimeError, match="registration rejected"):
        await module.setup(cog.bot)
    assert cog._closing
    assert cog.runtime._closed


@pytest.mark.asyncio
async def test_registration_does_not_repeat_framework_cleanup(cog, monkeypatch):
    import sys

    module = sys.modules[type(cog).__module__]
    monkeypatch.setattr(module, "TidalPlayerExp", lambda bot: cog)
    original_unload = cog.cog_unload
    cog.cog_unload = AsyncMock(wraps=original_unload)

    async def reject(instance):
        # discord.py already invokes unload for prefix-command conflicts.
        await instance.cog_unload()
        raise RuntimeError("duplicate command")

    cog.bot.add_cog.side_effect = reject
    with pytest.raises(RuntimeError, match="duplicate command"):
        await module.setup(cog.bot)
    cog.cog_unload.assert_awaited_once()


@pytest.mark.asyncio
async def test_registration_preserves_original_failure_when_cleanup_fails(cog, monkeypatch, caplog):
    import sys

    module = sys.modules[type(cog).__module__]
    monkeypatch.setattr(module, "TidalPlayerExp", lambda bot: cog)
    cog.bot.add_cog.side_effect = RuntimeError("registration rejected")
    original_unload = cog.cog_unload
    cog.cog_unload = AsyncMock(side_effect=OSError("private cleanup details"))
    try:
        with pytest.raises(RuntimeError, match="registration rejected"):
            await module.setup(cog.bot)
        assert "private cleanup details" not in caplog.text
        assert "OSError" in caplog.text
    finally:
        await original_unload()

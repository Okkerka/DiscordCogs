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

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from moderation.moderation import Moderation, mod_or_permissions


def test_slash_surface():
    commands = {c.name: c for c in Moderation.__cog_commands__}
    for name in (
        "kick",
        "ban",
        "purge",
        "cleanup",
        "dehoist",
        "nickname",
        "modhistory",
        "msgblock",
    ):
        assert name in commands
        assert commands[name].app_command is not None


def test_group_children_have_permission_checks():
    for group in (Moderation.purge, Moderation.dehoist, Moderation.nickname):
        for child in group.commands:
            assert child.checks, (
                f"{child.qualified_name} must enforce guild/moderator checks itself"
            )
    for group in (Moderation.mods, Moderation.modlog, Moderation.msgblock):
        for child in group.commands:
            assert child.requires.privilege_level == group.requires.privilege_level


@pytest.mark.asyncio
async def test_cleanup_never_passes_async_filter_to_discord():
    async def purge(**kwargs):
        assert not inspect.iscoroutinefunction(kwargs["check"])
        return []

    async def history(**kwargs):
        if False:
            yield None

    cog = object.__new__(Moderation)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=1), get_context=AsyncMock())
    cog._log_action = AsyncMock()
    channel = SimpleNamespace(purge=purge, history=history, mention="#test")
    ctx = SimpleNamespace(
        channel=channel,
        message=SimpleNamespace(id=100),
        author=SimpleNamespace(id=2),
        guild=object(),
        interaction=None,
        send=AsyncMock(),
    )
    await Moderation.cleanup.callback(cog, ctx)


@pytest.mark.asyncio
async def test_history_deferred_privately():
    cog = object.__new__(Moderation)
    ctx = SimpleNamespace(
        interaction=SimpleNamespace(response=SimpleNamespace(is_done=lambda: False)),
        command=SimpleNamespace(qualified_name="modhistory"),
        kwargs={},
        defer=AsyncMock(),
    )
    await cog.cog_before_invoke(ctx)
    assert ctx.defer.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_unban_failure_keeps_expiry_record():
    cog = object.__new__(Moderation)
    cog.bot = SimpleNamespace(fetch_user=AsyncMock(return_value=SimpleNamespace(id=5)))
    value = AsyncMock()
    cog.config = SimpleNamespace(
        guild=lambda guild: SimpleNamespace(tempbans=lambda: value)
    )
    cog._execute_mod_action = AsyncMock(return_value=False)
    ctx = SimpleNamespace(guild=SimpleNamespace(unban=AsyncMock()), author=object())
    await Moderation.unban.callback(cog, ctx, 5)
    value.__aenter__.assert_not_awaited()


@pytest.mark.asyncio
async def test_mod_action_rejects_equal_role_before_side_effects():
    cog = object.__new__(Moderation)
    target = SimpleNamespace(id=5, top_role=10)
    author = SimpleNamespace(id=6, top_role=10)
    ctx = SimpleNamespace(
        author=author,
        guild=SimpleNamespace(
            owner=object(),
            me=SimpleNamespace(top_role=20),
            get_member=lambda uid: target,
        ),
        send=AsyncMock(),
    )
    action = AsyncMock()
    assert not await cog._execute_mod_action(ctx, target, "Timeout Removed", action)
    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_moderator_still_needs_current_channel_permission():
    from redbot.core import commands

    author = SimpleNamespace(id=1)
    ctx = SimpleNamespace(
        author=author,
        guild=SimpleNamespace(id=2, owner=object()),
        bot=SimpleNamespace(
            is_owner=AsyncMock(return_value=False),
            get_cog=lambda name: SimpleNamespace(
                _get_cached_moderators=lambda gid: {1}
            ),
        ),
        channel=SimpleNamespace(
            permissions_for=lambda user: SimpleNamespace(manage_messages=False)
        ),
    )
    with pytest.raises(commands.MissingPermissions):
        await mod_or_permissions(manage_messages=True).predicate(ctx)


@pytest.mark.asyncio
async def test_ordinary_member_with_permission_is_not_custom_moderator():
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1),
        guild=SimpleNamespace(id=2, owner=object()),
        bot=SimpleNamespace(
            is_owner=AsyncMock(return_value=False),
            get_cog=lambda name: SimpleNamespace(
                _get_cached_moderators=lambda gid: set()
            ),
        ),
    )
    assert not await mod_or_permissions(manage_messages=True).predicate(ctx)


@pytest.mark.asyncio
async def test_purge_anchors_before_slash_invocation_and_rejects_human_bot_filter():
    cog = object.__new__(Moderation)
    cog._log_action = AsyncMock()
    channel = SimpleNamespace(purge=AsyncMock(return_value=[]), mention="#test")
    ctx = SimpleNamespace(
        channel=channel,
        message=SimpleNamespace(id=123),
        author=SimpleNamespace(id=1),
        guild=object(),
        interaction=object(),
        send=AsyncMock(),
    )
    await cog._purge_messages(ctx, 100, "bots", member=SimpleNamespace(bot=False))
    channel.purge.assert_not_awaited()
    await cog._purge_messages(ctx, 100, "bots")
    assert channel.purge.call_args.kwargs["before"].id == 123
    assert ctx.send.call_args.kwargs["ephemeral"]

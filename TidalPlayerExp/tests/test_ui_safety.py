"""Untrusted catalog text must remain bounded, inert Discord display content."""
from __future__ import annotations

import importlib.util
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord as real_discord
import pytest


@pytest.mark.asyncio
async def test_interactive_search_bounds_and_escapes_provider_metadata(cog, monkeypatch):
    import TidalPlayerExp.tidalplayer as module

    monkeypatch.setattr(module.TrackSelectView, "wait_for_selection", AsyncMock(return_value=None))
    message = SimpleNamespace(delete=AsyncMock())
    ctx = SimpleNamespace(author=SimpleNamespace(id=1), send=AsyncMock(return_value=message))
    hostile = "[click](https://evil.example) @everyone " + "😀" * 3000
    tracks = [SimpleNamespace(name=hostile, artist=SimpleNamespace(name=hostile),
                              album=SimpleNamespace(name=hostile), duration=120)] * 5
    await cog._interactive_select(ctx, tracks)
    payload = ctx.send.call_args.kwargs
    description = payload["embed"].description
    assert len(description.encode("utf-16-le")) // 2 <= 4096
    assert "@everyone" not in description and "[click](https://evil.example)" not in description
    assert payload["allowed_mentions"].everyone is False
    assert all(len(button.label.encode("utf-16-le")) // 2 <= 80 for button in payload["view"].children)


@pytest.mark.asyncio
async def test_interactive_search_cancellation_stops_view_and_deletes_prompt(cog, monkeypatch):
    import TidalPlayerExp.tidalplayer as module

    started = asyncio.Event()

    async def wait_for_selection(_view):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module.TrackSelectView, "wait_for_selection", wait_for_selection)
    message = SimpleNamespace(delete=AsyncMock())
    ctx = SimpleNamespace(author=SimpleNamespace(id=1), send=AsyncMock(return_value=message))
    pending = asyncio.create_task(cog._interactive_select(ctx, [SimpleNamespace(name="Song", duration=120)]))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        view = ctx.send.call_args.kwargs["view"]
        stopped = Mock(wraps=view.stop)
        monkeypatch.setattr(view, "stop", stopped)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        stopped.assert_called_once()
        message.delete.assert_awaited_once()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.fixture
def real_ui(cog, monkeypatch):
    # Keep the real serializer and limits; only the cog's external services are stubbed.
    monkeypatch.setitem(sys.modules, "discord", real_discord)
    monkeypatch.setitem(sys.modules, "discord.ui", real_discord.ui)
    modules = []
    for name in ("embeds", "controller"):
        path = Path(__file__).parents[1] / "ui" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"TidalPlayerExp.ui._test_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def metadata(**changes):
    meta = {"title": "Track", "artist": "Artist", "album": "Album", "duration": 178,
            "quality": "LOSSLESS", "source": "Tidal", "image": "https://example.com/art.jpg",
            "share_url": "https://listen.tidal.com/track/123"}
    meta.update(changes)
    return meta


@pytest.mark.parametrize("factory", ["make_now_playing_embed", "make_queue_embed"])
def test_embeds_escape_metadata_but_preserve_authored_markdown(real_ui, factory):
    embeds, _ = real_ui
    embed = getattr(embeds, factory)(metadata(
        title="[click](https://evil.example) @everyone", artist="**admin** <@123456789012345678>",
        album="_album_ @here"))
    assert "[click](https://evil.example)" not in embed.description
    assert "@everyone" not in embed.description
    assert "<@123456789012345678>" not in embed.description
    assert "@here" not in embed.description
    assert embed.description.startswith("**\\[click\\]")
    assert "\\*\\*admin\\*\\*" in embed.description
    assert "_\\_album\\_" in embed.description


@pytest.mark.parametrize("factory", ["make_now_playing_embed", "make_queue_embed"])
def test_embeds_bound_all_metadata_fields(real_ui, factory):
    embeds, _ = real_ui
    huge = "😀*" * 5000
    embed = getattr(embeds, factory)(metadata(title=huge, artist=huge, album=huge,
        audio_resolution=huge))
    assert len(embed.title) <= 256
    assert len(embed.description) <= 4096
    assert len(embed) <= 6000
    for field in embed.fields:
        assert len(field.name) <= 256
        assert len(field.value) <= 1024
    assert "02:58" in embed.footer.text


@pytest.mark.asyncio
async def test_controller_bounds_text_and_thumbnail_and_escapes_next_up(real_ui, cog):
    _, controller = real_ui
    huge = "😀*" * 5000
    view = controller.PlayerControllerView(cog, metadata(title=huge, artist=huge, album=huge,
        audio_resolution=huge), next_up=metadata(title="[click](https://evil.example) @everyone",
        artist="<@123456789012345678>"))
    texts = [item.content for item in view.walk_children() if isinstance(item, real_discord.ui.TextDisplay)]
    assert sum(len(text.encode("utf-16-le")) // 2 for text in texts) <= 4000
    assert all("@everyone" not in text and "<@123456789012345678>" not in text for text in texts)
    assert any("**Next up:** \\[click\\]" in text for text in texts)
    assert any("**Duration:** 02:58" in text for text in texts)
    thumbnails = [item for item in view.walk_children() if isinstance(item, real_discord.ui.Thumbnail)]
    assert thumbnails
    assert all(len(item.description.encode("utf-16-le")) // 2 <= 1024 for item in thumbnails)
    view.to_components()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://example.com/)\n@everyone",
    "https://example.com/" + "a" * 5000, "https://user:password@example.com/track"])
def test_unusable_share_links_are_omitted(real_ui, url):
    embeds, controller = real_ui
    meta = metadata(share_url=url)
    for factory in (embeds.make_now_playing_embed, embeds.make_queue_embed):
        assert all(not field.name.startswith("Open in") for field in factory(meta).fields)
    assert "[Open in" not in controller._track_info(meta, autoplay_enabled=False)


def test_source_and_quality_are_escaped_in_controller(real_ui):
    _, controller = real_ui
    info = controller._track_info(metadata(audio_resolution="**fake** @here"), autoplay_enabled=True)
    assert "**Catalog quality:** \\*\\*fake\\*\\*" in info
    assert "@here" not in info
    info = controller._track_info(metadata(source="[evil](https://evil.example) @everyone"), autoplay_enabled=False)
    assert "[evil](https://evil.example)" not in info
    assert "@everyone" not in info


@pytest.mark.asyncio
@pytest.mark.parametrize("uploaded", [True, False])
async def test_uploaded_file_controller_omits_autoplay_and_suggestions(real_ui, cog, uploaded):
    _, controller = real_ui
    view = controller.PlayerControllerView(cog, metadata(
        source="Uploaded file" if uploaded else "YouTube", duration=15,
    ), autoplay_enabled=True)
    try:
        text = "\n".join(item.content for item in view.walk_children()
                         if isinstance(item, real_discord.ui.TextDisplay))
        controls = {getattr(item, "custom_id", None) for item in view.walk_children()}
        assert "**Duration:** 00:15" in text
        assert ("Autoplay" in text) is not uploaded
        assert ("Suggested songs" in text) is not uploaded
        assert ("tidalplayer:v2:autoplay" in controls) is not uploaded
        assert ("tidalplayer:v2:suggestions" in controls) is not uploaded
        assert {"tidalplayer:v2:pause", "tidalplayer:v2:skip", "tidalplayer:v2:stop"} <= controls
        view.to_components()
    finally:
        view.stop()


@pytest.mark.parametrize("factory", ["error_embed", "success_embed"])
def test_status_embeds_keep_authored_markdown_and_bound_descriptions(real_ui, factory):
    embeds, _ = real_ui
    render = getattr(embeds, factory)
    assert render("Run `[p]tidalsetup login`.").description == "Run `[p]tidalsetup login`."
    description = render("😀" * 5000).description
    assert len(description.encode("utf-16-le")) // 2 <= 4096


def test_share_link_parentheses_cannot_escape_the_authored_link(real_ui):
    embeds, controller = real_ui
    meta = metadata(share_url="https://example.com/a)[evil](https://evil.example)")
    expected = "https://example.com/a%29%5Bevil%5D%28https://evil.example%29"
    assert embeds.make_queue_embed(meta).fields[0].value == f"[Listen]({expected})"
    assert f"[Open in TIDAL]({expected})" in controller._track_info(meta, autoplay_enabled=False)


@pytest.mark.asyncio
async def test_large_source_and_suggestion_labels_fit_discord_payload(real_ui, cog):
    embeds, controller = real_ui
    huge = "😀[" * 5000
    meta = metadata(source=huge, title=huge, artist=huge, album=huge)
    embed = embeds.make_now_playing_embed(meta)
    assert len(embed.title.encode("utf-16-le")) // 2 <= 256
    assert all(len(field.name.encode("utf-16-le")) // 2 <= 256 for field in embed.fields)
    view = controller.PlayerControllerView(cog, meta, next_up=meta, recommendations=[
        SimpleNamespace(full_name=huge, artist=SimpleNamespace(name=huge))])
    texts = [item.content for item in view.walk_children() if isinstance(item, real_discord.ui.TextDisplay)]
    assert sum(len(text.encode("utf-16-le")) // 2 for text in texts) <= 4000
    select = next(item for item in view.walk_children() if isinstance(item, real_discord.ui.Select))
    assert len(select.options[0].label.encode("utf-16-le")) // 2 <= 100
    assert len(select.options[0].description.encode("utf-16-le")) // 2 <= 100

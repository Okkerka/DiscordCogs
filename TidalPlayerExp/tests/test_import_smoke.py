"""Catch syntax errors, merge markers, and missing command dependencies early."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_python_files_have_valid_syntax_and_no_merge_conflicts() -> None:
    paths = (
        "commands.py",
        "tidalplayer.py",
        "ui/queue.py",
        "playback/session.py",
        "playback/backend.py",
        "playback/ffmpeg.py",
        "providers/attachments.py",
        "providers/tidal_source.py",
        "providers/youtube_resolver.py",
    )

    markers = ("<<<<<<<", "=======", ">>>>>>>")

    for relative_path in paths:
        source = _read(relative_path)
        assert not any(marker in source for marker in markers), relative_path
        ast.parse(source, filename=relative_path)


def test_playnext_uses_an_existing_internal_play_handler() -> None:
    source = _read("commands.py")
    player_source = _read("tidalplayer.py")

    assert "await self._play_request(ctx, query=query)" in source
    assert "async def _play_request(" in player_source

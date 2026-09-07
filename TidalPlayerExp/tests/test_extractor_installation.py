"""Downloader's newer yt-dlp must win over an older global installation."""

from __future__ import annotations

import importlib.metadata
from importlib.machinery import ModuleSpec

import pytest

from TidalPlayerExp.playback.errors import PlaybackUnavailable
from TidalPlayerExp.providers import youtube_resolver as resolver


def installation(root, version, *, metadata_version=None):
    package = root / "yt_dlp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("raise AssertionError('must not import yt-dlp')\n")
    (package / "__main__.py").write_text("")
    (package / "version.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
    metadata = root / "yt_dlp-test.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Name: yt-dlp\nVersion: {metadata_version or version}\n")
    return package


def discovery(monkeypatch, roots):
    distributions = list(importlib.metadata.distributions(path=[str(root) for root in roots]))
    monkeypatch.setattr(importlib.metadata, "distributions", lambda **kwargs: iter(distributions))
    spec = ModuleSpec("yt_dlp", loader=None, is_package=True)
    spec.submodule_search_locations = [str(roots[0] / "yt_dlp")]
    monkeypatch.setattr(resolver.importlib.util, "find_spec", lambda name: spec)


def test_newer_downloader_extractor_wins_without_importing_it(tmp_path, monkeypatch):
    old, target = tmp_path / "global", tmp_path / "Downloader" / "lib"
    installation(old, "2025.03.31")
    installation(target, "2026.08.19")
    discovery(monkeypatch, [old, target])
    assert resolver._yt_dlp_root() == str(target)


def test_stale_dist_info_does_not_override_actual_extractor_code(tmp_path, monkeypatch):
    stale, target = tmp_path / "global", tmp_path / "lib"
    installation(stale, "2025.03.31", metadata_version="2099.01.01")
    installation(target, "2026.08.19", metadata_version="2024.01.01")
    discovery(monkeypatch, [stale, target])
    assert resolver._yt_dlp_root() == str(target)


@pytest.mark.parametrize("version_text", [
    "__version__ = '2025.03.31'\n",
    "__version__ = 'not-a-version'\n",
    "__version__ = str(__import__('sys').version)\n",
    "broken python (",
    "#" * 17000,
], ids=["outdated", "invalid-version", "nonliteral", "syntax", "oversized"])
def test_outdated_or_unreadable_extractor_is_not_reported_ready(tmp_path, monkeypatch, version_text):
    package = installation(tmp_path / "lib", "2026.08.19")
    (package / "version.py").write_text(version_text, encoding="utf-8")
    discovery(monkeypatch, [package.parent])
    with pytest.raises(PlaybackUnavailable):
        resolver._yt_dlp_root()

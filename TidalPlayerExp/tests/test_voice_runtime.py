"""Exercise Red's late dependency visibility with real discord.py in isolation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_STARTUP = r"""
import asyncio
import importlib.abc
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
mode = sys.argv[2]

class HiddenDependencies(importlib.abc.MetaPathFinder):
    broken = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'nacl', 'davey'}:
            if self.broken:
                raise OSError('private installation path or token')
            raise ImportError('Downloader lib is not visible yet')

hidden = HiddenDependencies()
if mode != 'ready':
    sys.meta_path.insert(0, hidden)

import discord
import discord.voice_client as vc
import discord.voice_state as vs
import discord.gateway as gateway

original_client = discord.VoiceClient
original_state = vs.VoiceConnectionState
if mode != 'ready':
    assert not vc.has_nacl and not vc.has_dave
if mode == 'late':
    sys.meta_path.remove(hidden)
elif mode == 'broken':
    hidden.broken = True

from TidalPlayerExp.tidalplayer import TidalPlayerExp

async def noop(**kwargs):
    pass

async def main():
    # Config and provider initialization are unrelated to local voice loading.
    cog = SimpleNamespace(
        bot=SimpleNamespace(get_cog=lambda name: None, add_view=lambda view: None),
        runtime=SimpleNamespace(cleanup=noop),
        _migrate_config=noop, _initialize_apis=noop,
    )
    await TidalPlayerExp.cog_load(cog)
    cog._persistent_view.stop()
    assert discord.VoiceClient is original_client
    assert vs.VoiceConnectionState is original_state
    if mode in {'missing', 'broken'}:
        assert not vc.has_nacl and not vc.has_dave and not vs.has_dave
        return

    assert vc.has_nacl and vc.has_dave and vs.has_dave, 'late voice dependencies were not activated'
    import nacl.secret
    import davey
    assert vc.nacl.secret is nacl.secret
    assert vs.davey is davey and gateway.davey is davey
    # Verify the restored binding can encrypt actual Discord transport audio.
    transport = SimpleNamespace(secret_key=[0] * 32, _incr_nonce=0)
    transport.checked_add = lambda attr, value, limit: setattr(transport, attr, 1)
    header = bytes(12)
    packet = vc.VoiceClient._encrypt_aead_xchacha20_poly1305_rtpsize(transport, header, b'opus')
    nonce = packet[-4:] + bytes(20)
    assert nacl.secret.Aead(bytes(32)).decrypt(packet[12:-4], header, nonce) == b'opus'
    # Exercise DAVE session construction and key generation through Discord.
    packages = []
    async def send_binary(op, data):
        packages.append(data)
    state = SimpleNamespace(
        dave_protocol_version=davey.DAVE_PROTOCOL_VERSION, dave_session=None,
        user=SimpleNamespace(id=123),
        voice_client=SimpleNamespace(channel=SimpleNamespace(id=456), ws=SimpleNamespace(send_binary=send_binary)),
    )
    await vs.VoiceConnectionState.reinit_dave_session(state)
    assert isinstance(state.dave_session, davey.DaveSession)
    assert packages and packages[0]
    # Repeated cog loads must preserve ready bindings and classes.
    await TidalPlayerExp.cog_load(cog)
    cog._persistent_view.stop()
    assert vc.nacl.secret is nacl.secret and vs.davey is davey

asyncio.run(main())
"""


@pytest.mark.parametrize("mode", ["late", "ready", "missing", "broken"])
def test_cog_load_handles_real_discord_dependency_startup_order(mode):
    result = subprocess.run(
        [sys.executable, "-I", "-c", _STARTUP, str(Path(__file__).resolve().parents[2]), mode],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "private installation path or token" not in result.stderr

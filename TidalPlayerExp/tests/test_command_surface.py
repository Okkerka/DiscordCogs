"""Verify the real Red command/slash schema without connecting to Discord."""
import subprocess
import sys
from pathlib import Path


def test_prefix_and_slash_surface_has_new_names_and_attachment_option():
    code = '''
from TidalPlayerExp.tidalplayer import TidalPlayerExp
from redbot.core.commands import HybridCommand, HybridGroup
import discord
commands = {c.name: c for c in TidalPlayerExp.__cog_commands__ if c.parent is None}
required = {'play','playfrom','playfile','playnext','queue','stop','remove','clear','volume','pause','resume','skip',
            'now','move','shuffle','repeat','seek','replay','autoplay','retry','musichelp',
            'tidalsearch','tplaylist','setup','tfilter','tinteractive'}
assert required <= commands.keys(), required - commands.keys()
assert not {'tplay','tstop','tqueue','tsearch','tpl','tidalsetup'} & commands.keys()
assert not any('tstop' in c.aliases for c in commands.values())
for command in commands.values():
    assert isinstance(command, (HybridCommand, HybridGroup)), command.name
    assert command.app_command is not None, command.name
    for child in getattr(command, 'commands', []):
        assert child.app_command is not None, child.qualified_name
params = {p.name:p for p in commands['playfile'].app_command.parameters}
assert params['file'].type.value == 11
play_params = {p.name:p for p in commands['play'].app_command.parameters}
assert set(play_params) == {'query'}
from_params = {p.name:p for p in commands['playfrom'].app_command.parameters}
assert set(from_params) == {'platform', 'query'}
assert {c.value for c in from_params['platform'].choices} == {'tidal', 'youtube', 'soundcloud'}
tree = discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
wire_options = {p['name']: p for p in commands['play'].app_command.to_dict(tree)['options']}
assert set(wire_options) == {'query'}
assert wire_options['query']['required'] is True
from_options = commands['playfrom'].app_command.to_dict(tree)['options']
assert {p['name'] for p in from_options} == {'platform', 'query'}
assert all(p['required'] for p in from_options)
import inspect
assert list(commands['playfrom'].clean_params) == ['platform', 'query']
assert commands['playfrom'].clean_params['query'].kind == inspect.Parameter.KEYWORD_ONLY
assert commands['remove'].get_command('all') is not None
assert {'list','create','add','remove','play'} <= {c.name for c in commands['tplaylist'].commands}
assert 'disconnect' not in commands
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr

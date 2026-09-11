from utilities.utilities import Utilities


def test_slash_surface_and_thanos_exclusion():
    commands = {c.name: c for c in Utilities.__cog_commands__}
    for name in ("avatar", "choose", "remindme", "reminders", "quote", "timestamp", "membercount"):
        assert name in commands
        assert commands[name].app_command is not None
    assert getattr(commands["thanos"], "app_command", None) is None

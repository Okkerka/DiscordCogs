from moderation.moderation import Moderation


def test_slash_surface():
    commands = {c.name: c for c in Moderation.__cog_commands__}
    for name in ("kick", "ban", "purge", "cleanup", "dehoist", "nickname", "modhistory", "msgblock"):
        assert name in commands
        assert commands[name].app_command is not None


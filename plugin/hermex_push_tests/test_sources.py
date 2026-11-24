import pytest

from hermex_push.sources import coarse_source, should_notify


@pytest.mark.parametrize("platform,expected", [
    ("ios", "bot"), ("desktop", "bot"), ("tui", "bot"), ("bot_room", "other"), ("webui", "webui"), ("cli", "other"), ("", "other"), (None, "other"),
])
def test_coarse_source(platform, expected):
    assert coarse_source(platform) == expected


def test_native_notifying_platforms_and_automation_sessions_are_skipped():
    assert should_notify("s1", "desktop")
    assert should_notify("s1", "webui")
    assert not should_notify("s1", "")  # unknown platform fails closed
    assert not should_notify("s1", "telegram")
    assert not should_notify("cron_job_1", "desktop")
    assert not should_notify("kanban_task", "desktop")
    assert not should_notify("", "desktop")


def test_automation_platforms_skip_replies_but_keep_failures():
    # Hermes 0.22+ cron/kanban sessions: id is "local-<hash>", the platform names the automation.
    assert not should_notify("local-1f7a8d60", "cron")  # progress calls omit the kind
    assert not should_notify("local-1f7a8d60", "cron", "reply")
    assert not should_notify("local-1f7a8d60", "cron", "clarify")
    assert not should_notify("local-1f7a8d60", "cron", "approval")
    assert should_notify("local-1f7a8d60", "cron", "turn_error")
    assert not should_notify("local-1f7a8d60", "kanban", "reply")
    assert should_notify("local-1f7a8d60", "kanban", "turn_error")
    # Old-style ids stay skipped on interactive platforms, whatever the kind.
    assert not should_notify("cron_job_1", "desktop", "reply")
    assert not should_notify("kanban_task", "desktop", "turn_error")
    # Interactive sessions are unaffected by the kind argument.
    assert should_notify("s1", "desktop", "reply")
    assert should_notify("s1", "webui", "turn_error")

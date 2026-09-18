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
    assert should_notify("s1", "")
    assert not should_notify("s1", "telegram")
    assert not should_notify("cron_job_1", "desktop")
    assert not should_notify("kanban_task", "desktop")
    assert not should_notify("", "desktop")

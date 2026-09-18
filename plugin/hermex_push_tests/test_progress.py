from hermex_push.progress import ProgressCoalescer


def test_status_changes_always_emit_and_routine_updates_coalesce():
    c = ProgressCoalescer()
    assert c.turn_started("s", 0.0).status == "running"
    assert c.tool_started("s", "terminal", 0.2) is None  # within the hold window
    assert c.tool_finished("s", 0.4) is None
    assert c.has_pending()
    assert c.due(0.9) == []
    flushed = c.due(1.0)
    assert [f.tool_calls for f in flushed] == [1] and flushed[0].tool is None
    assert not c.has_pending()
    waiting = c.waiting("s", 1.1)
    assert waiting is not None and waiting.status == "waiting"
    assert c.resumed("s", 1.2).status == "running"


def test_turn_end_emits_and_forgets_the_session():
    c = ProgressCoalescer()
    c.turn_started("s", 0.0)
    c.tool_started("s", "read_file", 0.1)
    ended = c.turn_ended("s", 0.2, failed=True)
    assert ended.status == "failed" and ended.tool_calls == 1
    assert c.turn_ended("s", 0.3, failed=False) is None
    assert c.due(5.0) == []


def test_sessions_are_independent():
    c = ProgressCoalescer()
    c.turn_started("a", 0.0)
    assert c.turn_started("b", 0.1).session_id == "b"
    assert c.tool_started("a", "terminal", 0.2) is None
    assert c.turn_ended("b", 0.3, failed=False).session_id == "b"
    assert [s.session_id for s in c.due(1.5)] == ["a"]


def test_sessions_that_never_end_are_bounded_and_stale_ones_forgotten():
    from hermex_push.progress import MAX_SESSIONS, STALE_SECONDS
    c = ProgressCoalescer()
    for i in range(MAX_SESSIONS + 100):
        c.tool_started(f"s{i}", "terminal", float(i))
    assert len(c._sessions) <= MAX_SESSIONS
    assert "s0" not in c._sessions
    c.due(float(MAX_SESSIONS + 100) + STALE_SECONDS)
    assert c._sessions == {}

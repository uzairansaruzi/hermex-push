from hermex_push.progress import ProgressCoalescer


def test_status_changes_always_emit_and_routine_updates_coalesce():
    c = ProgressCoalescer()
    assert c.turn_started("s", 0.0).status == "running"
    # Routine tool boundaries inside the five-second hold coalesce into one held update.
    assert c.tool_started("s", "terminal", 1.0) is None
    assert c.tool_finished("s", 2.0) is None
    assert c.tool_started("s", "read_file", 3.0) is None
    assert c.tool_finished("s", 4.0) is None
    assert c.flush_delay(4.0) == 1.0  # due when the hold ends, not a full hold after the last update
    assert c.due(4.9) == []
    flushed = c.due(5.0)
    assert [f.tool_calls for f in flushed] == [2] and flushed[0].tool is None
    assert c.flush_delay(5.0) is None
    # A status change inside the next hold window still goes out at once.
    waiting = c.waiting("s", 6.0)
    assert waiting is not None and waiting.status == "waiting"
    assert c.resumed("s", 7.0).status == "running"


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
    assert [s.session_id for s in c.due(5.0)] == ["a"]


def test_sessions_that_never_end_are_bounded_and_stale_ones_forgotten():
    from hermex_push.progress import MAX_SESSIONS, STALE_SECONDS
    c = ProgressCoalescer()
    for i in range(MAX_SESSIONS + 100):
        c.tool_started(f"s{i}", "terminal", float(i))
    assert len(c._sessions) <= MAX_SESSIONS
    assert "s0" not in c._sessions
    c.due(float(MAX_SESSIONS + 100) + STALE_SECONDS)
    assert c._sessions == {}

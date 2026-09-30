from hermex_push.progress import GRACE_SECONDS, QUIET_SECONDS, ProgressCoalescer


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


def test_an_unwatched_answer_inside_the_grace_period_is_ignored():
    # The phone registers its activity around the turn's first event, so early answers can lag it.
    c = ProgressCoalescer()
    c.turn_started("s", 0.0)
    c.unwatched("s", GRACE_SECONDS - 1)
    assert c.tool_started("s", "terminal", GRACE_SECONDS) is not None


def test_after_the_grace_period_routine_updates_wait_out_the_quiet_window():
    c = ProgressCoalescer()
    c.turn_started("s", 0.0)
    c.tool_started("s", "terminal", 30.0)
    c.unwatched("s", 30.0)  # quiet until 90
    assert c.tool_started("s", "read_file", 40.0) is None
    assert c.tool_started("s", "search", 60.0) is None
    assert c.flush_delay(60.0) == 30.0
    assert c.due(89.0) == []
    [flushed] = c.due(30.0 + QUIET_SECONDS)
    assert (flushed.tool, flushed.tool_calls) == ("search", 3)
    # The flushed update is the next probe; its answer starts another window.
    c.unwatched("s", 90.0)
    assert c.tool_started("s", "terminal", 95.0) is None
    assert c.flush_delay(95.0) == 55.0


def test_a_status_change_skips_the_quiet_window():
    c = ProgressCoalescer()
    c.turn_started("s", 0.0)
    c.unwatched("s", 30.0)
    assert c.waiting("s", 40.0).status == "waiting"
    assert c.resumed("s", 41.0).status == "running"
    assert c.tool_started("s", "terminal", 50.0) is None
    assert c.turn_ended("s", 51.0, failed=False).status == "done"


def test_watched_restores_the_five_second_cadence():
    c = ProgressCoalescer()
    c.turn_started("s", 0.0)
    c.unwatched("s", 30.0)
    assert c.tool_started("s", "terminal", 40.0) is None
    c.watched("s")
    assert c.flush_delay(40.0) == 0.0
    assert [f.tool_calls for f in c.due(40.0)] == [1]
    assert c.tool_started("s", "read_file", 44.0) is None
    assert c.flush_delay(44.0) == 1.0


def test_answers_for_a_session_whose_turn_ended_are_dropped():
    c = ProgressCoalescer()
    c.turn_started("s", 0.0)
    c.turn_ended("s", 40.0, failed=False)
    c.unwatched("s", 41.0)
    c.watched("s")
    assert c._sessions == {}
    # The next turn starts at the normal cadence.
    c.turn_started("s", 50.0)
    c.unwatched("s", 51.0)
    assert c.tool_started("s", "terminal", 55.0) is not None


def test_a_quiet_session_does_not_delay_another_sessions_flush():
    c = ProgressCoalescer()
    c.turn_started("quiet", 0.0)
    c.unwatched("quiet", 30.0)  # quiet until 90
    assert c.tool_started("quiet", "terminal", 31.0) is None
    c.turn_started("busy", 30.0)
    assert c.tool_started("busy", "terminal", 32.0) is None  # held until 35
    assert c.flush_delay(32.0) == 3.0
    assert [s.session_id for s in c.due(35.0)] == ["busy"]
    assert c.flush_delay(35.0) == 55.0

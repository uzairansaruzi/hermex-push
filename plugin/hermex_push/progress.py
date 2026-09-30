"""Per-session Live Activity progress with five-second coalescing.

A status change (``running`` / ``waiting`` / ``done`` / ``failed``) always produces an event
right away. Routine tool boundaries inside the same status are held so a session emits at most
one routine progress event every :data:`HOLD_SECONDS`; the caller flushes held events with
:meth:`due` when :meth:`flush_delay` says a hold has ended.

The relay answers ``no_activity`` when no phone shows a Live Activity for the session; the
caller reports that with :meth:`unwatched`, which (after :data:`GRACE_SECONDS` of the turn)
holds routine updates for :data:`QUIET_SECONDS`. The held update that goes out when the window
ends probes again, and any other answer calls :meth:`watched` to restore the normal cadence.
Pure: time comes from the caller, so tests drive it with a fake clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

HOLD_SECONDS = 5.0  # progress is most relay traffic; status changes skip the hold
# The phone registers its activity around the turn's first event, so early answers can lag it.
GRACE_SECONDS = 30.0
QUIET_SECONDS = 60.0  # an unwatched session's routine updates, once the grace has passed
MAX_SESSIONS = 512
STALE_SECONDS = 15 * 60  # a session silent this long is forgotten; matches the activity stale date


@dataclass
class SessionProgress:
    status: str = "running"
    tool: Optional[str] = None
    tool_calls: int = 0
    started_at: float = 0.0
    last_emit: float = float("-inf")
    pending: bool = False
    quiet_until: float = float("-inf")  # routine updates hold until then; no phone is watching


@dataclass
class ProgressSnapshot:
    session_id: str
    status: str
    tool: Optional[str]
    tool_calls: int
    started_at: float


@dataclass
class ProgressCoalescer:
    _sessions: dict[str, SessionProgress] = field(default_factory=dict)

    @staticmethod
    def _holding(state: SessionProgress, now: float) -> bool:
        return now - state.last_emit < HOLD_SECONDS or now < state.quiet_until

    def _snapshot(self, session_id: str, state: SessionProgress, now: float) -> ProgressSnapshot:
        state.last_emit = now
        state.pending = False
        return ProgressSnapshot(session_id, state.status, state.tool, state.tool_calls, state.started_at)

    def _update(self, session_id: str, now: float, *, status: Optional[str] = None,
                tool: Optional[str] = None, count_call: bool = False) -> Optional[ProgressSnapshot]:
        state = self._sessions.get(session_id)
        if state is None:
            self._evict(now)
            state = self._sessions[session_id] = SessionProgress(started_at=now)
            status_changed = True
        else:
            status_changed = status is not None and status != state.status
        if status is not None:
            state.status = status
        state.tool = tool
        if count_call:
            state.tool_calls += 1
        if status_changed or not self._holding(state, now):
            return self._snapshot(session_id, state, now)
        state.pending = True
        return None

    def _evict(self, now: float) -> None:
        """Forget stale sessions, then the oldest ones past the cap. Agents that never reach
        ``on_session_end`` (persist-disabled forks) would otherwise accumulate forever."""
        for sid, state in list(self._sessions.items()):
            if now - max(state.last_emit, state.started_at) >= STALE_SECONDS:
                self._sessions.pop(sid, None)
        while len(self._sessions) >= MAX_SESSIONS:
            self._sessions.pop(next(iter(self._sessions)), None)

    def turn_started(self, session_id: str, now: float) -> Optional[ProgressSnapshot]:
        state = self._sessions.get(session_id)
        if state is None or state.status in ("done", "failed"):
            self._evict(now)
            self._sessions[session_id] = SessionProgress(started_at=now)
            return self._snapshot(session_id, self._sessions[session_id], now)
        return self._update(session_id, now, status="running")

    def tool_started(self, session_id: str, tool: str, now: float) -> Optional[ProgressSnapshot]:
        return self._update(session_id, now, status="running", tool=tool, count_call=True)

    def tool_finished(self, session_id: str, now: float) -> Optional[ProgressSnapshot]:
        return self._update(session_id, now, tool=None)

    def waiting(self, session_id: str, now: float) -> Optional[ProgressSnapshot]:
        return self._update(session_id, now, status="waiting")

    def resumed(self, session_id: str, now: float) -> Optional[ProgressSnapshot]:
        return self._update(session_id, now, status="running")

    def turn_ended(self, session_id: str, now: float, *, failed: bool) -> Optional[ProgressSnapshot]:
        if session_id not in self._sessions:
            return None
        snapshot = self._update(session_id, now, status="failed" if failed else "done", tool=None)
        self._sessions.pop(session_id, None)
        return snapshot

    def unwatched(self, session_id: str, now: float) -> None:
        """The relay found no Live Activity for the session: quiet its routine updates, unless
        the turn is still inside its grace period. A session whose turn ended is ignored."""
        state = self._sessions.get(session_id)
        if state is not None and now - state.started_at >= GRACE_SECONDS:
            state.quiet_until = now + QUIET_SECONDS

    def watched(self, session_id: str) -> None:
        """A phone shows a Live Activity for the session: back to the normal cadence."""
        state = self._sessions.get(session_id)
        if state is not None:
            state.quiet_until = float("-inf")

    def due(self, now: float) -> list[ProgressSnapshot]:
        """Held routine updates whose hold (and quiet window) has passed."""
        self._evict(now)
        out = []
        for session_id, state in self._sessions.items():
            if state.pending and not self._holding(state, now):
                out.append(self._snapshot(session_id, state, now))
        return out

    def flush_delay(self, now: float) -> Optional[float]:
        """Seconds until the earliest held update's hold (or quiet window) ends, or None when
        nothing is held."""
        holds = [max(HOLD_SECONDS - (now - s.last_emit), s.quiet_until - now)
                 for s in self._sessions.values() if s.pending]
        return max(0.0, min(holds)) if holds else None

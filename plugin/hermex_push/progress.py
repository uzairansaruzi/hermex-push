"""Per-session Live Activity progress with one-second coalescing.

A status change (``running`` / ``waiting`` / ``done`` / ``failed``) always produces an event
right away. Routine tool boundaries inside the same status are held so a session emits at most
one progress event per second; the caller flushes held events with :meth:`due` after
:attr:`HOLD_SECONDS`. Pure: time comes from the caller, so tests drive it with a fake clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

HOLD_SECONDS = 1.0


@dataclass
class SessionProgress:
    status: str = "running"
    tool: Optional[str] = None
    tool_calls: int = 0
    started_at: float = 0.0
    last_emit: float = float("-inf")
    pending: bool = False


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

    def _snapshot(self, session_id: str, state: SessionProgress, now: float) -> ProgressSnapshot:
        state.last_emit = now
        state.pending = False
        return ProgressSnapshot(session_id, state.status, state.tool, state.tool_calls, state.started_at)

    def _update(self, session_id: str, now: float, *, status: Optional[str] = None,
                tool: Optional[str] = None, count_call: bool = False) -> Optional[ProgressSnapshot]:
        state = self._sessions.get(session_id)
        if state is None:
            state = self._sessions[session_id] = SessionProgress(started_at=now)
            status_changed = True
        else:
            status_changed = status is not None and status != state.status
        if status is not None:
            state.status = status
        state.tool = tool
        if count_call:
            state.tool_calls += 1
        if status_changed or now - state.last_emit >= HOLD_SECONDS:
            return self._snapshot(session_id, state, now)
        state.pending = True
        return None

    def turn_started(self, session_id: str, now: float) -> Optional[ProgressSnapshot]:
        state = self._sessions.get(session_id)
        if state is None or state.status in ("done", "failed"):
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

    def due(self, now: float) -> list[ProgressSnapshot]:
        """Held routine updates whose hold window has passed."""
        out = []
        for session_id, state in self._sessions.items():
            if state.pending and now - state.last_emit >= HOLD_SECONDS:
                out.append(self._snapshot(session_id, state, now))
        return out

    def has_pending(self) -> bool:
        return any(s.pending for s in self._sessions.values())

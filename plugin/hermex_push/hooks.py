"""Hook callbacks. One :class:`HermexPush` instance owns the per-session state that turns
hermes-agent lifecycle hooks into relay events.

Per turn: ``pre_llm_call`` records the platform and starts progress, ``post_llm_call`` captures
the final reply text, ``on_session_end`` is the single boundary that sends exactly one ``reply``
or ``turn_error`` (or nothing, for an interrupted turn). Approvals come from the
``pre_approval_request`` observer, questions from ``pre_tool_call`` on the ``clarify`` tool.
Every callback swallows its own errors: a push must never break a turn.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import RELAY_URL_ENV
from .keys import Keys, load_or_create_keys
from .payload import notify_event, preview, progress_event
from .progress import HOLD_SECONDS, ProgressCoalescer, ProgressSnapshot
from .relay import RelaySender, notify_url
from .sources import coarse_source, should_notify

logger = logging.getLogger("hermex_push")

CLARIFY_TOOL = "clarify"
INPUT_TOOLS = frozenset({"sudo", "secret"})  # V1.1: ``input`` pushes
MAX_TRACKED_SESSIONS = 512


def relay_url_from_env() -> str:
    """``HERMEX_PUSH_RELAY_URL`` resolved the way hermes-agent resolves any managed credential:
    the active profile scope, then the process environment, then ``<hermes_home>/.env``. The
    last step matters for hosts such as hermes-webui that never export the Hermes ``.env``.
    Read on every send so a value set after startup still works."""
    try:
        from hermes_cli.config import get_env_value
        value = get_env_value(RELAY_URL_ENV)
    except Exception:
        value = os.environ.get(RELAY_URL_ENV, "")
    return (value or "").strip()


class _Recent(OrderedDict):
    """Bounded insertion-ordered map so long-lived gateways never grow without limit."""

    def remember(self, key: str, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > MAX_TRACKED_SESSIONS:
            self.popitem(last=False)


class HermexPush:
    def __init__(
        self, *, profile: str = "", sender: Optional[RelaySender] = None,
        keys_loader: Callable[[], Keys] = load_or_create_keys,
        relay_url: Callable[[], str] = relay_url_from_env, now: Callable[[], float] = time.time,
        schedule: Optional[Callable[[float, Callable[[], None]], None]] = None,
    ) -> None:
        self._profile = profile
        self._sender = sender or RelaySender()
        self._keys_loader = keys_loader
        self._relay_url = relay_url
        self._now = now
        self._schedule = schedule or self._timer
        self._lock = threading.RLock()
        self._keys: Optional[Keys] = None
        self._platform_by_session = _Recent()
        self._reply_by_session = _Recent()
        self._subagent_sessions = _Recent()
        self._progress = ProgressCoalescer()
        self._flush_scheduled = False

    # -- wiring ---------------------------------------------------------------------------

    def hook_callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "pre_llm_call": self.pre_llm_call,
            "post_llm_call": self.post_llm_call,
            "on_session_end": self.on_session_end,
            "pre_tool_call": self.pre_tool_call,
            "post_tool_call": self.post_tool_call,
            "pre_approval_request": self.pre_approval_request,
            "post_approval_response": self.post_approval_response,
            "subagent_start": self.subagent_start,
        }

    @staticmethod
    def _timer(delay: float, fn: Callable[[], None]) -> None:
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        timer.start()

    def _keys_or_none(self) -> Optional[Keys]:
        if self._keys is None:
            try:
                self._keys = self._keys_loader()
            except Exception:
                logger.warning("hermex-push: keys unavailable; nothing will be sent", exc_info=True)
        return self._keys

    def _send(self, event: dict[str, Any]) -> bool:
        url = self._relay_url()
        keys = self._keys_or_none()
        if not url or keys is None:
            logger.debug("hermex-push: relay not configured; dropping a %s event", event.get("kind"))
            return False
        return self._sender.enqueue(notify_url(url, keys.install_key), event)

    def _session(self, session_id: str) -> tuple[str, str, bool]:
        platform = self._platform_by_session.get(session_id, "")
        return platform, coarse_source(platform), session_id in self._subagent_sessions

    def _notify(self, kind: str, session_id: str, event_ref: str, content: Optional[dict[str, str]]) -> None:
        platform, source, is_subagent = self._session(session_id)
        if not should_notify(session_id, platform):
            return
        keys = self._keys_or_none()
        if keys is None:
            return
        self._send(notify_event(
            kind=kind, session_id=session_id, event_ref=event_ref, source=source, is_subagent=is_subagent,
            keys=keys, preview=content, now=self._now(),
        ))

    # -- progress -------------------------------------------------------------------------

    def _emit_progress(self, snapshot: Optional[ProgressSnapshot]) -> None:
        if snapshot is None:
            self._arm_flush()
            return
        platform, source, is_subagent = self._session(snapshot.session_id)
        if not should_notify(snapshot.session_id, platform):
            return
        keys = self._keys_or_none()
        if keys is None:
            return
        self._send(progress_event(
            session_id=snapshot.session_id, source=source, is_subagent=is_subagent, keys=keys,
            status=snapshot.status, tool=snapshot.tool, tool_calls=snapshot.tool_calls,
            started_at=snapshot.started_at, now=self._now(),
        ))

    def _arm_flush(self) -> None:
        with self._lock:
            if self._flush_scheduled or not self._progress.has_pending():
                return
            self._flush_scheduled = True
        self._schedule(HOLD_SECONDS, self.flush_progress)

    def flush_progress(self) -> None:
        with self._lock:
            self._flush_scheduled = False
            snapshots = self._progress.due(self._now())
        for snapshot in snapshots:
            self._emit_progress(snapshot)
        self._arm_flush()

    # -- hook callbacks (kwargs only; unknown kwargs ignored) ------------------------------

    def pre_llm_call(self, *, session_id: str = "", platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                if platform:
                    self._platform_by_session.remember(session_id, platform)
                self._reply_by_session.pop(session_id, None)
                snapshot = self._progress.turn_started(session_id, self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.debug("hermex-push: pre_llm_call failed", exc_info=True)

    def post_llm_call(self, *, session_id: str = "", user_message: str = "", assistant_response: str = "",
                      platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                if platform:
                    self._platform_by_session.remember(session_id, platform)
                self._reply_by_session.remember(session_id, (user_message or "", assistant_response or ""))
        except Exception:
            logger.debug("hermex-push: post_llm_call failed", exc_info=True)

    def on_session_end(self, *, session_id: str = "", completed: bool = False, interrupted: bool = False,
                       failed: bool = False, turn_id: str = "", turn_exit_reason: str = "",
                       platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                if platform:
                    self._platform_by_session.remember(session_id, platform)
                captured = self._reply_by_session.pop(session_id, None)
                snapshot = self._progress.turn_ended(session_id, self._now(), failed=bool(failed))
            self._emit_progress(snapshot)
            if interrupted:
                return
            ref = turn_id or str(int(self._now()))
            if failed:
                self._notify("turn_error", session_id, ref, preview(
                    title="Turn failed", body=turn_exit_reason or "The agent stopped without finishing.",
                    profile=self._profile))
            elif completed and captured is not None:
                user_message, reply = captured
                self._notify("reply", session_id, ref, preview(
                    title=self._profile or "Hermes", subtitle=user_message, body=reply, profile=self._profile))
        except Exception:
            logger.debug("hermex-push: on_session_end failed", exc_info=True)

    def pre_tool_call(self, *, tool_name: str = "", args: Optional[dict[str, Any]] = None, session_id: str = "",
                      tool_call_id: str = "", **_: Any) -> None:
        try:
            now = self._now()
            if tool_name == CLARIFY_TOOL:
                with self._lock:
                    snapshot = self._progress.waiting(session_id, now)
                self._emit_progress(snapshot)
                question = str((args or {}).get("question") or "The agent has a question for you.")
                self._notify("clarify", session_id, tool_call_id or f"{tool_name}:{int(now)}", preview(
                    title="Question", body=question, profile=self._profile, request_id=tool_call_id))
                return
            with self._lock:
                snapshot = self._progress.tool_started(session_id, tool_name, now)
            self._emit_progress(snapshot)
        except Exception:
            logger.debug("hermex-push: pre_tool_call failed", exc_info=True)

    def post_tool_call(self, *, tool_name: str = "", session_id: str = "", **_: Any) -> None:
        try:
            with self._lock:
                if tool_name == CLARIFY_TOOL:
                    snapshot = self._progress.resumed(session_id, self._now())
                else:
                    snapshot = self._progress.tool_finished(session_id, self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.debug("hermex-push: post_tool_call failed", exc_info=True)

    def pre_approval_request(self, *, command: str = "", description: str = "", session_key: str = "",
                             session_id: str = "", surface: str = "", request_id: str = "",
                             tool_call_id: str = "", **_: Any) -> None:
        if surface == "smart":  # auxiliary-model decision, nobody is waiting
            return
        try:
            sid = session_id or session_key
            with self._lock:
                snapshot = self._progress.waiting(sid, self._now())
            self._emit_progress(snapshot)
            ref = request_id or tool_call_id or f"approval:{int(self._now())}"
            self._notify("approval", sid, ref, preview(
                title="Approval needed", subtitle=description, body=command, profile=self._profile,
                request_id=request_id))
        except Exception:
            logger.debug("hermex-push: pre_approval_request failed", exc_info=True)

    def post_approval_response(self, *, session_key: str = "", session_id: str = "", **_: Any) -> None:
        try:
            with self._lock:
                snapshot = self._progress.resumed(session_id or session_key, self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.debug("hermex-push: post_approval_response failed", exc_info=True)

    def subagent_start(self, *, child_session_id: Optional[str] = None, **_: Any) -> None:
        if child_session_id:
            with self._lock:
                self._subagent_sessions.remember(child_session_id, True)

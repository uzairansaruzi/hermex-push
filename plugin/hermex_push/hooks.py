"""Hook callbacks. One :class:`HermexPush` instance owns the per-session state that turns
hermes-agent lifecycle hooks into relay events.

Per turn: ``pre_llm_call`` records the platform and starts progress, ``post_llm_call`` captures
the final reply text, ``on_session_end`` is the single boundary that sends exactly one ``reply``
or ``turn_error`` (or nothing, for an interrupted turn). Approvals come from the
``pre_approval_request`` observer, questions from ``pre_tool_call`` on the ``clarify`` tool.
Every callback swallows its own errors: a push must never break a turn. A session whose
platform was never seen gets nothing (fail closed), so an evicted chat-platform session can
never be pushed to the phone by mistake.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import RELAY_URL_ENV
from .keys import Keys, hermes_root, load_or_create_keys
from .payload import notify_event, preview, progress_event
from .privacy import keyed_id
from .progress import HOLD_SECONDS, ProgressCoalescer, ProgressSnapshot
from .relay import RelaySender, allowed_relay_url, notify_url
from .sources import coarse_source, should_notify

logger = logging.getLogger("hermex_push")

CLARIFY_TOOL = "clarify"
MAX_TRACKED_SESSIONS = 4096
RELAY_URL_CACHE_SECONDS = 5.0


def _root_dotenv_value(key: str) -> str:
    """``KEY=VALUE`` from ``<hermes root>/.env`` so profiles inherit the host's relay URL. This
    bypasses profile secret scoping on purpose and must only ever read a non-secret setting."""
    try:
        for line in (hermes_root() / ".env").read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("'\"")
    except Exception:
        pass
    return ""


def relay_url_from_env() -> str:
    """``HERMEX_PUSH_RELAY_URL`` resolved the way hermes-agent resolves any managed credential:
    the active profile scope, then the process environment, then the active home's ``.env``
    (the step hosts such as hermes-webui need, since they never export the Hermes ``.env``).
    A profile without its own value falls back to the root ``.env``. Anything but https (or
    plain http to loopback, for local capture) is refused so the install key never travels in
    the clear."""
    try:
        from hermes_cli.config import get_env_value
        value = get_env_value(RELAY_URL_ENV)
    except Exception:
        value = os.environ.get(RELAY_URL_ENV, "")
    value = (value or "").strip() or _root_dotenv_value(RELAY_URL_ENV)
    if value and not allowed_relay_url(value):
        logger.warning("hermex-push: refusing %s=%r (https required, http only to loopback)", RELAY_URL_ENV, value)
        return ""
    return value


class _Recent(OrderedDict):
    """Bounded insertion-ordered map so long-lived gateways never grow without limit."""

    def remember(self, key: str, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > MAX_TRACKED_SESSIONS:
            self.popitem(last=False)


def clarify_question(args: Optional[dict[str, Any]]) -> str:
    """The first question of a ``clarify`` call, canonical ``questions: [{question}]`` shape
    first, legacy top-level ``question`` second; a batch notes how many more there are."""
    args = args or {}
    questions = args.get("questions")
    if isinstance(questions, list) and questions:
        first = questions[0]
        text = str((first.get("question") if isinstance(first, dict) else first) or "")
        if text and len(questions) > 1:
            text += f" (+{len(questions) - 1} more)"
        if text:
            return text
    return str(args.get("question") or "") or "The agent has a question for you."


class HermexPush:
    def __init__(
        self, *, profile: str = "", sender: Optional[RelaySender] = None,
        keys_loader: Callable[[], Keys] = load_or_create_keys,
        relay_url: Callable[[], str] = relay_url_from_env, now: Callable[[], float] = time.time,
        schedule: Optional[Callable[[float, Callable[[], None]], Any]] = None,
    ) -> None:
        self._profile = "" if profile == "default" else profile
        self._sender = sender or RelaySender()
        self._keys_loader = keys_loader
        self._relay_url = relay_url
        self._relay_url_cache: tuple[float, str] = (float("-inf"), "")
        self._now = now
        self._schedule = schedule or self._timer
        self._lock = threading.RLock()
        self._keys: Optional[Keys] = None
        self._platform_by_session = _Recent()
        self._reply_by_session = _Recent()
        self._subagent_sessions = _Recent()
        self._progress = ProgressCoalescer()
        self._flush_scheduled = False
        self._flush_timer: Optional[threading.Timer] = None
        self._closed = False

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

    def close(self) -> None:
        """Plugin unload: stop the flush timer and the relay thread so a reload leaks nothing."""
        with self._lock:
            self._closed = True
            timer, self._flush_timer = self._flush_timer, None
        if timer is not None:
            timer.cancel()
        self._sender.close()

    def _timer(self, delay: float, fn: Callable[[], None]) -> threading.Timer:
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        timer.start()
        return timer

    def _keys_or_none(self) -> Optional[Keys]:
        if self._keys is None:
            try:
                self._keys = self._keys_loader()
            except Exception:
                logger.warning("hermex-push: keys unavailable; nothing will be sent", exc_info=True)
        return self._keys

    def _relay(self) -> str:
        """Relay URL, re-resolved at most every few seconds so the turn path never re-reads a file."""
        with self._lock:
            resolved_at, url = self._relay_url_cache
            if self._now() - resolved_at < RELAY_URL_CACHE_SECONDS:
                return url
        url = self._relay_url()
        with self._lock:
            self._relay_url_cache = (self._now(), url)
        return url

    def _send(self, event: dict[str, Any]) -> bool:
        url = self._relay()
        keys = self._keys_or_none()
        if not url or keys is None or self._closed:
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
            if self._closed or self._flush_scheduled or not self._progress.has_pending():
                return
            self._flush_scheduled = True
        try:
            timer = self._schedule(HOLD_SECONDS, self.flush_progress)
        except Exception:
            with self._lock:
                self._flush_scheduled = False
            logger.debug("hermex-push: could not schedule a progress flush", exc_info=True)
            return
        with self._lock:
            self._flush_timer = timer if isinstance(timer, threading.Timer) else None

    def flush_progress(self) -> None:
        with self._lock:
            self._flush_scheduled = False
            self._flush_timer = None
            snapshots = self._progress.due(self._now())
        for snapshot in snapshots:
            self._emit_progress(snapshot)
        self._arm_flush()

    # -- hook callbacks (kwargs only; unknown kwargs ignored) ------------------------------

    def _remember_platform(self, session_id: str, platform: str) -> bool:
        """Record a session's platform; True the first time the session is seen."""
        first_sight = session_id not in self._platform_by_session
        if platform:
            self._platform_by_session.remember(session_id, platform)
        return first_sight and bool(platform)

    def pre_llm_call(self, *, session_id: str = "", platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                first_sight = self._remember_platform(session_id, platform)
                self._reply_by_session.pop(session_id, None)
                snapshot = self._progress.turn_started(session_id, self._now())
            if first_sight:  # ids and platform only, never content
                logger.info("hermex-push: session %s platform=%r source=%s", session_id, platform,
                            coarse_source(platform))
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("hermex-push: pre_llm_call failed", exc_info=True)

    def post_llm_call(self, *, session_id: str = "", user_message: Any = "", assistant_response: Any = "",
                      platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                self._remember_platform(session_id, platform)
                self._reply_by_session.remember(session_id, (user_message, assistant_response))
        except Exception:
            logger.warning("hermex-push: post_llm_call failed", exc_info=True)

    def on_session_end(self, *, session_id: str = "", completed: bool = False, interrupted: bool = False,
                       failed: bool = False, turn_id: str = "", turn_exit_reason: str = "",
                       platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                self._remember_platform(session_id, platform)
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
            logger.warning("hermex-push: on_session_end failed; a push was dropped", exc_info=True)

    def pre_tool_call(self, *, tool_name: str = "", args: Optional[dict[str, Any]] = None, session_id: str = "",
                      tool_call_id: str = "", **_: Any) -> None:
        try:
            now = self._now()
            if tool_name == CLARIFY_TOOL:
                with self._lock:
                    snapshot = self._progress.waiting(session_id, now)
                self._emit_progress(snapshot)
                self._notify("clarify", session_id, tool_call_id or f"{tool_name}:{int(now)}", preview(
                    title="Question", body=clarify_question(args), profile=self._profile, request_id=tool_call_id))
                return
            with self._lock:
                snapshot = self._progress.tool_started(session_id, tool_name, now)
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("hermex-push: pre_tool_call failed", exc_info=True)

    def post_tool_call(self, *, tool_name: str = "", session_id: str = "", **_: Any) -> None:
        try:
            with self._lock:
                if tool_name == CLARIFY_TOOL:
                    snapshot = self._progress.resumed(session_id, self._now())
                else:
                    snapshot = self._progress.tool_finished(session_id, self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("hermex-push: post_tool_call failed", exc_info=True)

    def _approval_session(self, session_id: str, session_key: str) -> str:
        """The agent session id when the host bound one, else an opaque id derived from the
        gateway session key, which can embed chat ids or phone numbers and must never go out raw."""
        if session_id:
            return session_id
        keys = self._keys_or_none()
        if not session_key or keys is None:
            return ""
        return keyed_id("session", session_key, install_key=keys.install_key)

    def pre_approval_request(self, *, command: str = "", description: str = "", session_key: str = "",
                             session_id: str = "", surface: str = "", request_id: str = "",
                             tool_call_id: str = "", **_: Any) -> None:
        if surface == "smart":  # auxiliary-model decision, nobody is waiting
            return
        try:
            sid = self._approval_session(session_id, session_key)
            with self._lock:
                snapshot = self._progress.waiting(sid, self._now())
            self._emit_progress(snapshot)
            ref = request_id or tool_call_id or f"approval:{int(self._now())}"
            self._notify("approval", sid, ref, preview(
                title="Approval needed", subtitle=description, body=command, profile=self._profile,
                request_id=request_id))
        except Exception:
            logger.warning("hermex-push: pre_approval_request failed", exc_info=True)

    def post_approval_response(self, *, session_key: str = "", session_id: str = "", **_: Any) -> None:
        try:
            with self._lock:
                snapshot = self._progress.resumed(self._approval_session(session_id, session_key), self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("hermex-push: post_approval_response failed", exc_info=True)

    def subagent_start(self, *, child_session_id: Optional[str] = None, **_: Any) -> None:
        if child_session_id:
            with self._lock:
                self._subagent_sessions.remember(child_session_id, True)

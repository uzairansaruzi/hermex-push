"""Relay-facing event shapes. Pure functions; the only inputs with content are sealed here.

Notification event (``POST /installs/{install_key}/notify``)::

    {"v": 1, "kind": "reply" | "approval" | "clarify" | "turn_error",
     "event_id": <32 hex>, "thread_id": <32 hex>, "collapse_id": <32 hex>,
     "session_id": str, "source": "bot" | "webui" | "other", "is_subagent": bool,
     "sent_at": <unix seconds>, "sealed": <base64> | null}

``sealed`` decrypts to ``{"title", "subtitle", "body", "profile", "request_id", "bot_name"}``.
``bot_name`` is the name the Hermex roster shows for the bot, omitted when unknown; the phone
builds a title in its own language from it and ``kind``, and older builds show the English
``title``. A null ``sealed`` means the host could not encrypt; the phone shows a generic
"New activity" banner.

Progress event (Live Activity state, same route)::

    {"v": 1, "kind": "progress", "event_id", "thread_id", "session_id", "source", "is_subagent",
     "sent_at", "status": "running" | "waiting" | "done" | "failed",
     "tool": <built-in tool name> | null, "tool_calls": int, "started_at": <unix seconds>}

Progress carries no ``sealed`` blob: the relay has to build the activity's content state itself.
It never carries tool arguments or results, only the tool's name.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from .keys import Keys
from .privacy import keyed_id, seal

PAYLOAD_VERSION = 1
NOTIFY_KINDS = frozenset({"reply", "approval", "clarify", "turn_error"})
PROGRESS_STATUSES = frozenset({"running", "waiting", "done", "failed"})

TITLE_CHARS = 80
SUBTITLE_CHARS = 120
BODY_CHARS = 400
# Only a text's head is flattened, and the rest is dropped rather than shown raw: unpaired
# delimiters on one long line make the scan quadratic, and no banner shows more than a few
# hundred characters. The head is long enough for markup-heavy openings (long link URLs).
FLATTEN_CHARS = 8000


def _as_text(value: Any) -> str:
    """Hook payloads are not always strings: a multimodal user message is a list of parts."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            _as_text(part.get("text") if isinstance(part, dict) else part) for part in value
            if not isinstance(part, dict) or part.get("type", "text") == "text"
        )
    if isinstance(value, dict):
        return _as_text(value.get("text") or value.get("content") or "")
    return "" if value is None else str(value)


# Code is stashed behind NUL-delimited indexes first so its content stays verbatim: a fenced
# block (closed, or running to the end of the text) or a one-line span between equal backtick runs.
_CODE = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[^`\n]*\n(?P<block>.*?)(?:^[ \t]*(?P=fence)[`~]*[ \t]*$|\Z)"
    r"|(?<!`)(?P<ticks>`+)(?!`)(?P<span>[^\n]+?)(?<!`)(?P=ticks)(?!`)",
    re.M | re.S,
)
_STASHED = re.compile(r"\x00(\d+)\x00")

# Applied in order to the text around the code. Block markers only count at a line start, so
# this runs while the newlines are still there. Emphasis follows CommonMark flanking: an opener
# is followed by non-space, a closer preceded by non-space (so ``2 * 3 * 4`` stays), a ``*``
# between a word and punctuation is literal (so ``2*(n-1)*k`` and ``f(*args)`` stay), and ``_``
# never opens or closes inside a word (so ``snake_case_name`` stays).
_MARKDOWN = [
    (re.compile(r"^[ \t]*(?:>[ \t]?)+", re.M), ""),  # blockquote markers
    (re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M), ""),  # thematic breaks
    (re.compile(r"^[ \t]*#{1,6}[ \t]+", re.M), ""),  # heading markers; `C#` and `#tag` stay
    (re.compile(r"^[ \t]*[-*+][ \t]+", re.M), ""),  # list bullets
    (re.compile(r"!?\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)"), r"\1"),  # links and images
    # The `*` rules match the literal before checking flanking, so a long line of unpaired
    # asterisks stays cheap to scan.
    (re.compile(r"\*\*(?:(?<![\w*]\*\*)(?=\S)|(?<=\w\*\*)(?=\w))(.+?)"
                r"\*\*(?<=\S\*\*)(?:(?![\w*])|(?<=\w\*\*)(?=\w))"), r"\1"),
    (re.compile(r"(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)"), r"\1"),
    (re.compile(r"(?<!~)~~(?!\s)(.+?)(?<!\s)~~(?!~)"), r"\1"),
    (re.compile(r"\*(?:(?<![\w*]\*)(?=[^\s*])|(?<=\w\*)(?=\w))(.+?)"
                r"\*(?<=[^\s*]\*)(?:(?![\w*])|(?<=\w\*)(?=\w))"), r"\1"),
    (re.compile(r"(?<!\w)_(?![\s_])(.+?)(?<![\s_])_(?!\w)"), r"\1"),
]


def _plain(text: str) -> str:
    """Markdown to the plain text a banner can show: the syntax goes, the words stay."""
    code: list[str] = []

    def stash(match: re.Match[str]) -> str:
        code.append(match["block"] if match["fence"] else match["span"])
        return f"\x00{len(code) - 1}\x00"

    text = _CODE.sub(stash, text.replace("\x00", ""))
    for pattern, replacement in _MARKDOWN:
        text = pattern.sub(replacement, text)
    return _STASHED.sub(lambda match: code[int(match[1])], text)


def _clip(text: Any, limit: int, *, markdown: bool) -> str:
    """Flatten first, then collapse whitespace and clip, so the budget is spent on words."""
    text = _as_text(text)
    if markdown:
        text = _plain(text[:FLATTEN_CHARS])
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def preview(*, title: Any, body: Any, profile: str, subtitle: Any = "", request_id: str = "",
            bot_name: Any = "", markdown: bool = True) -> dict[str, str]:
    """The sealed banner content. iOS banners render plain text, so the subtitle and body are
    flattened from markdown before sealing; ``markdown=False`` leaves text that is not markdown,
    like a shell command, unflattened. The title (the bot's name and a fixed label) and the bot's
    name are never markdown, and an empty name is left out."""
    content = {
        "title": _clip(title, TITLE_CHARS, markdown=False),
        "subtitle": _clip(subtitle, SUBTITLE_CHARS, markdown=markdown),
        "body": _clip(body, BODY_CHARS, markdown=markdown),
        "profile": profile,
        "request_id": request_id,
    }
    name = _clip(bot_name, TITLE_CHARS, markdown=False)
    if name:
        content["bot_name"] = name
    return content


def _identity(keys: Keys, kind: str, session_id: str, event_ref: str) -> dict[str, str]:
    return {
        "event_id": keyed_id("event", f"{kind}:{session_id}:{event_ref}", install_key=keys.install_key),
        "thread_id": keyed_id("thread", session_id, install_key=keys.install_key),
        "collapse_id": keyed_id("collapse", session_id, install_key=keys.install_key),
    }


def notify_event(
    *, kind: str, session_id: str, event_ref: str, source: str, is_subagent: bool, keys: Keys,
    preview: Optional[dict[str, Any]], now: Optional[float] = None,
) -> dict[str, Any]:
    """Build one notification event. ``event_ref`` makes ``event_id`` stable across retries
    (turn id, approval request id, tool call id)."""
    if kind not in NOTIFY_KINDS:
        raise ValueError(f"unknown push kind {kind!r}")
    sealed = None
    if preview is not None:
        sealed = seal(preview, preview_key=keys.preview_key, install_key=keys.install_key)
    return {
        "v": PAYLOAD_VERSION,
        "kind": kind,
        **_identity(keys, kind, session_id, event_ref),
        "session_id": session_id,
        "source": source,
        "is_subagent": bool(is_subagent),
        "sent_at": int(now if now is not None else time.time()),
        "sealed": sealed,
    }


def progress_event(
    *, session_id: str, source: str, is_subagent: bool, keys: Keys, status: str, tool: Optional[str],
    tool_calls: int, started_at: float, now: Optional[float] = None,
) -> dict[str, Any]:
    if status not in PROGRESS_STATUSES:
        raise ValueError(f"unknown progress status {status!r}")
    sent_at = int(now if now is not None else time.time())
    identity = _identity(keys, "progress", session_id, f"{status}:{tool_calls}:{sent_at}")
    return {
        "v": PAYLOAD_VERSION,
        "kind": "progress",
        "event_id": identity["event_id"],
        "thread_id": identity["thread_id"],
        "session_id": session_id,
        "source": source,
        "is_subagent": bool(is_subagent),
        "sent_at": sent_at,
        "status": status,
        "tool": tool or None,
        "tool_calls": int(tool_calls),
        "started_at": int(started_at),
    }

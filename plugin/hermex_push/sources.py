"""Which sessions get a push, and the coarse ``source`` every payload carries.

``platform`` is the agent's platform string. Hermex's Bot connection runs through the Hermes
Desktop backend, whose sessions carry the source the creating client declared: ``ios`` for
sessions phone clients created (observed on the owner's host for a Hermex Bot turn), ``desktop``
or ``tui`` for its own surfaces; hermes-webui constructs its agents with ``platform="webui"``.
Group rooms (``bot_room``) and other clients stay ``other``. Chat platforms that already notify natively are skipped, as
are cron and kanban sessions, which never have a human waiting on a phone.
"""

from __future__ import annotations

BOT_PLATFORMS = frozenset({"ios", "desktop", "tui"})
WEBUI_PLATFORMS = frozenset({"webui"})

# Platforms whose own client already delivers notifications (built-in enum values plus the
# bundled chat plugins). Everything else is pushed.
NATIVE_NOTIFY_PLATFORMS = frozenset({
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal", "mattermost", "matrix",
    "homeassistant", "email", "sms", "dingtalk", "feishu", "wecom", "wecom_callback", "weixin",
    "bluebubbles", "qqbot", "yuanbao", "irc", "line", "teams", "google_chat", "ntfy", "simplex",
    "buzz", "photon", "raft", "a2a",
})

SKIPPED_SESSION_PREFIXES = ("cron_", "kanban_")


def coarse_source(platform: str | None) -> str:
    """``bot`` / ``webui`` / ``other``."""
    name = (platform or "").strip().lower()
    if name in BOT_PLATFORMS:
        return "bot"
    if name in WEBUI_PLATFORMS:
        return "webui"
    return "other"


def should_notify(session_id: str | None, platform: str | None) -> bool:
    """Fail closed: a session whose platform is unknown could be a chat platform that already
    notified the user, so it gets nothing until a turn hook names its platform."""
    if not session_id or session_id.startswith(SKIPPED_SESSION_PREFIXES):
        return False
    name = (platform or "").strip().lower()
    return bool(name) and name not in NATIVE_NOTIFY_PLATFORMS

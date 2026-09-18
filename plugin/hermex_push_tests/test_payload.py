import pytest

from hermex_push.payload import notify_event, preview, progress_event
from hermex_push.privacy import unseal

TOP_LEVEL = {"v", "kind", "event_id", "thread_id", "collapse_id", "session_id", "source", "is_subagent", "sent_at", "sealed"}


def test_notify_event_has_only_opaque_fields_outside_the_seal(keys):
    event = notify_event(
        kind="reply", session_id="sess-1", event_ref="turn-9", source="bot", is_subagent=False, keys=keys,
        preview=preview(title="Hermes", subtitle="what time is it", body="Noon.", profile="default"), now=1_700_000_000,
    )
    assert set(event) == TOP_LEVEL
    assert event["v"] == 1 and event["kind"] == "reply" and event["source"] == "bot"
    assert "Noon" not in str({k: v for k, v in event.items()})
    assert unseal(event["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)["body"] == "Noon."


def test_event_id_is_stable_per_turn_and_thread_per_session(keys):
    a = notify_event(kind="reply", session_id="s", event_ref="t1", source="bot", is_subagent=False, keys=keys, preview=None)
    b = notify_event(kind="reply", session_id="s", event_ref="t1", source="bot", is_subagent=False, keys=keys, preview=None)
    c = notify_event(kind="reply", session_id="s", event_ref="t2", source="bot", is_subagent=False, keys=keys, preview=None)
    assert a["event_id"] == b["event_id"] != c["event_id"]
    assert a["thread_id"] == c["thread_id"] and a["collapse_id"] == c["collapse_id"]
    assert a["sealed"] is None


def test_preview_is_clipped_and_whitespace_normalised():
    p = preview(title="  a\n b ", body="x" * 1000, profile="p")
    assert p["title"] == "a b"
    assert len(p["body"]) == 400 and p["body"].endswith("…")


def test_unknown_kind_and_status_are_rejected(keys):
    with pytest.raises(ValueError):
        notify_event(kind="system", session_id="s", event_ref="r", source="bot", is_subagent=False, keys=keys, preview=None)
    with pytest.raises(ValueError):
        progress_event(session_id="s", source="bot", is_subagent=False, keys=keys, status="idle", tool=None,
                       tool_calls=0, started_at=0)


def test_progress_event_carries_state_but_no_seal(keys):
    event = progress_event(session_id="s", source="webui", is_subagent=True, keys=keys, status="running",
                           tool="terminal", tool_calls=3, started_at=10, now=12)
    assert event["kind"] == "progress" and event["status"] == "running" and event["tool"] == "terminal"
    assert event["tool_calls"] == 3 and event["started_at"] == 10 and event["is_subagent"] is True
    assert "sealed" not in event and "collapse_id" not in event

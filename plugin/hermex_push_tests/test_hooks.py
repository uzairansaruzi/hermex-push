import pytest

from hermex_push.hooks import HermexPush
from hermex_push.privacy import unseal


@pytest.fixture
def push(keys, sender):
    clock = {"now": 1000.0}
    p = HermexPush(profile="work", sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://relay.test/",
                   now=lambda: clock["now"], schedule=lambda delay, fn: None)
    p.clock = clock
    return p


def notifications(sender):
    return [e for _, e in sender.events if e["kind"] != "progress"]


def run_turn(push, session_id="sess-1", platform="desktop", reply="Done.", **end):
    push.pre_llm_call(session_id=session_id, platform=platform, user_message="do it")
    if reply is not None:
        push.post_llm_call(session_id=session_id, user_message="do it", assistant_response=reply, platform=platform)
    push.on_session_end(session_id=session_id, completed=reply is not None, interrupted=False, failed=False,
                        turn_id="turn-1", platform=platform, **end)


def test_one_sealed_reply_per_completed_turn(push, sender, keys):
    run_turn(push)
    events = notifications(sender)
    assert len(events) == 1
    url, event = sender.events[-1]
    assert url == "https://relay.test/installs/" + keys.install_key + "/notify"
    assert event["kind"] == "reply" and event["source"] == "bot" and event["is_subagent"] is False
    assert "Done" not in str(event)
    inner = unseal(event["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert inner == {"title": "work", "subtitle": "do it", "body": "Done.", "profile": "work", "request_id": ""}
    # Progress: turn start and turn end were status changes, so both went through.
    assert [e["status"] for _, e in sender.events if e["kind"] == "progress"] == ["running", "done"]


def test_webui_turns_carry_the_webui_source(push, sender):
    run_turn(push, platform="webui")
    assert notifications(sender)[0]["source"] == "webui"


def test_interrupted_teardown_sends_nothing(push, sender):
    push.pre_llm_call(session_id="s", platform="desktop")
    push.post_llm_call(session_id="s", user_message="q", assistant_response="partial", platform="desktop")
    push.on_session_end(session_id="s", completed=False, interrupted=True, platform="tui")
    assert notifications(sender) == []
    assert [e["status"] for _, e in sender.events] == ["running", "done"]


def test_failed_turn_sends_turn_error(push, sender, keys):
    push.pre_llm_call(session_id="s", platform="desktop")
    push.on_session_end(session_id="s", completed=False, interrupted=False, failed=True, turn_id="t",
                        turn_exit_reason="max_iterations", platform="desktop")
    event = notifications(sender)[0]
    assert event["kind"] == "turn_error"
    assert unseal(event["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)["body"] == "max_iterations"
    assert [e["status"] for _, e in sender.events if e["kind"] == "progress"][-1] == "failed"


def test_native_platform_sessions_are_skipped(push, sender):
    run_turn(push, platform="telegram")
    assert sender.events == []


def test_clarify_and_approval_push_and_mark_waiting(push, sender, keys):
    push.pre_llm_call(session_id="s", platform="desktop")
    push.pre_tool_call(tool_name="clarify", args={"questions": [{"question": "Which branch?", "choices": ["main", "dev"]}]},
                       session_id="s", tool_call_id="call-1")
    push.pre_approval_request(command="rm -rf build", description="Delete build dir", session_id="s", session_key="s",
                              surface="cli", request_id="req-1")
    kinds = [e["kind"] for e in notifications(sender)]
    assert kinds == ["clarify", "approval"]
    clarify, approval = notifications(sender)
    assert unseal(clarify["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)["body"] == "Which branch?"
    inner = unseal(approval["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert inner["body"] == "rm -rf build" and inner["request_id"] == "req-1"
    assert "rm -rf" not in str(approval)
    assert [e["status"] for _, e in sender.events if e["kind"] == "progress"] == ["running", "waiting"]
    push.post_approval_response(session_id="s", session_key="s", choice="once")
    assert [e["status"] for _, e in sender.events if e["kind"] == "progress"][-1] == "running"


def test_clarify_question_shapes():
    from hermex_push.hooks import clarify_question
    assert clarify_question({"questions": [{"question": "A?"}, {"question": "B?"}]}) == "A? (+1 more)"
    assert clarify_question({"question": "Legacy?"}) == "Legacy?"
    assert clarify_question({}) == "The agent has a question for you."


def test_unknown_platform_gets_nothing_and_session_keys_never_go_out_raw(push, sender, keys):
    push.pre_approval_request(command="ls", description="", session_key="default:whatsapp:dm:15551234567@c.us",
                              surface="cli")
    assert sender.events == []  # platform never seen: fail closed
    push.pre_llm_call(session_id="known", platform="desktop")
    push.pre_approval_request(command="ls", description="", session_id="known",
                              session_key="default:whatsapp:dm:15551234567@c.us", surface="cli")
    assert [e["kind"] for e in notifications(sender)] == ["approval"]
    assert "15551234567" not in str(sender.events)


def test_multimodal_user_message_still_pushes(push, sender, keys):
    parts = [{"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": "data:..."}}]
    push.pre_llm_call(session_id="s", platform="desktop")
    push.post_llm_call(session_id="s", user_message=parts, assistant_response="A cat.", platform="desktop")
    push.on_session_end(session_id="s", completed=True, interrupted=False, turn_id="t", platform="desktop")
    event = notifications(sender)[0]
    inner = unseal(event["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert inner["subtitle"] == "what is this" and inner["body"] == "A cat."


def test_seal_failure_sends_a_content_free_event(keys, sender, monkeypatch):
    import hermex_push.payload as payload
    monkeypatch.setattr(payload, "seal", lambda *a, **k: None)
    p = HermexPush(sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://r", schedule=lambda d, f: None)
    run_turn(p, reply="SECRET-REPLY-8f3a")
    event = notifications(sender)[0]
    assert event["sealed"] is None and "SECRET-REPLY-8f3a" not in str(sender.events)


def test_no_event_of_any_kind_carries_plaintext(push, sender):
    import json
    marker = "zq9v-marker-7f2e1c"
    push.pre_llm_call(session_id="s", platform="desktop", user_message=marker)
    push.pre_tool_call(tool_name="terminal", args={"command": marker}, session_id="s", tool_call_id="c1")
    push.post_tool_call(tool_name="terminal", result=marker, session_id="s")
    push.pre_tool_call(tool_name="clarify", args={"questions": [{"question": marker}]}, session_id="s", tool_call_id="c2")
    push.pre_approval_request(command=marker, description=marker, session_id="s", surface="cli", request_id="r")
    push.post_llm_call(session_id="s", user_message=marker, assistant_response=marker, platform="desktop")
    push.on_session_end(session_id="s", completed=True, interrupted=False, turn_id="t", platform="desktop")
    push.clock["now"] += 2
    push.flush_progress()
    assert len(sender.events) >= 6
    assert marker not in json.dumps([e for _, e in sender.events])


def test_default_profile_titles_as_hermes(keys, sender):
    p = HermexPush(profile="default", sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://r",
                   schedule=lambda d, f: None)
    run_turn(p)
    inner = unseal(notifications(sender)[0]["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert inner["title"] == "Hermes" and inner["profile"] == ""


def test_scheduler_failure_does_not_wedge_flushing(keys, sender):
    calls = {"n": 0}

    def schedule(delay, fn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no loop")
    p = HermexPush(sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://r", schedule=schedule)
    p.pre_llm_call(session_id="s", platform="desktop")
    p.pre_tool_call(tool_name="terminal", args={}, session_id="s")  # held; scheduling fails
    p.post_tool_call(tool_name="terminal", session_id="s")  # held again; scheduling retried
    assert calls["n"] == 2


def test_close_stops_the_relay_thread():
    from hermex_push.relay import RelaySender
    sender = RelaySender(post=lambda u, b: 200)
    p = HermexPush(sender=sender, keys_loader=lambda: None, relay_url=lambda: "", schedule=lambda d, f: None)
    sender.enqueue("https://r/installs/k/notify", {"kind": "reply"})
    sender.wait_idle()
    p.close()
    assert not sender._thread.is_alive()


def test_smart_approvals_do_not_push(push, sender):
    push.pre_approval_request(command="ls", description="", session_key="s", surface="smart")
    assert sender.events == []


def test_subagent_sessions_are_flagged(push, sender):
    push.subagent_start(parent_session_id="p", child_session_id="child-1")
    run_turn(push, session_id="child-1")
    assert notifications(sender)[0]["is_subagent"] is True


def test_routine_tool_progress_is_coalesced_then_flushed(push, sender):
    push.pre_llm_call(session_id="s", platform="desktop")
    push.pre_tool_call(tool_name="terminal", args={"command": "secret"}, session_id="s")
    push.post_tool_call(tool_name="terminal", session_id="s")
    assert len(sender.events) == 1  # only the turn start went through
    push.clock["now"] += 1.5
    push.flush_progress()
    assert len(sender.events) == 2
    last = sender.events[-1][1]
    assert last["tool_calls"] == 1 and "secret" not in str(last)


def test_nothing_is_sent_without_a_relay_url_or_keys(keys, sender):
    p = HermexPush(sender=sender, keys_loader=lambda: keys, relay_url=lambda: "", schedule=lambda d, f: None)
    run_turn(p)
    assert sender.events == []

    def broken():
        raise OSError("no home")
    p = HermexPush(sender=sender, keys_loader=broken, relay_url=lambda: "https://r", schedule=lambda d, f: None)
    run_turn(p)
    assert sender.events == []


def test_profile_without_its_own_relay_url_inherits_the_root_dotenv(hermes_home, monkeypatch):
    from hermex_push.hooks import relay_url_from_env
    (hermes_home / ".env").write_text("OTHER=1\nHERMEX_PUSH_RELAY_URL=https://root.test/\n")
    profile = hermes_home / "profiles" / "dev"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert relay_url_from_env() == "https://root.test/"
    monkeypatch.setenv("HERMEX_PUSH_RELAY_URL", "https://env.test")
    assert relay_url_from_env() == "https://env.test"


def test_relay_url_from_env_refuses_cleartext_to_remote_hosts(monkeypatch):
    from hermex_push.hooks import relay_url_from_env
    monkeypatch.setenv("HERMEX_PUSH_RELAY_URL", "http://relay.example")
    assert relay_url_from_env() == ""
    monkeypatch.setenv("HERMEX_PUSH_RELAY_URL", "https://relay.example")
    assert relay_url_from_env() == "https://relay.example"

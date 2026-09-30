import sys
import types

import pytest

from hermex_push.hooks import HermexPush, bot_name_for
from hermex_push.privacy import unseal


@pytest.fixture
def push(keys, sender):
    clock = {"now": 1000.0}
    p = HermexPush(profile="work", sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://relay.test/",
                   now=lambda: clock["now"], schedule=lambda delay, fn: None, bot_name=lambda: "Inbox Triage")
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
    assert inner == {"title": "Inbox Triage", "subtitle": "do it", "body": "Done.", "profile": "work",
                     "request_id": "", "bot_name": "Inbox Triage"}
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


def test_questions_are_flattened_but_approval_commands_stay_verbatim(push, sender, keys):
    command = "rm -rf __pycache__ && find . -name '*.md' -o -name '*.txt'"
    push.pre_llm_call(session_id="s", platform="desktop")
    push.pre_tool_call(tool_name="clarify", args={"question": "Deploy **now**?"}, session_id="s", tool_call_id="c")
    push.pre_approval_request(command=command, description="", session_id="s", surface="cli", request_id="r")
    clarify, approval = (unseal(e["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
                         for e in notifications(sender))
    assert clarify["body"] == "Deploy now?" and approval["body"] == command


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
    push.clock["now"] += 5
    push.flush_progress()
    assert len(sender.events) >= 6
    relayed = json.dumps([e for _, e in sender.events])
    assert marker not in relayed and "Inbox Triage" not in relayed


def test_every_banner_names_the_bot(push, sender, keys):
    push.pre_llm_call(session_id="s", platform="desktop")
    push.pre_approval_request(command="ls", description="", session_id="s", surface="cli", request_id="r")
    push.pre_tool_call(tool_name="clarify", args={"question": "Which?"}, session_id="s", tool_call_id="c")
    push.on_session_end(session_id="s", completed=False, interrupted=False, failed=True, turn_id="t1",
                        platform="desktop")
    run_turn(push, session_id="s")
    sealed = {e["kind"]: unseal(e["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
              for e in notifications(sender)}
    assert {kind: (inner["title"], inner["bot_name"]) for kind, inner in sealed.items()} == {
        "approval": ("Inbox Triage · Approval needed", "Inbox Triage"),
        "clarify": ("Inbox Triage · Question", "Inbox Triage"),
        "turn_error": ("Inbox Triage · Turn failed", "Inbox Triage"),
        "reply": ("Inbox Triage", "Inbox Triage"),
    }


def test_bot_name_follows_the_roster_chain(monkeypatch, tmp_path):
    """Desktop title, then display_name, then the slug, and "Hermes" for the default Profile."""
    metas = {
        "inbox": {"bot_title": "Inbox Triage", "display_name": "Inbox"},
        "ops": {"bot_title": "", "display_name": "Ops Desk"},
        "bare": {"bot_title": "", "display_name": ""},
        "default": {"bot_title": "", "display_name": ""},
    }
    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.get_profile_dir = lambda name: tmp_path / name
    profiles.read_profile_meta = lambda profile_dir: metas[profile_dir.name]
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)
    assert [bot_name_for(p) for p in ("inbox", "ops", "bare", "")] == ["Inbox Triage", "Ops Desk", "bare", "Hermes"]
    metas["default"] = {"bot_title": "", "display_name": "Main"}
    assert bot_name_for("") == "Main"
    # A host without the helpers, or one whose read fails, falls back to the slug.
    profiles.read_profile_meta = lambda profile_dir: 1 / 0
    assert [bot_name_for(p) for p in ("inbox", "")] == ["inbox", "Hermes"]


def test_default_profile_titles_as_hermes(keys, sender):
    p = HermexPush(profile="default", sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://r",
                   schedule=lambda d, f: None)
    run_turn(p)
    inner = unseal(notifications(sender)[0]["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert inner["title"] == "Hermes" and inner["bot_name"] == "Hermes" and inner["profile"] == ""


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
    sender = RelaySender(post=lambda u, b: (200, "accepted"))
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


def test_routine_tool_progress_is_coalesced_then_flushed(keys, sender):
    clock, delays = {"now": 1000.0}, []
    push = HermexPush(sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://r",
                      now=lambda: clock["now"], schedule=lambda delay, fn: delays.append(delay))
    push.pre_llm_call(session_id="s", platform="desktop")
    clock["now"] += 3
    push.pre_tool_call(tool_name="terminal", args={"command": "secret"}, session_id="s")
    push.post_tool_call(tool_name="terminal", session_id="s")
    assert len(sender.events) == 1  # only the turn start went through
    assert delays == [2.0]  # one flush, when the turn start's five-second hold ends
    clock["now"] += 2
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


def test_a_session_whose_hold_ends_first_is_not_kept_waiting_by_another(keys, sender):
    clock, delays = {"now": 1000.0}, []
    push = HermexPush(sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://r",
                      now=lambda: clock["now"], schedule=lambda delay, fn: delays.append(delay))
    push.pre_llm_call(session_id="b", platform="desktop")
    clock["now"] = 1002.0
    push.pre_llm_call(session_id="a", platform="desktop")
    clock["now"] = 1003.0
    push.pre_tool_call(tool_name="terminal", session_id="a")  # held until 1007
    clock["now"] = 1003.5
    push.pre_tool_call(tool_name="terminal", session_id="b")  # held until 1005
    assert delays == [4.0, 1.5]
    clock["now"] = 1005.0
    push.flush_progress()
    assert [e["session_id"] for _, e in sender.events] == ["b", "a", "b"]
    assert delays == [4.0, 1.5, 2.0]  # re-armed for a's hold


def test_an_unwatched_turn_sends_routine_progress_once_a_minute_after_the_grace(keys, sender):
    clock = {"now": 1000.0}
    push = HermexPush(sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://r",
                      now=lambda: clock["now"], schedule=lambda delay, fn: None)
    sender.result = "no_activity"  # no phone shows a Live Activity for this session
    push.pre_llm_call(session_id="s", platform="cli")
    for second in range(1, 170):  # a tool boundary every second
        clock["now"] = 1000.0 + second
        push.pre_tool_call(tool_name="terminal", session_id="s")
    push.pre_approval_request(command="ls", description="", session_id="s", surface="cli", request_id="r")
    progress = [(e["sent_at"] - 1000, e["status"]) for _, e in sender.events if e["kind"] == "progress"]
    # Five-second cadence through the 30 s grace, then one routine update a minute; waiting goes out at once.
    assert progress == [(t, "running") for t in (0, 5, 10, 15, 20, 25, 30, 90, 150)] + [(169, "waiting")]
    assert [e["kind"] for e in notifications(sender)] == ["approval"]


def test_a_status_change_probes_and_a_watched_answer_restores_the_cadence(keys, sender):
    clock = {"now": 1000.0}
    push = HermexPush(sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://r",
                      now=lambda: clock["now"], schedule=lambda delay, fn: None)
    sender.result = "no_activity"
    push.pre_llm_call(session_id="s", platform="desktop")
    clock["now"] = 1030.0
    push.pre_tool_call(tool_name="terminal", session_id="s")  # quiet until 1090
    clock["now"] = 1040.0
    push.pre_tool_call(tool_name="read_file", session_id="s")  # held
    sender.result = "accepted"  # the phone has registered its activity
    clock["now"] = 1041.0
    push.pre_approval_request(command="ls", description="", session_id="s", surface="cli", request_id="r")
    clock["now"] = 1042.0
    push.post_approval_response(session_id="s")
    clock["now"] = 1047.0
    push.pre_tool_call(tool_name="search", session_id="s")  # five seconds after the last send
    progress = [(e["sent_at"] - 1000, e["status"]) for _, e in sender.events if e["kind"] == "progress"]
    assert progress == [(0, "running"), (30, "running"), (41, "waiting"), (42, "running"), (47, "running")]

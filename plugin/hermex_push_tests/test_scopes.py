"""One install serving every profile through the wrapped plugin manager (hermex#634)."""
import pytest

from hermex_push.hooks import HermexPush
from hermex_push.privacy import unseal
from hermex_push.scopes import serve_every_profile


class DispatchMixin:
    """The shape of hermes-agent's dispatch methods: inherited, not defined on the manager."""

    def invoke_hook(self, hook_name, **kwargs):
        return [callback(**kwargs) for callback in self.hooks.get(hook_name, [])]

    def has_hook(self, hook_name):
        return bool(self.hooks.get(hook_name))

    def iter_hook_callbacks(self, hook_name):
        return tuple(self.hooks.get(hook_name, ()))


def manager_init(self, own_copy=None):
    """A profile manager, optionally with its own loaded copy of the plugin."""
    self.hooks = {name: [callback] for name, callback in (own_copy.hook_callbacks() if own_copy else {}).items()}


@pytest.fixture
def served(keys, sender):
    """A manager class wrapped for the active profile in ``state``; undone after the test."""
    state = {"profile": "code-man", "pushes": []}

    def make_push(profile):
        push = HermexPush(profile=profile, sender=sender, keys_loader=lambda: keys,
                          relay_url=lambda: "https://relay.test/", schedule=lambda delay, fn: None)
        state["pushes"].append(push)
        return push

    manager_class = type("PluginManager", (DispatchMixin,), {"__init__": manager_init})
    state["undo"] = serve_every_profile(manager_class, lambda: state["profile"], make_push)
    state["class"] = manager_class
    yield state
    if state["undo"]:
        state["undo"]()


def run_turn(manager, session_id):
    manager.invoke_hook("pre_llm_call", session_id=session_id, platform="tui")
    manager.invoke_hook("post_llm_call", session_id=session_id, platform="tui", user_message="hi",
                        assistant_response="A story.")
    manager.invoke_hook("on_session_end", session_id=session_id, platform="tui", completed=True, turn_id="t1")


def test_a_profile_without_its_own_copy_pushes_under_its_own_name(served, sender, keys):
    run_turn(served["class"](), "s-code-man")
    assert [e["kind"] for _, e in sender.events] == ["progress", "progress", "reply"]
    reply = sender.events[-1][1]
    assert reply["source"] == "bot" and reply["session_id"] == "s-code-man"
    assert unseal(reply["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)["profile"] == "code-man"


def test_a_profile_with_its_own_copy_is_not_served_twice(served, sender, keys):
    own = HermexPush(profile="dev", sender=sender, keys_loader=lambda: keys,
                     relay_url=lambda: "https://relay.test/", schedule=lambda delay, fn: None)
    served["profile"] = "dev"
    run_turn(served["class"](own_copy=own), "s-dev")
    assert [e["kind"] for _, e in sender.events] == ["progress", "progress", "reply"]
    assert served["pushes"] == []


def test_each_profile_keeps_its_own_push_state(served):
    manager = served["class"]()
    run_turn(manager, "s-1")
    served["profile"] = "inbox-triage"
    run_turn(manager, "s-2")
    run_turn(manager, "s-3")
    assert [push._profile for push in served["pushes"]] == ["code-man", "inbox-triage"]


def test_gated_hooks_report_a_listener_and_others_keep_their_answer(served):
    manager = served["class"]()
    assert manager.has_hook("post_tool_call") is True
    assert manager.has_hook("pre_api_request") is False


def test_only_the_first_copy_wraps_the_class(served):
    assert serve_every_profile(served["class"], lambda: "other") is None


def test_undo_restores_the_inherited_methods_and_closes_served_pushes(served):
    manager_class = served["class"]
    run_turn(manager_class(), "s-1")
    served["undo"]()
    served["undo"] = None
    assert "invoke_hook" not in manager_class.__dict__ and "has_hook" not in manager_class.__dict__
    assert manager_class().has_hook("post_tool_call") is False
    assert served["pushes"][0]._closed is True


def test_a_host_without_the_dispatch_methods_is_left_alone():
    bare = type("PluginManager", (), {})
    assert serve_every_profile(bare, lambda: "code-man") is None
    assert bare.__dict__.get("_hermex_push_serves_every_profile") is None


def test_a_failing_push_never_breaks_the_hosts_hooks(served):
    manager = served["class"]()
    manager.hooks = {"pre_llm_call": [lambda **_: "host result"]}

    def broken(profile):
        raise RuntimeError("no keys")

    served["undo"]()
    served["undo"] = serve_every_profile(served["class"], lambda: "code-man", broken)
    assert manager.invoke_hook("pre_llm_call", session_id="s", platform="tui") == ["host result"]

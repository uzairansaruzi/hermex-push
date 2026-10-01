"""The plugin entry point, the pairing and restart routes, the pinned install identifier and the
version."""
import importlib.util
import json
import os
import re
import sys
import threading
import types
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermex_push import INSTALL_IDENTIFIER, PLUGIN_NAME, PLUGIN_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    profile_name = "work"

    def __init__(self):
        self.platform = None
        self.hooks = {}

    def register_platform(self, **kwargs):
        self.platform = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def on_unload(self, callback):
        self.unload = callback


def test_install_identifier_and_manifest_names_agree():
    git_url, _, subdir = INSTALL_IDENTIFIER.partition(".git/")
    assert git_url + ".git" == "https://github.com/uzairansaruzi/hermex-push.git" and subdir == "plugin"
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["name"] == PLUGIN_NAME and manifest["kind"] == "platform"
    import json
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text())["name"] == PLUGIN_NAME


def test_declared_versions_match_the_loaded_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", PLUGIN_VERSION)
    pyproject = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M)
    assert {
        "plugin.yaml": str(yaml.safe_load((ROOT / "plugin.yaml").read_text())["version"]),
        "pyproject.toml": pyproject and pyproject[1],
        "dashboard/manifest.json": json.loads((ROOT / "dashboard" / "manifest.json").read_text())["version"],
    } == dict.fromkeys(["plugin.yaml", "pyproject.toml", "dashboard/manifest.json"], PLUGIN_VERSION)


def test_register_declares_platform_and_every_manifest_hook():
    plugin = _load(ROOT / "__init__.py", "hermex_push_plugin_under_test")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.platform["name"] == "hermex" and ctx.platform["required_env"] == ["HERMEX_PUSH_RELAY_URL"]
    assert ctx.platform["pii_safe"] is True
    declared = set(yaml.safe_load((ROOT / "plugin.yaml").read_text())["provides_hooks"])
    assert set(ctx.hooks) == declared
    ctx.unload()  # registered and callable


def test_pairing_route_returns_keys_only_with_a_relay_url(hermes_home, monkeypatch):
    api = _load(ROOT / "dashboard" / "plugin_api.py", "hermex_push_pairing_under_test")
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermex-push")
    client = TestClient(app)

    assert client.get("/api/plugins/hermex-push/pairing").status_code == 409

    monkeypatch.setenv("HERMEX_PUSH_RELAY_URL", "https://relay.test")
    response = client.get("/api/plugins/hermex-push/pairing")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["relay_url"] == "https://relay.test" and body["platform"] == "hermex" and body["payload_version"] == 1
    assert body["plugin_version"] == PLUGIN_VERSION
    assert len(body["install_key"]) == 64
    import base64
    assert len(base64.b64decode(body["preview_key"])) == 32
    assert client.get("/api/plugins/hermex-push/pairing").json() == body


def _restart_api(monkeypatch, calls):
    """The route module, with ``threading.Timer`` held until a test fires it, ``os.execv``
    recorded, and the dashboard's two SIGTERM steps as the dashboard process has them loaded."""
    timers = []

    class HeldTimer:
        def __init__(self, interval, function):
            self.interval, self.function, self.daemon, self.started = interval, function, False, False
            timers.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(threading, "Timer", HeldTimer)
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append(("execv", path, list(argv))))
    server = types.ModuleType("tui_gateway.server")
    server._stop_turns_before_exit = lambda: calls.append("stop turns")
    server._flush_sessions_before_exit = lambda: calls.append("flush sessions")
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    return _load(ROOT / "dashboard" / "plugin_api.py", "hermex_push_restart_under_test"), timers


def test_restart_answers_202_then_reexecs_the_dashboard_with_its_own_command_line(monkeypatch):
    calls = []
    api, timers = _restart_api(monkeypatch, calls)
    argv = ["/opt/hermes/bin/python3", "-m", "hermes_cli.main", "dashboard", "--port", "9119"]
    monkeypatch.setattr(sys, "executable", "/opt/hermes/bin/python3")
    monkeypatch.setattr(sys, "orig_argv", argv)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermex-push")
    client = TestClient(app)

    response = client.post("/api/plugins/hermex-push/restart")

    assert response.status_code == 202 and response.json() == {"ok": True}
    assert calls == [], "Nothing stops before the answer is on its way"
    assert [(t.interval, t.daemon, t.started) for t in timers] == [(1.0, True, True)]
    assert client.post("/api/plugins/hermex-push/restart").status_code == 202
    assert len(timers) == 1, "A second tap joins the pending restart"
    assert client.get("/api/plugins/hermex-push/restart").status_code == 405, "Never a GET a link could trigger"

    timers[0].function()

    assert calls == ["stop turns", "flush sessions", ("execv", "/opt/hermes/bin/python3", argv)]


def test_restart_needs_the_dashboard_sign_in(monkeypatch):
    """Against the real dashboard, when hermes-agent is importable: its auth middleware answers
    401 before the route runs, on a loopback bind and behind the password gate alike."""
    web_server = pytest.importorskip("hermes_cli.web_server")
    calls = []
    api, timers = _restart_api(monkeypatch, calls)
    web_server.app.include_router(api.router, prefix="/api/plugins/hermex-push")
    client = TestClient(web_server.app, base_url="http://127.0.0.1")

    for gated in (False, True):
        monkeypatch.setattr(web_server.app.state, "auth_required", gated, raising=False)
        assert client.post("/api/plugins/hermex-push/restart").status_code == 401, f"gated={gated}"

    assert timers == [] and calls == []

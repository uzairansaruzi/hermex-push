"""The plugin entry point, the pairing route and the pinned install identifier."""
import importlib.util
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermex_push import INSTALL_IDENTIFIER, PLUGIN_NAME

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


def test_install_identifier_and_manifest_names_agree():
    git_url, _, subdir = INSTALL_IDENTIFIER.partition(".git/")
    assert git_url + ".git" == "https://github.com/uzairansaruzi/hermex-push.git" and subdir == "plugin"
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["name"] == PLUGIN_NAME and manifest["kind"] == "platform"
    import json
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text())["name"] == PLUGIN_NAME


def test_register_declares_platform_and_every_manifest_hook():
    plugin = _load(ROOT / "__init__.py", "hermex_push_plugin_under_test")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.platform["name"] == "hermex" and ctx.platform["required_env"] == ["HERMEX_PUSH_RELAY_URL"]
    assert ctx.platform["pii_safe"] is True
    declared = set(yaml.safe_load((ROOT / "plugin.yaml").read_text())["provides_hooks"])
    assert set(ctx.hooks) == declared


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
    assert len(body["install_key"]) == 64
    import base64
    assert len(base64.b64decode(body["preview_key"])) == 32
    assert client.get("/api/plugins/hermex-push/pairing").json() == body

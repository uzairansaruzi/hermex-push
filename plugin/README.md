# hermex-push plugin

A `hermes-agent` platform plugin (`hermex`) that posts sealed events to a hermex-push relay on
each turn boundary. Design: [hermex#490](https://github.com/uzairansaruzi/hermex/issues/490).
Slice: [hermex#555](https://github.com/uzairansaruzi/hermex/issues/555).

Install identifier the app sends: `https://github.com/uzairansaruzi/hermex-push.git/plugin`
(hermes-agent splits it at `.git/` into the clone URL and the `plugin` subdirectory). Enable
with `hermes plugins enable hermex-push`, set `HERMEX_PUSH_RELAY_URL`, restart the gateway.

## Fact checks (hermes-agent v0.21.2 = tag `v2026.9.11`, re-read at v0.21.3)

1. **The turn-end hook fires for webui turns.** hermes-webui runs `AIAgent` in its own process
   with `platform="webui"` (`api/streaming.py`), and every `run_conversation()` ends in
   `agent/turn_finalizer.py`, which invokes `on_session_end`. `invoke_hook` lazily discovers
   plugins, so hooks fire in any process that shares the Hermes home and has the plugin in
   `plugins.enabled`. Hermex's Bot connection runs through the Hermes Desktop backend
   (`tui_gateway`); its sessions carry the source the creating client declared, `ios` for
   phone-created sessions (observed on the owner's host), `desktop` or `tui` for its own
   surfaces. The plugin maps `ios`/`desktop`/`tui` to `source: bot`, `webui` to
   `source: webui`, anything else (including `bot_room` group chats) to `other`.
   No plugin change is needed for [hermex#561](https://github.com/uzairansaruzi/hermex/issues/561).
   One caveat: the Desktop backend also fires `on_session_end` with `interrupted=True` when a
   socket drops; the plugin sends nothing for interrupted turns, so a turn never pushes twice.
2. **A `kind: platform` plugin can mount a dashboard route.** Dashboard discovery scans
   `<plugins>/*/dashboard/manifest.json` without reading `plugin.yaml`, and
   `_mount_plugin_api_routes` mounts the manifest's `api` file at `/api/plugins/<name>/` once
   the plugin is in `plugins.enabled`. All `/api/` routes sit behind the dashboard's auth
   middleware. So `GET /api/plugins/hermex-push/pairing` works and no QR fallback is needed.
3. **Approvals use the observer hooks, not `register_approval_transport`.** Both exist on
   0.21.2, but a registered transport *replaces* every built-in prompt surface once selected
   by `security.approval.transport`, which would take approvals away from the Bot UI that
   answers them. `pre_approval_request` / `post_approval_response` are in `VALID_HOOKS` and
   observe every surface, so the plugin uses those. Questions come from `pre_tool_call` on the
   `clarify` tool. The `hermex` platform is loaded eagerly (only bundled platforms defer), so
   the hooks are live in every process.

**Profiles.** Sessions run under a Hermes profile scope (`<root>/profiles/<name>`), and each
profile has its own plugin directory, `plugins.enabled` list and `.env`. The plugin must be
installed and enabled in every profile that should push (Cadu is installed the same way);
the app's install flow in [hermex#557](https://github.com/uzairansaruzi/hermex/issues/557)
has to target the profile the user runs. Keys live at the root so every profile pairs as one
host, and a profile without `HERMEX_PUSH_RELAY_URL` inherits the root `.env` value.

## Validated on the owner's host (2026-09-18, hermes-agent 0.21.3)

CLI, webui and Bot turns each produced exactly one sealed `reply` plus `running`/`done`
progress events against a local capture endpoint, alongside Cadu. The captured bodies
contained ciphertext only. The pairing route answered 401 without a login and returned the
keys through the dashboard login.

## What it sends

`POST {relay_url}/installs/{install_key}/notify`, JSON. Ciphertext only; the relay never sees
a title or body. See `hermex_push/payload.py` for the authoritative shapes.

Notification (`kind` is `reply`, `approval`, `clarify` or `turn_error`):

```json
{"v": 1, "kind": "reply", "event_id": "<32 hex>", "thread_id": "<32 hex>", "collapse_id": "<32 hex>",
 "session_id": "…", "source": "bot", "is_subagent": false, "sent_at": 1726600000, "sealed": "<base64>"}
```

`sealed` is base64 of `nonce(12) || AES-256-GCM ciphertext || tag(16)` over the UTF-8 JSON
`{"title","subtitle","body","profile","request_id"}`, AAD `hermex-preview-v1:<sha256 hex of
install_key>`. A `null` seal means encryption failed on the host; the phone shows a generic
banner. `event_id` is stable per turn or request for relay dedupe; `thread_id` and
`collapse_id` are stable per session. All three are HMAC-SHA256 keyed from the install key.

Progress (Live Activity state, one per session per second, status changes always through):

```json
{"v": 1, "kind": "progress", "event_id": "…", "thread_id": "…", "session_id": "…", "source": "bot",
 "is_subagent": false, "sent_at": 1726600000, "status": "running", "tool": "terminal", "tool_calls": 3,
 "started_at": 1726599990}
```

Progress carries the tool's name only, never arguments or results.

## Pairing

`GET /api/plugins/hermex-push/pairing` (dashboard auth) returns
`{"relay_url", "install_key", "preview_key", "platform": "hermex", "payload_version": 1}`.
`preview_key` is base64 of 32 bytes. Both keys are generated on first use under a file lock at
`<hermes_home>/plugins/hermex-push/{install_key,preview_key}` (mode 0600) and never rotated
behind a paired device. The route answers 409 while `HERMEX_PUSH_RELAY_URL` is unset.

## Layout

- `__init__.py` registers the `hermex` platform and the hooks.
- `hermex_push/keys.py` key files; `privacy.py` sealing and keyed ids; `payload.py` event shapes;
  `sources.py` coarse source and skip rules; `progress.py` coalescing; `relay.py` background POST;
  `hooks.py` hook callbacks; `adapter.py` the send-only platform adapter.
- `dashboard/plugin_api.py` the pairing route; `dashboard/dist/index.js` a no-op the SPA requires.

Cron `deliver: hermex` and the `input`, `delivery` and `system` kinds are V1.1
([hermex#566](https://github.com/uzairansaruzi/hermex/issues/566)).

## Development

```sh
uv venv .venv && uv pip install --python .venv/bin/python pytest cryptography fastapi httpx pyyaml
.venv/bin/python -m pytest plugin -q
```

# Hermex relay

A Cloudflare Worker that relays the [plugin's sealed events](../plugin/README.md) to paired iPhones. Implements [hermex#556](https://github.com/uzairansaruzi/hermex/issues/556). Nothing here provisions or deploys itself during tests.

## Run locally

Requires Node 22 or newer (CI uses Node 24).

```sh
cd relay
npm ci
npm run check
npm run deploy:dry-run
npm run dev
```

Tests execute in Cloudflare's Workers runtime with local KV and Durable Objects. APNs calls are mocked; JWT tests generate disposable P-256 keys and verify the signatures with WebCrypto. No Apple or Cloudflare credentials are needed. A local dev server can use a gitignored `.dev.vars` containing `APNS_PRIVATE_KEY`, but tests do not need it. Never use a real install key in local URLs or logs.

## Storage and coordination

The public Worker validates requests, hashes the 64-character lowercase hexadecimal install capability with SHA-256, and forwards only the hash and parsed command to one Durable Object per install. The raw capability is never persisted or forwarded to APNs.

Device records live in the `hermex_relay` KV binding under `installs:<sha256(install_key)>:devices:<device_token>:<revision>`. Immutable revisions avoid KV's one-write-per-key-per-second limit. The Durable Object stores the authoritative device-to-revision index, activity tokens/timing, and ten-minute event receipts. Registry updates replace the pointer before removing the previous revision; deletion removes the pointer before deleting KV data. Fan-out never uses an eventually consistent KV list. An unavailable referenced KV record fails closed with a retryable 503.

Durable Objects are necessary because [KV is eventually consistent and limits writes to the same key](https://developers.cloudflare.com/kv/api/write-key-value-pairs/). They serialize registration, revocation, notification delivery, and alarms within each install, including concurrent requests at different edges. Activity coalescing uses persistent alarms so held updates survive object eviction. There is no separate server to operate.

Limits per install: 32 devices, 64 activity registrations per device, 4,096 event receipts during a ten-minute window. Activity registrations expire after eight hours; end markers after fifteen minutes. Alarms clean up expired state. A Worker interruption between a KV write and its index update can leave an unused KV revision; it cannot restore a revoked device. Such orphaned revisions may be removed during owner maintenance by comparing KV records with the durable index.

## HTTP contract

All write bodies are JSON (`Content-Type: application/json`), capped at 16 KiB. HTTPS is required except on loopback. Routes return JSON and `Cache-Control: no-store`; they never redirect. The install key is the authentication capability: anyone holding it can manage that install. Device and activity tokens are lowercase even-length hex, 32–512 characters; session IDs are UTF-8 strings up to 256 characters, encoded as one URL path segment.

| Method | Route | Body |
| --- | --- | --- |
| POST | `/installs/{key}/devices` | `{device_token, bundle_id, environment, prefs?}` |
| DELETE | `/installs/{key}/devices/{device_token}` | None; idempotent |
| POST | `/installs/{key}/notify` | Plugin event, below |
| PUT | `/installs/{key}/devices/{device_token}/activities/{session_id}` | `{activity_token}`; device must be registered |
| DELETE | `/installs/{key}/devices/{device_token}/activities/{session_id}` | None; idempotent |
| GET | `/health` | None; liveness only, not an APNs credential check |

Supported bundle IDs are `com.uzairansar.hermesmobile` and `com.uzairansar.hermesmobile.branch`. `environment` is explicitly `sandbox` or `production`, used for both banners and that device's Live Activities. Register again to replace preferences or update the environment; on a device token change, register the new token and delete the old one.

Device preference defaults:

```json
{
  "replies": true,
  "mute_subagents": true,
  "previews": true,
  "presence_suppression": false,
  "active_session_id": null,
  "active_until": 0
}
```

Presence suppression is opt-in. The foreground app can renew `active_until` (Unix seconds) using device registration; the relay caps it at two minutes from receipt. Only replies to that active session are suppressed. Approval, clarification, and error banners still request attention. Activity updates continue regardless of banner preferences.

Notifications use the authoritative V1 contract in [`plugin/hermex_push/payload.py`](../plugin/hermex_push/payload.py): `v`, `kind` (`reply`, `approval`, `clarify`, `turn_error`), `event_id`, `thread_id`, `collapse_id`, `session_id`, `source`, `is_subagent`, `sent_at`, `sealed`. IDs are 32-character hexadecimal HMACs. `sealed` is base64 or null. Progress uses `kind: progress` and `status`, `tool`, `tool_calls`, `started_at` instead of `sealed`/`collapse_id`. Tool names may contain letters, numbers, `_`, `.`, `:`, `/`, `-`; arguments/results are never accepted. Unknown kinds receive `200 {"result":"ignored"}` for forward compatibility. Known events with unsupported versions or extra plaintext fields are rejected.

A successful request returns 200 with `ok`, `accepted`, `deduplicated`, or `ignored`. Invalid requests return 400, oversized requests 413, unmatched routes 404, unsupported methods 405, registration limits 409, and receipt capacity 429. Transient storage/APNs errors and APNs configuration rejections return 503 so the plugin's single retry can recover. Successful recipients are retained in the receipt and skipped on retries. APNs invalid-token responses revoke only the affected device or activity.

## Delivery and iOS integration

[`src/policy.ts`](src/policy.ts) is the pure per-device policy. Replies use interruption level `active`; approval, clarification, and turn errors use `time-sensitive`. Every banner has `apns-collapse-id` and `aps.thread-id`, the generic alert “Hermex / New activity”, and `mutable-content: 1`. Routing fields outside `aps` are `v`, `kind`, `event_id`, `install_hash`, `session_id`, `source`, `is_subagent`, `sealed`. The notification extension uses `install_hash` to select its server-scoped preview key. Disabled previews omit ciphertext (`sealed: null`); oversized encrypted previews also fall back to a generic banner to stay within APNs' 4 KiB limit.

A registered activity suppresses all banners for its session. Routine progress is coalesced to the latest state at most once per second; status transitions bypass the hold at APNs priority 10, routine updates use priority 5. Every activity push sets a fifteen-minute stale date. `done` and `failed` send `event: end`; all others send `update`. The topic is `<bundle_id>.push-type.liveactivity`. There is no push-to-start.

The iOS activity's Codable content state must match this wire shape (the iOS implementation is a separate slice):

```json
{
  "v": 1,
  "status": "running",
  "tool": "terminal",
  "tool_calls": 3,
  "started_at": 1800000000
}
```

Times are Unix seconds, status remains a string, tool is nullable. The phone should tolerate unknown versions/statuses. The relay retains a short completion marker because the plugin emits the end update before its reply. Deleting an ended activity erases its token but retains that marker; a new turn clears it. A subsequent turn needs a fresh phone-created activity/token. Deleting an active activity cancels pending progress and restores banners immediately.

Event receipts are reserved before sending, preventing concurrent or post-restart replays from repeating confirmed recipients. Like any external push API, there is no transaction spanning storage and APNs: a crash after reservation can lose a push, and a network timeout after Apple accepted a request can make a retry ambiguous. This is not an exactly-once delivery guarantee. Held progress gets up to two alarm retries; later status changes supersede stale held progress. APNs acceptance also does not prove device display.

## Owner provisioning and deployment checklist

These steps touch Apple credentials and the live Cloudflare account; the owner performs them or explicitly authorizes them. The implementation has not deployed a Worker, changed the existing KV namespace, uploaded a key, or sent a physical-device push.

1. **Apple Developer → Certificates, Identifiers & Profiles → Identifiers**: select each of the two App IDs, enable Push Notifications, save, and regenerate provisioning profiles as needed. **Keys → +**: create an Apple Push Notifications service key covering both topics and required environments; record the Key ID and Team ID. Download its `.p8` once and keep it outside this repository.
2. **Cloudflare dashboard → account → Storage & databases → KV → Create namespace**: create `hermex-relay` (skip if it exists). Copy the namespace ID. In `wrangler.jsonc`, set your account ID and the namespace ID under binding `hermex_relay`. For a self-hosted instance, also choose a unique Worker `name` and your Apple Team/Key IDs. The checked-in IDs are the owner's already-provisioned resources from the issue.
3. Review the `INSTALLS` Durable Object binding and `v1` SQLite migration. First deployment creates this namespace automatically. No additional manual service is required. Keep migration history on subsequent deployments.
4. Run `npx wrangler login`, then `npm ci`, `npm run check`, and `npm run deploy:dry-run` from `relay/`. These last three commands are local validation.
5. When ready to create the Worker, run `npm run deploy`. The script runs checks and a dry run before `wrangler deploy`. The first deploy creates `hermex-relay` and its Durable Object namespace. Leave `workers.dev` enabled; no DNS route is needed for V1.
6. **Cloudflare → Workers & Pages → hermex-relay → Settings → Variables and Secrets → Add → Secret**: set `APNS_PRIVATE_KEY` to the complete PEM contents of the `.p8`, including BEGIN/END lines, then deploy the change. Alternatively use `npx wrangler secret put APNS_PRIVATE_KEY < /absolute/private/path/AuthKey.p8`. The Worker cannot deliver until this secret is configured. Do not put the key in `wrangler.jsonc`, an issue, or a command argument.
7. **Settings → Observability**: leave Workers logs and traces disabled. The URL contains the install capability; automatic invocation/access logs, `wrangler tail`, or third-party HTTP logging can expose it even when application code never logs. Do not enable URL logging or export request bodies. If adding a custom domain later, configure its logging the same way and use the canonical HTTPS URL directly.
8. Set the plugin's `HERMEX_PUSH_RELAY_URL` to the Worker URL. Pair a dedicated Debug installation and complete the physical-device checks below before claiming delivery works.

### APNs environment check

Debug development tokens use `sandbox`. The issue also requests sandbox for Branch TestFlight; the relay supports sandbox for the branch bundle, but do not infer the environment solely from the bundle suffix. Inspect the built app's **signed `aps-environment` entitlement**: `development` maps to sandbox and `production` maps to production. Distribution/TestFlight builds normally use production APNs. A mismatched token/environment is rejected by Apple. The phone must register the actual environment; the relay never silently tries both. See [Apple's entitlement documentation](https://developer.apple.com/documentation/bundleresources/entitlements/aps-environment).

### Physical-device acceptance (still required)

- Pair a dedicated Debug app on a physical iPhone with `environment: sandbox`. Trigger one plugin reply while suspended, then while terminated; repeat on LAN and through the supported tunnel. Confirm exactly one generic/decrypted banner and correct session routing.
- Confirm both bundle IDs with their signed environment. Re-send the same `event_id` within ten minutes and observe no duplicate. Register two phones, revoke one, and verify only the remaining phone receives the next event.
- Start a phone-created activity and register its update token. Verify routine progress, immediate waiting/done/failed transitions, no duplicate banner, and the stale state after fifteen minutes of silence. Confirm token deletion stops future updates.
- Capture a **synthetic** sealed event at the relay boundary in a local test (never dump real request URLs, tokens, or keys). Verify titles, bodies, profile, and request IDs exist only inside ciphertext. The checked-in sender/privacy tests inspect the outgoing payload without enabling production logging. Production packet capture has not been performed.

## Key rotation

For an APNs signing-key rotation, create the replacement in Apple Developer, retain the old key for rollback, update `APNS_KEY_ID` in the configuration and stage the matching new `APNS_PRIVATE_KEY` secret together before routing traffic to the new version. With Wrangler's version workflow, use `wrangler versions upload`, `wrangler versions secret put APNS_PRIVATE_KEY` on that latest version, then `wrangler versions deploy` after confirming both values belong to the same key. Verify a sandbox push and the required production topic, then revoke the old key in Apple Developer. Do not do a gradual split across mismatched credentials. JWTs are signed using WebCrypto ES256 and shared across installs in an isolate and cached for fifty minutes; a new deployment constructs fresh senders.

An install-key compromise is separate: generate a new pairing on the host, revoke devices under the old capability, and re-pair every phone. Preview-key rotation must be coordinated with the plugin and phone; the relay never receives or rotates that key. Removing a preview key without re-pairing prevents decryption.

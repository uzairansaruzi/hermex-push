import { env, exports } from 'cloudflare:workers';
import { reset, runInDurableObject, runDurableObjectAlarm, evictDurableObject } from 'cloudflare:test';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Env as RelayEnv } from '../src/coordinator';
import { ApnsSender } from '../src/apns';
import { sha256 } from '../src/contract';
import { device, installKey, notification, progress } from './fixtures';

declare global { namespace Cloudflare { interface Env extends RelayEnv {} interface GlobalProps { mainModule: typeof import('../src/index'); durableNamespaces: 'InstallCoordinator' } } }

const bindings = env;
function request(path: string, method = 'POST', body?: unknown, key = installKey) {
  return exports.default.fetch(new Request(`https://relay.test/installs/${key}/${path}`, {
    method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
  }));
}
async function coordinator(key = installKey) {
  return bindings.INSTALLS.get(bindings.INSTALLS.idFromName(await sha256(key)));
}
async function register() { expect((await request('devices', 'POST', device)).status).toBe(200); }
async function registerActivity() {
  await register();
  expect((await request(`devices/${device.device_token}/activities/${encodeURIComponent(progress.session_id)}`, 'PUT', { activity_token: 'f'.repeat(64) })).status).toBe(200);
}

beforeEach(() => { vi.spyOn(ApnsSender.prototype, 'send').mockResolvedValue('sent'); });
afterEach(async () => { vi.restoreAllMocks(); await reset(); });

it('registers under only the install hash, updates preferences, and revokes immediately', async () => {
  await register();
  const hash = await sha256(installKey);
  let keys = await bindings.hermex_relay.list();
  expect(keys.keys).toHaveLength(1);
  expect(keys.keys[0]?.name).toContain(`installs:${hash}:devices:`);
  expect(JSON.stringify(keys)).not.toContain(installKey);
  await request('devices', 'POST', { ...device, prefs: { replies: false } });
  keys = await bindings.hermex_relay.list();
  expect(keys.keys).toHaveLength(1);
  expect((await request('notify', 'POST', notification)).status).toBe(200);
  expect(ApnsSender.prototype.send).not.toHaveBeenCalled();
  expect((await request(`devices/${device.device_token}`, 'DELETE')).status).toBe(200);
  expect((await request(`devices/${device.device_token}`, 'DELETE')).status).toBe(200);
  expect((await bindings.hermex_relay.list()).keys).toHaveLength(0);
});

it('deduplicates concurrent requests and retains receipts across object eviction', async () => {
  await register();
  const responses = await Promise.all(Array.from({ length: 5 }, () => request('notify', 'POST', notification)));
  expect(responses.map(response => response.status)).toEqual([200, 200, 200, 200, 200]);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
  await evictDurableObject(await coordinator());
  expect((await request('notify', 'POST', notification)).status).toBe(200);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
});

it('expires dedupe receipts after ten minutes', async () => {
  await register();
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  await request('notify', 'POST', notification);
  clock.mockReturnValue(1_800_000_599_999);
  await request('notify', 'POST', notification);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
  clock.mockReturnValue(1_800_000_600_000);
  await request('notify', 'POST', notification);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(2);
});

it('isolates installs even when event ids and device tokens match', async () => {
  await register();
  await request('devices', 'POST', device, '2'.repeat(64));
  await request('notify', 'POST', notification);
  await request('notify', 'POST', notification, '2'.repeat(64));
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(2);
});

it('retries failed recipients without repeating successful recipients', async () => {
  await register();
  await request('devices', 'POST', { ...device, device_token: '2'.repeat(64) });
  vi.mocked(ApnsSender.prototype.send).mockResolvedValueOnce('sent').mockResolvedValueOnce('retry').mockResolvedValueOnce('sent');
  expect((await request('notify', 'POST', notification)).status).toBe(503);
  expect((await request('notify', 'POST', notification)).status).toBe(200);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(3);
  expect(vi.mocked(ApnsSender.prototype.send).mock.calls.map(([push]) => push.token)).toEqual([device.device_token, '2'.repeat(64), '2'.repeat(64)]);
});

it('removes invalid device tokens without revoking other devices', async () => {
  await register();
  await request('devices', 'POST', { ...device, device_token: '2'.repeat(64) });
  vi.mocked(ApnsSender.prototype.send).mockResolvedValueOnce('invalid-token');
  await request('notify', 'POST', notification);
  expect((await bindings.hermex_relay.list()).keys).toHaveLength(1);
  expect((await bindings.hermex_relay.list()).keys[0]?.name).toContain('2'.repeat(64));
});

it('coalesces routine progress to the latest value and flushes with an alarm', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  await registerActivity();
  await request('notify', 'POST', progress);
  clock.mockReturnValue(1_800_000_000_100);
  await request('notify', 'POST', { ...progress, event_id: '1'.repeat(32), tool_calls: 2 });
  clock.mockReturnValue(1_800_000_000_200);
  await request('notify', 'POST', { ...progress, event_id: '2'.repeat(32), tool_calls: 3 });
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
  clock.mockReturnValue(1_800_000_001_000);
  await runDurableObjectAlarm(await coordinator());
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(2);
  expect(vi.mocked(ApnsSender.prototype.send).mock.calls[1]?.[0]).toMatchObject({ priority: '5', payload: { aps: { 'content-state': { tool_calls: 3 }, 'stale-date': 1_800_000_901 } } });
});

it('sends status changes immediately, supersedes held progress, and suppresses the completion banner', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  await registerActivity();
  await request('notify', 'POST', progress);
  clock.mockReturnValue(1_800_000_000_100);
  await request('notify', 'POST', { ...progress, event_id: '1'.repeat(32), tool_calls: 2 });
  await request('notify', 'POST', { ...progress, event_id: '2'.repeat(32), status: 'waiting' });
  await request('notify', 'POST', { ...progress, event_id: '3'.repeat(32), status: 'done' });
  await request('notify', 'POST', notification);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(3);
  expect(vi.mocked(ApnsSender.prototype.send).mock.calls.every(([push]) => push.type === 'liveactivity' && push.priority === '10')).toBe(true);
  clock.mockReturnValue(1_800_000_001_000);
  await runDurableObjectAlarm(await coordinator());
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(3);
  // A new turn without a newly registered activity gets banners again.
  await request('notify', 'POST', { ...progress, event_id: '4'.repeat(32), started_at: progress.started_at + 20, sent_at: progress.sent_at + 20 });
  await request('notify', 'POST', { ...notification, event_id: '5'.repeat(32) });
  expect(vi.mocked(ApnsSender.prototype.send).mock.calls[3]?.[0].type).toBe('alert');
});

it('banners an approval during an activity while its waiting update stays silent', async () => {
  await registerActivity();
  await request('notify', 'POST', { ...progress, status: 'waiting' });
  await request('notify', 'POST', { ...notification, kind: 'approval' });
  const pushes = vi.mocked(ApnsSender.prototype.send).mock.calls.map(([push]) => push);
  expect(pushes.map(push => push.type)).toEqual(['liveactivity', 'alert']);
  expect(pushes[0]?.payload).not.toHaveProperty('aps.alert');
  expect(pushes[1]).toMatchObject({ payload: { aps: { 'interruption-level': 'time-sensitive' }, kind: 'approval' } });
});

it('activity deletion cancels pending updates and restores banners', async () => {
  await registerActivity();
  await request(`devices/${device.device_token}/activities/${encodeURIComponent(progress.session_id)}`, 'DELETE');
  await request('notify', 'POST', progress);
  await request('notify', 'POST', notification);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
  expect(vi.mocked(ApnsSender.prototype.send).mock.calls[0]?.[0].type).toBe('alert');
});

it('invalid activity tokens remove only the activity; the device still receives banners', async () => {
  await registerActivity();
  vi.mocked(ApnsSender.prototype.send).mockResolvedValueOnce('invalid-token');
  await request('notify', 'POST', progress);
  await request('notify', 'POST', notification);
  expect((await bindings.hermex_relay.list()).keys).toHaveLength(1);
  expect(vi.mocked(ApnsSender.prototype.send).mock.calls[1]?.[0].type).toBe('alert');
});

it('does not persist keys, ciphertext, or notification bodies in coordinator storage', async () => {
  await register();
  await request('notify', 'POST', notification);
  const stored = await runInDurableObject(await coordinator(), async (_instance, state) => JSON.stringify([...await state.storage.list()]));
  expect(stored).not.toContain(installKey);
  expect(stored).not.toContain(notification.sealed);
  expect(stored).not.toContain('New activity');
});

it('ignores unknown kinds and rejects plaintext fields, malformed payloads, and oversized bodies', async () => {
  expect((await request('notify', 'POST', { kind: 'future_kind', arbitrary: true })).status).toBe(200);
  for (const event of [{ ...notification, title: 'secret title' }, { ...notification, v: 2 }, { ...notification, event_id: 'bad' }, { ...progress, tool: 'cat secret text' }]) expect((await request('notify', 'POST', event)).status).toBe(400);
  expect((await request('notify', 'POST', { padding: 'x'.repeat(17000) })).status).toBe(413);
  expect((await request('devices', 'POST', { ...device, bundle_id: 'arbitrary.app' })).status).toBe(400);
  expect((await request('notify', 'GET')).status).toBe(405);
  expect((await request('notify', 'POST', notification, 'bad-key')).status).toBe(404);
  expect(ApnsSender.prototype.send).not.toHaveBeenCalled();
});

it('deleting an ended activity removes its token without reintroducing a completion banner', async () => {
  await registerActivity();
  await request('notify', 'POST', { ...progress, status: 'done' });
  await request(`devices/${device.device_token}/activities/${encodeURIComponent(progress.session_id)}`, 'DELETE');
  await request('notify', 'POST', notification);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
  const stored = await runInDurableObject(await coordinator(), async (_instance, state) => JSON.stringify([...await state.storage.list({ prefix: 'activity:' })]));
  expect(stored).not.toContain('f'.repeat(64));
});

it('does not roll back activity state for a late event from an earlier timestamp', async () => {
  await registerActivity();
  await request('notify', 'POST', progress);
  await request('notify', 'POST', { ...progress, event_id: '1'.repeat(32), status: 'waiting', sent_at: progress.sent_at + 2 });
  await request('notify', 'POST', { ...progress, event_id: '2'.repeat(32), sent_at: progress.sent_at + 1 });
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(2);
});

it('retries a failed coalesced update using a durable alarm', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  await registerActivity();
  await request('notify', 'POST', progress);
  clock.mockReturnValue(1_800_000_000_100);
  await request('notify', 'POST', { ...progress, event_id: '1'.repeat(32), tool_calls: 2 });
  vi.mocked(ApnsSender.prototype.send).mockResolvedValueOnce('retry').mockResolvedValueOnce('sent');
  clock.mockReturnValue(1_800_000_001_000);
  await runDurableObjectAlarm(await coordinator());
  clock.mockReturnValue(1_800_000_003_000);
  await runDurableObjectAlarm(await coordinator());
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(3);
  expect(vi.mocked(ApnsSender.prototype.send).mock.calls[2]?.[0]).toMatchObject({ payload: { aps: { 'content-state': { tool_calls: 2 } } } });
});

it('revocation cancels a held activity update before its alarm runs', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  await registerActivity();
  await request('notify', 'POST', progress);
  clock.mockReturnValue(1_800_000_000_100);
  await request('notify', 'POST', { ...progress, event_id: '1'.repeat(32), tool_calls: 2 });
  await request(`devices/${device.device_token}`, 'DELETE');
  clock.mockReturnValue(1_800_000_001_000);
  await runDurableObjectAlarm(await coordinator());
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
});

it('cleans up more than one storage delete batch of expired receipts', async () => {
  const stub = await coordinator();
  await runInDurableObject(stub, async (_instance, state) => {
    await state.storage.put(Object.fromEntries(Array.from({ length: 256 }, (_, i) => [`event:expired-${i}`, { expires: 0, completed: [] }])));
  });
  await register();
  const count = await runInDurableObject(stub, async (_instance, state) => (await state.storage.list({ prefix: 'event:' })).size);
  expect(count).toBe(0);
});

it('does not acknowledge delivery when the authoritative KV revision is unavailable', async () => {
  await register();
  const [record] = (await bindings.hermex_relay.list()).keys;
  if (!record) throw new Error('Missing test registration');
  await bindings.hermex_relay.delete(record.name);
  expect((await request('notify', 'POST', notification)).status).toBe(503);
  expect(ApnsSender.prototype.send).not.toHaveBeenCalled();
});

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

it('dates a banner hold from the relay clock in seconds, not the host sent_at', async () => {
  await register();
  vi.spyOn(Date, 'now').mockReturnValue(1_900_000_000_500);
  expect((await request('notify', 'POST', { ...notification, kind: 'approval' })).status).toBe(200);
  expect(ApnsSender.prototype.send).toHaveBeenCalledWith(expect.objectContaining({ type: 'alert', expiration: 1_900_000_300 }));
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

it('sweeps legacy receipts in batches, including rows written before the due index', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  const stub = await coordinator();
  // State as the previous version left it: receipts without index rows and an alarm from its schedule().
  await runInDurableObject(stub, async (_instance, state) => {
    await state.storage.put(Object.fromEntries(Array.from({ length: 256 }, (_, i) => [`event:expired-${i}`, { expires: 0, completed: [] }])));
    await state.storage.put('event:live', { expires: 1_800_000_030_000, completed: [] });
    await state.storage.put(`activity:${device.device_token}:expired`, { token: 'f'.repeat(64), expires: 0 });
    await state.storage.setAlarm(1_800_000_000_000);
  });
  const rows = () => runInDurableObject(stub, async (_instance, state) => [...(await state.storage.list()).keys()].filter(key => /^(event|activity):/.test(key)));
  expect(await runDurableObjectAlarm(stub)).toBe(true);
  expect(await rows()).toEqual(['event:live', 'event:~indexed']);
  clock.mockReturnValue(1_800_000_060_000);
  expect(await runDurableObjectAlarm(stub)).toBe(true);
  expect(await rows()).toEqual(['event:~indexed']);
});

it('indexes again after a rollback and redeploy, because the earlier version deletes the marker', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  const stub = await coordinator();
  await request('notify', 'POST', notification);
  expect(await runDurableObjectAlarm(stub)).toBe(true);
  // Rolled back: the earlier version deletes expired `event:` rows on each request and stores receipts without index entries.
  clock.mockReturnValue(1_800_000_600_000);
  await runInDurableObject(stub, async (_instance, state) => {
    const rows = await state.storage.list<{ expires: number }>({ prefix: 'event:' });
    await state.storage.delete([...rows].filter(([, row]) => row.expires <= Date.now()).map(([key]) => key));
    await state.storage.put(Object.fromEntries(['1', '2'].map(c => [`event:${c.repeat(32)}`, { expires: 1_800_001_200_000, completed: [] }])));
  });
  // Redeployed: the next alarm recounts and indexes those receipts, then sweeps them at expiry.
  await evictDurableObject(stub);
  const state = () => runInDurableObject(stub, async (_instance, state) => ({ receipts: await state.storage.get('receipts'), alarm: await state.storage.getAlarm() }));
  clock.mockReturnValue(1_800_000_660_000);
  expect(await runDurableObjectAlarm(stub)).toBe(true);
  expect(await state()).toEqual({ receipts: 2, alarm: 1_800_001_200_000 });
  clock.mockReturnValue(1_800_001_200_000);
  expect(await runDurableObjectAlarm(stub)).toBe(true);
  expect(await state()).toEqual({ receipts: 0, alarm: null });
});

it('sweeps expired receipts together on one minute-aligned alarm', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_001_000);
  const stub = await coordinator();
  await request('notify', 'POST', notification);
  clock.mockReturnValue(1_800_000_030_000);
  await request('notify', 'POST', { ...notification, event_id: '1'.repeat(32) });
  const state = () => runInDurableObject(stub, async (_instance, state) => ({
    alarm: await state.storage.getAlarm(), keys: [...(await state.storage.list()).keys()].filter(key => /^(event|due):/.test(key)),
  }));
  expect((await state()).alarm).toBe(1_800_000_660_000);
  clock.mockReturnValue(1_800_000_660_000);
  expect(await runDurableObjectAlarm(stub)).toBe(true);
  expect(await state()).toEqual({ alarm: null, keys: ['event:~indexed'] });
});

it('enforces the per-install receipt cap and frees it after the sweep', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  const stub = await coordinator();
  const hash = await sha256(installKey);
  const eventId = (i: number) => i.toString(16).padStart(32, '0');
  await runInDurableObject(stub, async instance => {
    for (let i = 0; i < 4096; i++) await instance.handle(hash, { action: 'notify', event: { ...notification, event_id: eventId(i) } });
  });
  expect((await request('notify', 'POST', { ...notification, event_id: eventId(4096) })).status).toBe(429);
  expect((await request('notify', 'POST', { ...notification, event_id: eventId(0) })).status).toBe(200);
  clock.mockReturnValue(1_800_000_660_000);
  await runDurableObjectAlarm(stub);
  expect((await request('notify', 'POST', { ...notification, event_id: eventId(4096) })).status).toBe(200);
});

it('notifies without listing stored receipts', async () => {
  await register();
  const stub = await coordinator();
  const hash = await sha256(installKey);
  await runInDurableObject(stub, async (instance, state) => {
    await state.storage.put(Object.fromEntries(Array.from({ length: 1000 }, (_, i) => [`event:stored-${i}`, { expires: Date.now() + 600_000, completed: [] }])));
    const list = vi.spyOn(state.storage, 'list');
    expect(await instance.handle(hash, { action: 'notify', event: notification })).toEqual({ status: 200, result: 'accepted' });
    expect(list).not.toHaveBeenCalled();
  });
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
});

it('reads only the registry and activity key for progress without an activity, and writes nothing', async () => {
  await register();
  const stub = await coordinator();
  const hash = await sha256(installKey);
  const activityKey = `activity:${device.device_token}:${await sha256(progress.session_id)}`;
  await runInDurableObject(stub, async (instance, state) => {
    const spies = (['get', 'list', 'put', 'delete', 'getAlarm', 'setAlarm'] as const).map(method => vi.spyOn(state.storage, method));
    expect(await instance.handle(hash, { action: 'notify', event: progress })).toEqual({ status: 200, result: 'accepted' });
    expect(spies[0]?.mock.calls.map(([key]) => key)).toEqual(['devices', activityKey]);
    for (const spy of spies.slice(1)) expect(spy).not.toHaveBeenCalled();
  });
});

it('sends a replayed progress event once and stores no receipt for it', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  await registerActivity();
  await request('notify', 'POST', progress);
  // A replay inside the hold window must not be held and flushed by the alarm...
  clock.mockReturnValue(1_800_000_000_100);
  await request('notify', 'POST', progress);
  clock.mockReturnValue(1_800_000_001_000);
  await runDurableObjectAlarm(await coordinator());
  // ...nor sent again once the window has passed.
  clock.mockReturnValue(1_800_000_005_000);
  await request('notify', 'POST', progress);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(1);
  // Only the alarm's legacy-pass marker; the progress event stored no receipt.
  const receipts = await runInDurableObject(await coordinator(), async (_instance, state) => [...(await state.storage.list({ prefix: 'event:' })).keys()]);
  expect(receipts).toEqual(['event:~indexed']);
});

it('wakes for the earliest held update when several devices hold in one request', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
  const second = '2'.repeat(64);
  const activity = (token: string) => request(`devices/${token}/activities/${encodeURIComponent(progress.session_id)}`, 'PUT', { activity_token: 'f'.repeat(64) });
  await register();
  await request('devices', 'POST', { ...device, device_token: second });
  await activity(second);
  await request('notify', 'POST', progress);
  clock.mockReturnValue(1_800_000_000_500);
  await activity(device.device_token);
  await request('notify', 'POST', { ...progress, event_id: '1'.repeat(32) });
  clock.mockReturnValue(1_800_000_001_000);
  await runDurableObjectAlarm(await coordinator());
  // The first device last sent at +500 and the second at +1000, so they hold until +1500 and +2000.
  clock.mockReturnValue(1_800_000_001_200);
  await request('notify', 'POST', { ...progress, event_id: '2'.repeat(32), tool_calls: 2 });
  const alarm = await runInDurableObject(await coordinator(), (_instance, state) => state.storage.getAlarm());
  expect(alarm).toBe(1_800_000_001_500);
});

it('lets the plugin retry a progress event that APNs asked to retry', async () => {
  await registerActivity();
  vi.mocked(ApnsSender.prototype.send).mockResolvedValueOnce('retry');
  expect((await request('notify', 'POST', progress)).status).toBe(503);
  expect((await request('notify', 'POST', progress)).status).toBe(200);
  expect(ApnsSender.prototype.send).toHaveBeenCalledTimes(2);
});

it('does not acknowledge delivery when the authoritative KV revision is unavailable', async () => {
  await register();
  const [record] = (await bindings.hermex_relay.list()).keys;
  if (!record) throw new Error('Missing test registration');
  await bindings.hermex_relay.delete(record.name);
  expect((await request('notify', 'POST', notification)).status).toBe(503);
  expect(ApnsSender.prototype.send).not.toHaveBeenCalled();
});

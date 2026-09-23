import { afterEach, expect, it, vi } from 'vitest';
import { ApnsSender, activityPush, bannerPush } from '../src/apns';
import { deliveryPolicy } from '../src/policy';
import { device, installKey, notification, progress } from './fixtures';
import { sha256 } from '../src/contract';

afterEach(() => vi.restoreAllMocks());

async function signingPair() {
  const pair = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign', 'verify']);
  if (!('privateKey' in pair)) throw new Error('Expected key pair');
  const bytes = await crypto.subtle.exportKey('pkcs8', pair.privateKey);
  if (!(bytes instanceof ArrayBuffer)) throw new Error('Expected PKCS8 bytes');
  const pem = `-----BEGIN PRIVATE KEY-----\n${btoa(String.fromCharCode(...new Uint8Array(bytes)))}\n-----END PRIVATE KEY-----`;
  return { publicKey: pair.publicKey, sender: new ApnsSender({ APNS_PRIVATE_KEY: pem, APNS_KEY_ID: 'TESTKEY', APNS_TEAM_ID: 'TESTTEAM' }) };
}

it('signs a verifiable ES256 JWT and renews it after 50 minutes', async () => {
  const { sender, publicKey } = await signingPair();
  const jwt = await sender.authorization(10000);
  const [header, claims, signature] = jwt.split('.');
  expect(JSON.parse(atob(header!))).toEqual({ alg: 'ES256', kid: 'TESTKEY' });
  expect(JSON.parse(atob(claims!))).toEqual({ iss: 'TESTTEAM', iat: 10000 });
  const bytes = Uint8Array.from(atob(signature!.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  expect(bytes.length).toBe(64);
  expect(await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, publicKey, bytes, new TextEncoder().encode(`${header}.${claims}`))).toBe(true);
  expect(await sender.authorization(12999)).toBe(jwt);
  expect(await sender.authorization(13000)).not.toBe(jwt);
});

it('builds generic banners containing ciphertext and routing metadata only', async () => {
  const policy = deliveryPolicy(notification, device, false, 0);
  if (policy.type !== 'banner') throw new Error('Expected banner');
  const push = bannerPush(notification, device, await sha256(installKey), policy);
  expect(push.payload).toMatchObject({ sealed: notification.sealed, session_id: notification.session_id, aps: { alert: { title: 'Hermex', body: 'New activity' }, 'mutable-content': 1 } });
  expect(JSON.stringify(push)).not.toContain(installKey);
  expect(bannerPush(notification, device, 'hash', { ...policy, preview: false }).payload).toMatchObject({ sealed: null });
  expect(bannerPush({ ...notification, sealed: 'a'.repeat(8000) }, device, 'hash', policy).payload).toMatchObject({ sealed: null });
});

it('uses versioned activity state, stale dates, end events, and the activity topic', () => {
  const push = activityPush({ ...progress, status: 'done' }, device, 'token', '10', 5000);
  expect(push.topic).toBe('com.uzairansar.hermesmobile.push-type.liveactivity');
  expect(push.payload).toEqual({ aps: { timestamp: 5000, event: 'end', 'stale-date': 5900, 'content-state': { v: 1, status: 'done', tool: 'terminal', tool_calls: 1, started_at: progress.started_at, updated_at: 5000 } } });
});

it.each(['sandbox', 'production'] as const)('sends to %s with APNs headers and no redirects', async environment => {
  const { sender } = await signingPair();
  // Build the Request for real: workerd throws on init values it does not support, which a bare stub hides.
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => { new Request(input, init); return new Response(null, { status: 200 }); });
  const push = activityPush(progress, { ...device, environment, bundle_id: 'com.uzairansar.hermesmobile.branch' }, 'token', '5', 5000);
  expect(await sender.send(push)).toBe('sent');
  expect(fetch).toHaveBeenCalledWith(`https://${environment === 'sandbox' ? 'api.sandbox.push.apple.com' : 'api.push.apple.com'}/3/device/token`, expect.objectContaining({ redirect: 'manual', headers: expect.objectContaining({ 'apns-topic': 'com.uzairansar.hermesmobile.branch.push-type.liveactivity', 'apns-priority': '5', 'apns-push-type': 'liveactivity', authorization: expect.stringMatching(/^bearer /) }) }));
});

it.each([
  [410, 'Unregistered', 'invalid-token'], [400, 'BadDeviceToken', 'invalid-token'],
  [400, 'DeviceTokenNotForTopic', 'invalid-token'], [403, 'InvalidProviderToken', 'rejected'],
  [429, 'TooManyRequests', 'retry'], [503, 'ServiceUnavailable', 'retry'],
] as const)('classifies APNs %s %s', async (status, reason, expected) => {
  const { sender } = await signingPair();
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ reason }, { status }));
  expect(await sender.send(activityPush(progress, device, 'token', '5', 1))).toBe(expected);
});

it('shares one JWT when devices request authorization concurrently', async () => {
  const { sender } = await signingPair();
  const tokens = await Promise.all(Array.from({ length: 8 }, () => sender.authorization(10000)));
  expect(new Set(tokens).size).toBe(1);
});

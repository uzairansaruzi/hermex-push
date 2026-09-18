import type { Device, NotificationEvent, ProgressEvent } from './contract';
import type { Delivery } from './policy';

export interface ApnsSecrets { APNS_KEY_ID: string; APNS_TEAM_ID: string; APNS_PRIVATE_KEY: string }
export interface Push { token: string; topic: string; environment: Device['environment']; type: 'alert' | 'liveactivity'; priority: '5' | '10'; collapseId?: string; payload: object }
export type SendResult = 'sent' | 'invalid-token' | 'retry' | 'rejected';
const encoder = new TextEncoder();

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
}

/** Cache one signing key/JWT per sender; renewal stays within Apple's one-hour window. */
export class ApnsSender {
  private jwt?: { value: Promise<string>; created: number };
  private key?: Promise<CryptoKey>;
  constructor(private secrets: ApnsSecrets) {}

  async authorization(now = Math.floor(Date.now() / 1000)): Promise<string> {
    if (this.jwt && now >= this.jwt.created && now - this.jwt.created < 3000) return this.jwt.value;
    const value = this.sign(now);
    this.jwt = { value, created: now };
    try { return await value; }
    catch (error) { this.jwt = undefined; throw error; }
  }

  private async sign(now: number): Promise<string> {
    this.key ??= crypto.subtle.importKey(
      'pkcs8', Uint8Array.from(atob(this.secrets.APNS_PRIVATE_KEY.replace(/-----[A-Z ]+-----/g, '').replace(/\s/g, '')), c => c.charCodeAt(0)),
      { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign'],
    );
    const header = base64url(encoder.encode(JSON.stringify({ alg: 'ES256', kid: this.secrets.APNS_KEY_ID })));
    const claims = base64url(encoder.encode(JSON.stringify({ iss: this.secrets.APNS_TEAM_ID, iat: now })));
    const unsigned = `${header}.${claims}`;
    const signature = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, await this.key, encoder.encode(unsigned));
    return `${unsigned}.${base64url(new Uint8Array(signature))}`;
  }

  async send(push: Push): Promise<SendResult> {
    try {
      const response = await fetch(`https://${push.environment === 'sandbox' ? 'api.sandbox.push.apple.com' : 'api.push.apple.com'}/3/device/${push.token}`, {
        method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
        headers: {
          authorization: `bearer ${await this.authorization()}`, 'content-type': 'application/json',
          'apns-topic': push.topic, 'apns-push-type': push.type, 'apns-priority': push.priority,
          'apns-expiration': '0', ...(push.collapseId ? { 'apns-collapse-id': push.collapseId } : {}),
        },
        body: JSON.stringify(push.payload),
      });
      if (response.ok) { await response.body?.cancel(); return 'sent'; }
      const data: unknown = await response.json().catch(() => null);
      const reason = data && typeof data === 'object' && 'reason' in data ? data.reason : undefined;
      if (response.status === 410 || (response.status === 400 && (reason === 'BadDeviceToken' || reason === 'DeviceTokenNotForTopic'))) return 'invalid-token';
      if (response.status === 429 || response.status >= 500) return 'retry';
      return 'rejected';
    } catch {
      // Do not log exceptions: fetch errors may embed tokens or credential-bearing URLs.
      return 'retry';
    }
  }
}

let shared: { secrets: ApnsSecrets; sender: ApnsSender } | undefined;

/** Reuse provider tokens across installs in the same isolate, including concurrent fan-out. */
export function senderFor(secrets: ApnsSecrets): ApnsSender {
  if (!shared || shared.secrets.APNS_KEY_ID !== secrets.APNS_KEY_ID ||
    shared.secrets.APNS_TEAM_ID !== secrets.APNS_TEAM_ID || shared.secrets.APNS_PRIVATE_KEY !== secrets.APNS_PRIVATE_KEY) {
    const credentials = { APNS_KEY_ID: secrets.APNS_KEY_ID, APNS_TEAM_ID: secrets.APNS_TEAM_ID, APNS_PRIVATE_KEY: secrets.APNS_PRIVATE_KEY };
    shared = { secrets: credentials, sender: new ApnsSender(credentials) };
  }
  return shared.sender;
}

export function bannerPush(event: NotificationEvent, device: Device, installHash: string, policy: Extract<Delivery, { type: 'banner' }>): Push {
  const payload = {
    aps: {
      alert: { title: 'Hermex', body: 'New activity' }, sound: 'default',
      'mutable-content': 1, 'thread-id': policy.threadId, 'interruption-level': policy.interruption,
    },
    v: 1, kind: event.kind, event_id: event.event_id, install_hash: installHash,
    session_id: event.session_id, source: event.source, is_subagent: event.is_subagent,
    sealed: policy.preview ? event.sealed : null,
  };
  // Unicode previews can exceed APNs' 4 KiB limit even with the plugin's character cap.
  if (encoder.encode(JSON.stringify(payload)).length > 4096) payload.sealed = null;
  return { token: device.device_token, topic: device.bundle_id, environment: device.environment, type: 'alert', priority: '10', collapseId: policy.collapseId, payload };
}

export function activityPush(event: ProgressEvent, device: Device, token: string, priority: '5' | '10', now: number): Push {
  return {
    token, topic: `${device.bundle_id}.push-type.liveactivity`, environment: device.environment,
    type: 'liveactivity', priority,
    payload: { aps: {
      timestamp: now, event: event.status === 'done' || event.status === 'failed' ? 'end' : 'update',
      'stale-date': now + 900,
      'content-state': { v: 1, status: event.status, tool: event.tool, tool_calls: event.tool_calls, started_at: event.started_at },
    } },
  };
}

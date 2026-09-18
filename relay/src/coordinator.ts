import { DurableObject } from 'cloudflare:workers';
import { ApnsSender, senderFor, activityPush, bannerPush, type ApnsSecrets } from './apns';
import { sha256, type Command, type Device, type ProgressEvent, type PushEvent } from './contract';
import { activityTiming, deliveryPolicy, type ActivityTiming } from './policy';

export interface Env extends ApnsSecrets {
  hermex_relay: KVNamespace;
  INSTALLS: DurableObjectNamespace<InstallCoordinator>;
}
type Registry = Record<string, string>;
interface Receipt { expires: number; completed: string[]; finished?: boolean }
interface Activity {
  token: string;
  expires: number;
  timing?: ActivityTiming;
  latest?: { started: number; sent: number };
  ended?: boolean;
  pending?: { event: ProgressEvent; due: number; attempts: number };
}
export interface Outcome { status: number; result: string }
const ok = (result = 'ok'): Outcome => ({ status: 200, result });

/** One serial owner per install. KV values are immutable; durable pointers make deletes immediate. */
export class InstallCoordinator extends DurableObject<Env> {
  private tail: Promise<unknown> = Promise.resolve();
  private sender: ApnsSender;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sender = senderFor(env);
  }

  private serial<T>(work: () => Promise<T>): Promise<T> {
    const next = this.tail.then(work);
    this.tail = next.catch(() => undefined);
    return next;
  }

  handle(installHash: string, command: Command): Promise<Outcome> {
    return this.serial(async () => {
      try {
        await this.cleanup();
        try { return await this.execute(installHash, command); }
        finally { await this.schedule(); }
      } catch {
        // RPC exception logging must not accidentally acquire request data later.
        return { status: 503, result: 'temporarily_unavailable' };
      }
    });
  }

  private async registry(): Promise<Registry> {
    return await this.ctx.storage.get<Registry>('devices') ?? {};
  }

  private async device(key: string): Promise<Device> {
    const device = await this.env.hermex_relay.get<Device>(key, 'json');
    // Never acknowledge an event against an incomplete registry read.
    if (!device) throw new Error('Registry unavailable');
    return device;
  }

  private async removeDevice(token: string, registry: Registry) {
    const key = registry[token];
    delete registry[token];
    await this.ctx.storage.put('devices', registry);
    const activities = await this.ctx.storage.list({ prefix: `activity:${token}:` });
    await this.ctx.storage.delete([...activities.keys()]);
    if (key) await this.env.hermex_relay.delete(key);
  }

  private async activityKey(token: string, session: string) {
    return `activity:${token}:${await sha256(session)}`;
  }

  private async execute(installHash: string, command: Command): Promise<Outcome> {
    const registry = await this.registry();
    switch (command.action) {
      case 'register': {
        const token = command.device.device_token;
        if (!registry[token] && Object.keys(registry).length >= 32) return { status: 409, result: 'device_limit' };
        const previous = registry[token];
        const key = `installs:${installHash}:devices:${token}:${crypto.randomUUID()}`;
        // Unique revisions avoid KV's one-write-per-key-per-second restriction.
        await this.env.hermex_relay.put(key, JSON.stringify(command.device));
        registry[token] = key;
        await this.ctx.storage.put('devices', registry);
        if (previous) await this.env.hermex_relay.delete(previous);
        return ok();
      }
      case 'delete-device':
        await this.removeDevice(command.token, registry);
        return ok();
      case 'put-activity': {
        if (!registry[command.token]) return { status: 404, result: 'device_not_found' };
        const key = await this.activityKey(command.token, command.session);
        const current = await this.ctx.storage.get<Activity>(key);
        const activities = await this.ctx.storage.list({ prefix: `activity:${command.token}:` });
        if (!current && activities.size >= 64) return { status: 409, result: 'activity_limit' };
        await this.ctx.storage.put<Activity>(key, {
          ...(current?.token === command.activityToken ? current : {}),
          token: command.activityToken, expires: Date.now() + 8 * 60 * 60 * 1000,
        });
        return ok();
      }
      case 'delete-activity': {
        const key = await this.activityKey(command.token, command.session);
        const current = await this.ctx.storage.get<Activity>(key);
        // The phone may acknowledge an end before the plugin's reply arrives.
        if (current?.ended) await this.ctx.storage.put(key, { ...current, token: '' });
        else await this.ctx.storage.delete(key);
        return ok();
      }
      case 'notify':
        return this.notify(installHash, command.event, registry);
    }
  }

  private async notify(installHash: string, event: PushEvent, registry: Registry): Promise<Outcome> {
    const key = `event:${event.event_id}`;
    const existing = await this.ctx.storage.get<Receipt>(key);
    if (existing?.finished) return ok('deduplicated');
    const receipt = existing ?? { expires: Date.now() + 600_000, completed: [] };
    const records = await this.ctx.storage.list({ prefix: 'event:' });
    if (!existing && records.size >= 4096) return { status: 429, result: 'event_limit' };
    // Read all KV revisions before making irreversible sends; missing KV data is retryable.
    const devices = await Promise.all(Object.entries(registry).map(async ([token, ref]) => ({ token, device: await this.device(ref) })));
    let retry = false;
    let rejected = false;
    const targets = await Promise.all(devices.filter(({ token }) => !receipt.completed.includes(token)).map(async ({ token, device }) => {
      const activityKey = await this.activityKey(token, event.session_id);
      let activity = await this.ctx.storage.get<Activity>(activityKey);
      if (activity?.ended && event.kind === 'progress' && (
        event.started_at > (activity.latest?.started ?? 0) || (
          event.status === 'running' && event.started_at === activity.latest?.started && event.sent_at >= activity.latest.sent
        )
      )) {
        await this.ctx.storage.delete(activityKey);
        activity = undefined; // A new turn needs a new phone-created activity token.
      }
      const policy = deliveryPolicy(event, device, !!activity, Date.now() / 1000);
      return { token, device, activityKey, activity, policy };
    }));
    // Reserve before APNs. A crash after acceptance cannot cause a replay to send twice.
    receipt.completed.push(...targets.map(target => target.token));
    await this.ctx.storage.put(key, receipt);
    // Concurrent fan-out keeps one slow phone from exhausting the plugin's 10-second timeout.
    const deliveries = await Promise.allSettled(targets.map(async ({ device, activityKey, activity, policy }) => {
      let result: 'sent' | 'invalid-token' | 'retry' | 'rejected' = 'sent';
      if (policy.type === 'activity' && event.kind === 'progress' && activity) {
        result = await this.progress(activityKey, activity, event, device);
      } else if (policy.type === 'banner' && event.kind !== 'progress') {
        result = await this.sender.send(bannerPush(event, device, installHash, policy));
      }
      return result;
    }));
    for (const [index, delivery] of deliveries.entries()) {
      const target = targets[index];
      if (!target) continue;
      const { token } = target;
      const result = delivery.status === 'fulfilled' ? delivery.value : 'retry';
      if (result === 'invalid-token') await this.removeDevice(token, registry);
      if (result === 'retry' || result === 'rejected') {
        receipt.completed = receipt.completed.filter(item => item !== token);
        retry = true;
        rejected ||= result === 'rejected';
      }
    }
    receipt.finished = !retry;
    // Also retain empty-fanout events so their storage lifetime is bounded and explicit.
    await this.ctx.storage.put(key, receipt);
    return retry ? { status: 503, result: rejected ? 'apns_rejected' : 'delivery_retry' } : ok(existing ? 'deduplicated' : 'accepted');
  }

  private async progress(key: string, activity: Activity, event: ProgressEvent, device: Device) {
    const now = Date.now();
    if (activity.ended) return 'sent';
    if (activity.latest && (event.started_at < activity.latest.started || (
      event.started_at === activity.latest.started && event.sent_at < activity.latest.sent
    ))) return 'sent';
    const timing = activityTiming(event, activity.timing, now);
    activity.latest = { started: event.started_at, sent: event.sent_at };
    if (!timing.send) {
      activity.pending = { event, due: (activity.timing?.lastSent ?? now) + 1000, attempts: 0 };
      await this.ctx.storage.put(key, activity);
      return 'sent';
    }
    // A status transition supersedes a held routine update.
    delete activity.pending;
    const result = await this.sender.send(activityPush(event, device, activity.token, timing.priority, Math.floor(now / 1000)));
    if (result === 'invalid-token') { await this.ctx.storage.delete(key); return 'sent'; }
    if (result === 'sent') {
      activity.timing = { status: event.status, lastSent: now };
      if (event.status === 'done' || event.status === 'failed') {
        // Keep a short completion marker: the plugin sends the reply after progress:end.
        activity.ended = true;
        activity.expires = now + 900_000;
      }
    }
    await this.ctx.storage.put(key, activity);
    return result;
  }

  alarm(): Promise<void> {
    return this.serial(async () => {
      await this.cleanup();
      const registry = await this.registry();
      const activities = await this.ctx.storage.list<Activity>({ prefix: 'activity:' });
      try {
        for (const [key, activity] of activities) {
          const pending = activity.pending;
          if (!pending || pending.due > Date.now()) continue;
          const token = key.split(':')[1];
          const ref = token ? registry[token] : undefined;
          if (!ref) { await this.ctx.storage.delete(key); continue; }
          let result;
          try { result = await this.progress(key, activity, pending.event, await this.device(ref)); }
          catch { result = 'retry'; }
          if (result === 'retry' || result === 'rejected') {
            delete activity.pending;
            if (pending.attempts < 2) activity.pending = { ...pending, due: Date.now() + 2000, attempts: pending.attempts + 1 };
            await this.ctx.storage.put(key, activity);
          }
        }
      } finally { await this.schedule(); }
    });
  }

  private async cleanup() {
    const now = Date.now();
    for (const prefix of ['event:', 'activity:']) {
      const rows = await this.ctx.storage.list<{ expires: number }>({ prefix });
      const expired = [...rows].filter(([, row]) => row.expires <= now).map(([key]) => key);
      for (let offset = 0; offset < expired.length; offset += 128) {
        await this.ctx.storage.delete(expired.slice(offset, offset + 128));
      }
    }
  }

  private async schedule() {
    const events = await this.ctx.storage.list<Receipt>({ prefix: 'event:' });
    const activities = await this.ctx.storage.list<Activity>({ prefix: 'activity:' });
    const due = [
      ...[...events.values()].map(event => event.expires),
      ...[...activities.values()].flatMap(activity => [activity.expires, ...(activity.pending ? [activity.pending.due] : [])]),
    ];
    if (due.length) await this.ctx.storage.setAlarm(Math.max(Date.now() + 1, Math.min(...due)));
    else await this.ctx.storage.deleteAlarm();
  }
}

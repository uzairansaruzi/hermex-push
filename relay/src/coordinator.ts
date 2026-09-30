import { DurableObject } from 'cloudflare:workers';
import { ApnsSender, senderFor, activityPush, bannerPush, endAlertPush, type ApnsSecrets, type SendResult } from './apns';
import { canonicalJson, sha256, type Command, type Device, type NotificationEvent, type ProgressEvent } from './contract';
import { activityTiming, deliveryPolicy, endAlert, type ActivityTiming } from './policy';

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
  /** Newest progress applied; `event` is set once it was sent or held, so a plugin retry of it is a replay. */
  latest?: { started: number; sent: number; event?: string };
  /** The end event whose alert APNs accepted (reserved before the send), so a plugin retry after a failed `end` sends only the `end`. */
  alerted?: string;
  ended?: boolean;
  pending?: { event: ProgressEvent; due: number; attempts: number };
}
export interface Outcome { status: number; result: string }
const ok = (result = 'ok'): Outcome => ({ status: 200, result });

/**
 * `due:<ms>:<key>` index rows (value: the target key) sort by time, so the alarm lists only what is due
 * and the next alarm is the first index row. Entries are hints: the sweep rechecks the target row.
 */
const dueKey = (at: number, key = '') => `due:${String(at).padStart(15, '0')}:${key}`;
// Expiry sweeps round up to the minute so a busy install's receipts share one alarm.
const sweepAt = (expires: number) => Math.ceil(expires / 60_000) * 60_000;
// Marks the legacy pass done. Event ids are hex, so this key never collides with a receipt.
const indexedKey = 'event:~indexed';
// SHA-256 of the canonical record the device's KV revision holds; written with its registry pointer.
const digestKey = (token: string) => `digest:${token}`;

/** One serial owner per install. KV values are immutable; durable pointers make deletes immediate. */
export class InstallCoordinator extends DurableObject<Env> {
  private tail: Promise<unknown> = Promise.resolve();
  private sender: ApnsSender;
  private indexed = false;
  /** Earliest due time indexed by the current serial unit; `wake()` moves the alarm to it once. */
  private wakeAt?: number;

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
        try { return await this.execute(installHash, command); }
        finally { await this.wake(); }
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
    // Bounded by the 64-activity cap; runs only on revocation. Dropping the digest makes the next registration write.
    const activities = await this.ctx.storage.list({ prefix: `activity:${token}:` });
    await this.ctx.storage.delete([...activities.keys(), digestKey(token)]);
    if (key) await this.env.hermex_relay.delete(key);
  }

  private async activityKey(token: string, session: string) {
    return `activity:${token}:${await sha256(session)}`;
  }

  /** Expired rows linger until the next sweep, so reads apply expiry exactly. */
  private async activity(key: string, now: number) {
    const activity = await this.ctx.storage.get<Activity>(key);
    return activity && activity.expires > now ? activity : undefined;
  }

  /** Atomically writes rows plus a due-index entry for `key`; the alarm moves once the serial unit ends. */
  private async putDue(rows: Record<string, unknown>, key: string, at: number) {
    this.wakeAt = Math.min(this.wakeAt ?? at, at);
    await this.ctx.storage.put({ ...rows, [dueKey(at, key)]: key });
  }

  /**
   * Moves the alarm earlier when the request indexed something due before it. Running once after
   * the fan-out means concurrent holds can't overwrite each other's earlier alarm.
   */
  private async wake() {
    const at = this.wakeAt;
    this.wakeAt = undefined;
    if (at === undefined) return;
    const alarm = await this.ctx.storage.getAlarm();
    if (alarm === null || alarm > at) await this.ctx.storage.setAlarm(at);
  }

  private async execute(installHash: string, command: Command): Promise<Outcome> {
    const registry = await this.registry();
    switch (command.action) {
      case 'register': {
        const token = command.device.device_token;
        const previous = registry[token];
        if (!previous && Object.keys(registry).length >= 32) return { status: 409, result: 'device_limit' };
        const digest = await sha256(canonicalJson(command.device));
        // The app re-registers on every launch; an identical record skips the paid KV put and delete.
        if (previous && await this.ctx.storage.get<string>(digestKey(token)) === digest) return ok();
        const key = `installs:${installHash}:devices:${token}:${crypto.randomUUID()}`;
        // Unique revisions avoid KV's one-write-per-key-per-second restriction.
        await this.env.hermex_relay.put(key, JSON.stringify(command.device));
        registry[token] = key;
        // One call keeps the pointer and the digest of the record it points to atomic.
        await this.ctx.storage.put({ devices: registry, [digestKey(token)]: digest });
        if (previous) await this.env.hermex_relay.delete(previous);
        return ok();
      }
      case 'delete-device':
        await this.removeDevice(command.token, registry);
        return ok();
      case 'put-activity': {
        if (!registry[command.token]) return { status: 404, result: 'device_not_found' };
        const now = Date.now();
        const key = await this.activityKey(command.token, command.session);
        const current = await this.activity(key, now);
        if (!current) {
          // Bounded by the cap itself; renewals of an existing activity skip it.
          const activities = await this.ctx.storage.list<Activity>({ prefix: `activity:${command.token}:` });
          if ([...activities.values()].filter(activity => activity.expires > now).length >= 64) return { status: 409, result: 'activity_limit' };
        }
        const activity: Activity = {
          ...(current?.token === command.activityToken ? current : {}),
          token: command.activityToken, expires: now + 8 * 60 * 60 * 1000,
        };
        await this.putDue({ [key]: activity }, key, sweepAt(activity.expires));
        return ok();
      }
      case 'delete-activity': {
        const key = await this.activityKey(command.token, command.session);
        const current = await this.activity(key, Date.now());
        // The phone may acknowledge an end before the plugin's reply arrives.
        if (current?.ended) await this.ctx.storage.put(key, { ...current, token: '' });
        else await this.ctx.storage.delete(key);
        return ok();
      }
      case 'notify':
        return command.event.kind === 'progress'
          ? this.notifyProgress(command.event, registry)
          : this.notify(installHash, command.event, registry);
    }
  }

  private async notify(installHash: string, event: NotificationEvent, registry: Registry): Promise<Outcome> {
    const now = Date.now();
    const key = `event:${event.event_id}`;
    const stored = await this.ctx.storage.get<Receipt>(key);
    const existing = stored && stored.expires > now ? stored : undefined;
    if (existing?.finished) return ok('deduplicated');
    // A counter row tracks stored receipts so the cap never lists them.
    const receipts = await this.ctx.storage.get<number>('receipts') ?? 0;
    if (!existing && receipts >= 4096) return { status: 429, result: 'event_limit' };
    const receipt = existing ?? { expires: now + 600_000, completed: [] };
    // Read all KV revisions before making irreversible sends; missing KV data is retryable.
    const devices = await Promise.all(Object.entries(registry).map(async ([token, ref]) => ({ token, device: await this.device(ref) })));
    let retry = false;
    let rejected = false;
    const targets = await Promise.all(devices.filter(({ token }) => !receipt.completed.includes(token)).map(async ({ token, device }) => {
      const activity = await this.activity(await this.activityKey(token, event.session_id), now);
      return { token, device, policy: deliveryPolicy(event, device, !!activity, now / 1000) };
    }));
    // Reserve before APNs. A crash after acceptance cannot cause a replay to send twice.
    receipt.completed.push(...targets.map(target => target.token));
    if (existing) await this.ctx.storage.put(key, receipt);
    // An expired row that was not swept yet is replaced, not counted again.
    else await this.putDue({ [key]: receipt, receipts: stored ? receipts : receipts + 1 }, key, sweepAt(receipt.expires));
    // Concurrent fan-out keeps one slow phone from exhausting the plugin's 10-second timeout.
    const deliveries = await Promise.allSettled(targets.map(({ device, policy }) =>
      policy.type === 'banner' ? this.sender.send(bannerPush(event, device, installHash, policy, Math.floor(now / 1000))) : 'sent'));
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

  /** Progress keeps no receipt: each activity's `latest` orders events and recognizes replays. */
  private async notifyProgress(event: ProgressEvent, registry: Registry): Promise<Outcome> {
    const now = Date.now();
    const found = await Promise.all(Object.entries(registry).map(async ([token, ref]) => {
      const key = await this.activityKey(token, event.session_id);
      let activity = await this.activity(key, now);
      if (activity?.ended && (
        event.started_at > (activity.latest?.started ?? 0) || (
          event.status === 'running' && event.started_at === activity.latest?.started && event.sent_at >= activity.latest.sent
        )
      )) {
        await this.ctx.storage.delete(key);
        activity = undefined; // A new turn needs a new phone-created activity token.
      }
      return { ref, key, activity };
    }));
    const active = found.flatMap(({ ref, key, activity }) => activity ? [{ ref, key, activity }] : []);
    // Only devices with an activity need their KV revision; read them all before any send.
    const targets = await Promise.all(active.map(async target => ({ ...target, device: await this.device(target.ref) })));
    const deliveries = await Promise.allSettled(targets.map(({ key, activity, device }) =>
      deliveryPolicy(event, device, true, now / 1000).type === 'activity' ? this.progress(key, activity, event, device) : 'sent'));
    const results = deliveries.map(delivery => delivery.status === 'fulfilled' ? delivery.value : 'retry');
    if (results.some(result => result === 'retry' || result === 'rejected')) {
      return { status: 503, result: results.includes('rejected') ? 'apns_rejected' : 'delivery_retry' };
    }
    return ok('accepted');
  }

  /** `held` flushes the activity's own pending event, which is already its `latest`. */
  private async progress(key: string, activity: Activity, event: ProgressEvent, device: Device, held = false): Promise<SendResult> {
    const now = Date.now();
    if (activity.ended) return 'sent';
    const latest = activity.latest;
    // Equal pairs are same-second status changes, unless it is the event already applied (a replay).
    if (!held && latest && (event.started_at < latest.started || (event.started_at === latest.started && (
      event.sent_at < latest.sent || (event.sent_at === latest.sent && event.event_id === latest.event)
    )))) return 'sent';
    const timing = activityTiming(event, activity.timing, now);
    activity.latest = { started: event.started_at, sent: event.sent_at };
    if (!timing.send) {
      const due = (activity.timing?.lastSent ?? now) + 1000;
      const scheduled = activity.pending?.due === due;
      activity.pending = { event, due, attempts: 0 };
      activity.latest.event = event.event_id;
      if (scheduled) await this.ctx.storage.put(key, activity);
      else await this.putDue({ [key]: activity }, key, due);
      return 'sent';
    }
    // A status transition supersedes a held routine update.
    delete activity.pending;
    const seconds = Math.floor(now / 1000);
    let result: SendResult = 'sent';
    // The run's end alerts once, on an update awaited ahead of the silent `end` so the relay sends them in order.
    const alert = endAlert(event, device, seconds);
    if (alert && activity.alerted !== event.event_id) {
      // Reserve before APNs, as banners do: a request that dies after acceptance cannot alert twice.
      activity.alerted = event.event_id;
      await this.ctx.storage.put(key, activity);
      result = await this.sender.send(endAlertPush(event, device, activity.token, alert, seconds));
      if (result !== 'sent') delete activity.alerted;
    }
    if (result === 'sent') result = await this.sender.send(activityPush(event, device, activity.token, timing.priority, seconds));
    if (result === 'invalid-token') { await this.ctx.storage.delete(key); return 'sent'; }
    if (result === 'sent') {
      activity.latest.event = event.event_id;
      activity.timing = { status: event.status, lastSent: now };
      if (event.status === 'done' || event.status === 'failed') {
        // Keep a short completion marker: the plugin sends the reply after progress:end.
        activity.ended = true;
        activity.expires = now + 900_000;
        await this.putDue({ [key]: activity }, key, sweepAt(activity.expires));
        return result;
      }
    }
    await this.ctx.storage.put(key, activity);
    return result;
  }

  alarm(): Promise<void> {
    return this.serial(async () => {
      try {
        await this.indexLegacy();
        await this.sweep();
      } finally {
        // The first index row holds the next due time (`due:` plus 15 digits), including anything the sweep indexed.
        this.wakeAt = undefined;
        const [next] = await this.ctx.storage.list<string>({ prefix: 'due:', limit: 1 });
        if (next) await this.ctx.storage.setAlarm(Number(next[0].slice(4, 19)));
      }
    });
  }

  /** Flushes held progress and deletes expired rows; reads only index entries that are due. */
  private async sweep() {
    let registry: Registry | undefined;
    while (true) {
      const now = Date.now();
      // 64 entries keep each batch's deletes (entries plus targets) within one 128-key call.
      const entries = await this.ctx.storage.list<string>({ start: 'due:', end: dueKey(now + 1), limit: 64 });
      if (!entries.size) return;
      const keys = [...new Set(entries.values())];
      const receipts = await this.ctx.storage.get<Receipt>(keys.filter(key => key.startsWith('event:')));
      const activities = await this.ctx.storage.get<Activity>(keys.filter(key => key.startsWith('activity:')));
      const removed = [...receipts, ...activities].filter(([, row]) => row.expires <= now).map(([key]) => key);
      for (const [key, activity] of activities) {
        const pending = activity.pending;
        if (activity.expires <= now || !pending || pending.due > now) continue;
        registry ??= await this.registry();
        const ref = registry[key.split(':')[1] ?? ''];
        if (!ref) { removed.push(key); continue; }
        let result;
        try { result = await this.progress(key, activity, pending.event, await this.device(ref), true); }
        catch { result = 'retry'; }
        if (result === 'retry' || result === 'rejected') {
          delete activity.pending;
          if (pending.attempts >= 2) await this.ctx.storage.put(key, activity);
          else {
            activity.pending = { ...pending, due: now + 2000, attempts: pending.attempts + 1 };
            await this.putDue({ [key]: activity }, key, activity.pending.due);
          }
        }
      }
      const swept = removed.filter(key => key.startsWith('event:')).length;
      const count = swept ? await this.ctx.storage.get<number>('receipts') ?? 0 : 0;
      // Issued without an await between them, so the deletes and the counter commit together.
      await Promise.all([
        this.ctx.storage.delete([...entries.keys(), ...removed]),
        ...(swept ? [this.ctx.storage.put('receipts', Math.max(0, count - swept))] : []),
      ]);
      if (entries.size < 64) return;
    }
  }

  /**
   * Indexes rows written before the due index existed and recounts receipts, once per object. The
   * marker is an expired `event:` row, which an earlier version's cleanup deletes on its first
   * request, so after a rollback and redeploy the pass reruns and picks up what that version wrote.
   */
  private async indexLegacy() {
    if (this.indexed || await this.ctx.storage.get(indexedKey)) { this.indexed = true; return; }
    const now = Date.now();
    const expired: string[] = [];
    const rows: [string, unknown][] = [];
    let receipts = 0;
    for (const prefix of ['event:', 'activity:']) {
      for (const [key, row] of await this.ctx.storage.list<{ expires: number; pending?: { due: number } }>({ prefix })) {
        if (row.expires <= now) { expired.push(key); continue; }
        if (prefix === 'event:') receipts++;
        rows.push([dueKey(sweepAt(row.expires), key), key]);
        if (row.pending) rows.push([dueKey(row.pending.due, key), key]);
      }
    }
    for (let offset = 0; offset < expired.length; offset += 128) {
      await this.ctx.storage.delete(expired.slice(offset, offset + 128));
    }
    for (let offset = 0; offset < rows.length; offset += 128) {
      await this.ctx.storage.put(Object.fromEntries(rows.slice(offset, offset + 128)));
    }
    // Written last: an interrupted pass reruns, and its index writes are idempotent.
    await this.ctx.storage.put({ receipts, [indexedKey]: { expires: 0 } });
    this.indexed = true;
  }
}

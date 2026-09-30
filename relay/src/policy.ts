import type { Device, NotificationEvent, PushEvent, ProgressEvent } from './contract';

/** `hold` is how many seconds APNs may store the banner while the phone is offline. */
export type Delivery =
  | { type: 'none' }
  | { type: 'activity' }
  | { type: 'banner'; interruption: 'active' | 'time-sensitive'; collapseId: string; threadId: string; preview: boolean; hold: number };

// An approval is moot after the host's five-minute approval timeout, a question after its one-hour
// clarify timeout; a reply or failure is still news an hour later.
const holds: Record<NotificationEvent['kind'], number> = { approval: 300, clarify: 3600, reply: 3600, turn_error: 3600 };

/**
 * All per-device delivery etiquette lives here; no storage, clocks, or network calls.
 * A session's activity replaces its reply and error banners, but approvals and questions
 * still banner: the activity's silent `waiting` update alone would leave the agent blocked unseen.
 */
export function deliveryPolicy(event: PushEvent, device: Device, hasActivity: boolean, now: number): Delivery {
  if (event.kind === 'progress') return hasActivity ? { type: 'activity' } : { type: 'none' };
  if (hasActivity && event.kind !== 'approval' && event.kind !== 'clarify') return { type: 'none' };
  const prefs = device.prefs;
  if (event.is_subagent && prefs.mute_subagents) return { type: 'none' };
  if (event.kind === 'reply' && (!prefs.replies || (
    prefs.presence_suppression && prefs.active_session_id === event.session_id && prefs.active_until > now
  ))) return { type: 'none' };
  return {
    type: 'banner', interruption: event.kind === 'reply' ? 'active' : 'time-sensitive',
    collapseId: event.collapse_id, threadId: event.thread_id, preview: prefs.previews, hold: holds[event.kind],
  };
}

export interface ActivityTiming { status: ProgressEvent['status']; lastSent: number }
export function activityTiming(event: ProgressEvent, previous: ActivityTiming | undefined, now: number) {
  const statusChanged = !previous || previous.status !== event.status;
  return { send: statusChanged || now - previous.lastSent >= 1000, priority: statusChanged ? '10' : '5' } as const;
}

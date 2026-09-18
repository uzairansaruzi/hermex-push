import { deviceSchema, type NotificationEvent, type ProgressEvent } from '../src/contract';

export const installKey = '1'.repeat(64);
export const device = deviceSchema.parse({
  device_token: 'a'.repeat(64), bundle_id: 'com.uzairansar.hermesmobile', environment: 'sandbox',
});
export const notification: NotificationEvent = {
  v: 1, kind: 'reply', event_id: 'b'.repeat(32), thread_id: 'c'.repeat(32), collapse_id: 'd'.repeat(32),
  session_id: 'session/one', source: 'bot', is_subagent: false, sent_at: 1_800_000_000,
  sealed: btoa('nonce-and-ciphertext-test-fixture'),
};
export const progress: ProgressEvent = {
  v: 1, kind: 'progress', event_id: 'e'.repeat(32), thread_id: notification.thread_id,
  session_id: notification.session_id, source: 'bot', is_subagent: false, sent_at: notification.sent_at,
  status: 'running', tool: 'terminal', tool_calls: 1, started_at: notification.sent_at - 10,
};

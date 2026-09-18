import { z } from 'zod';

export const tokenSchema = z.string().regex(/^(?:[a-f0-9]{2}){16,256}$/);
export const sessionSchema = z.string().min(1).max(256);
export const prefsSchema = z.object({
  replies: z.boolean().default(true),
  mute_subagents: z.boolean().default(true),
  previews: z.boolean().default(true),
  presence_suppression: z.boolean().default(false),
  // A short lease refreshed by the foreground app; never a sticky foreground flag.
  active_session_id: sessionSchema.nullable().default(null),
  active_until: z.number().int().nonnegative().default(0),
}).strict();
export const deviceSchema = z.object({
  device_token: tokenSchema,
  bundle_id: z.enum(['com.uzairansar.hermesmobile', 'com.uzairansar.hermesmobile.branch']),
  environment: z.enum(['sandbox', 'production']),
  prefs: prefsSchema.prefault({}),
}).strict();
export type Device = z.infer<typeof deviceSchema>;

const id = z.string().regex(/^[a-f0-9]{32}$/);
const seconds = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const common = {
  v: z.literal(1), event_id: id, thread_id: id, session_id: sessionSchema,
  source: z.enum(['bot', 'webui', 'other']), is_subagent: z.boolean(), sent_at: seconds,
};
const notificationSchema = z.object({
  ...common,
  kind: z.enum(['reply', 'approval', 'clarify', 'turn_error']),
  collapse_id: id,
  sealed: z.string().min(40).max(12000).regex(/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/).nullable(),
}).strict();
const progressSchema = z.object({
  ...common, kind: z.literal('progress'),
  status: z.enum(['running', 'waiting', 'done', 'failed']),
  tool: z.string().min(1).max(128).regex(/^[a-zA-Z0-9_.:/-]+$/).nullable(),
  tool_calls: seconds, started_at: seconds,
}).strict();
export const eventSchema = z.union([notificationSchema, progressSchema]);
export const activitySchema = z.object({ activity_token: tokenSchema }).strict();
export type PushEvent = z.infer<typeof eventSchema>;
export type ProgressEvent = z.infer<typeof progressSchema>;
export type NotificationEvent = z.infer<typeof notificationSchema>;
export const knownKinds = new Set(['reply', 'approval', 'clarify', 'turn_error', 'progress']);

export type Command =
  | { action: 'register'; device: Device }
  | { action: 'delete-device'; token: string }
  | { action: 'put-activity'; token: string; session: string; activityToken: string }
  | { action: 'delete-activity'; token: string; session: string }
  | { action: 'notify'; event: PushEvent };

export async function sha256(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('');
}

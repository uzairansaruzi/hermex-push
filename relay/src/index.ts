import { activitySchema, deviceSchema, eventSchema, knownKinds, sessionSchema, sha256, tokenSchema, type Command } from './contract';
import type { Env, Outcome } from './coordinator';
export { InstallCoordinator } from './coordinator';

function response(status: number, result: string) {
  return Response.json({ result }, { status, headers: { 'cache-control': 'no-store' } });
}

async function readBody(request: Request): Promise<unknown> {
  if (request.headers.get('content-type')?.split(';')[0]?.trim() !== 'application/json') throw new Error('json_required');
  if (!request.body) throw new Error('invalid_json');
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > 16384) { await reader.cancel(); throw new Error('body_too_large'); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/health' && request.method === 'GET') return response(200, 'ok');
    if (url.protocol !== 'https:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) return response(400, 'https_required');
    const match = /^\/installs\/([a-f0-9]{64})\/(notify|devices)(?:\/([^/]+)(?:\/activities\/([^/]+))?)?$/.exec(url.pathname);
    if (!match) return response(404, 'not_found');
    const [, installKey, route, rawToken, rawSession] = match;
    if (!installKey) return response(404, 'not_found');
    let command: Command;
    try {
      if (route === 'notify' && !rawToken && request.method === 'POST') {
        const body = await readBody(request);
        if (body && typeof body === 'object' && 'kind' in body && typeof body.kind === 'string' && !knownKinds.has(body.kind)) return response(200, 'ignored');
        command = { action: 'notify', event: eventSchema.parse(body) };
      } else if (route === 'devices' && !rawToken && request.method === 'POST') {
        const device = deviceSchema.parse(await readBody(request));
        // Presence leases cannot silently suppress a session indefinitely.
        device.prefs.active_until = Math.min(device.prefs.active_until, Math.floor(Date.now() / 1000) + 120);
        command = { action: 'register', device };
      } else if (route === 'devices' && rawToken) {
        const token = tokenSchema.parse(rawToken);
        if (rawSession) {
          const session = sessionSchema.parse(decodeURIComponent(rawSession));
          if (request.method === 'PUT') command = { action: 'put-activity', token, session, activityToken: activitySchema.parse(await readBody(request)).activity_token };
          else if (request.method === 'DELETE') command = { action: 'delete-activity', token, session };
          else return response(405, 'method_not_allowed');
        } else if (request.method === 'DELETE') command = { action: 'delete-device', token };
        else return response(405, 'method_not_allowed');
      } else return response(405, 'method_not_allowed');
    } catch (error) {
      return response(error instanceof Error && error.message === 'body_too_large' ? 413 : 400, 'invalid_request');
    }
    try {
      const installHash = await sha256(installKey);
      const outcome: Outcome = await env.INSTALLS.get(env.INSTALLS.idFromName(installHash)).handle(installHash, command);
      return response(outcome.status, outcome.result);
    } catch {
      return response(503, 'temporarily_unavailable');
    }
  },
} satisfies ExportedHandler<Env>;

import { describe, expect, it } from 'vitest';
import { activityTiming, deliveryPolicy, endAlert } from '../src/policy';
import { device, notification, progress } from './fixtures';

describe('delivery policy', () => {
  it('collapses and threads replies, but makes attention requests time-sensitive', () => {
    expect(deliveryPolicy(notification, device, false, 0)).toEqual({ type: 'banner', interruption: 'active', collapseId: notification.collapse_id, threadId: notification.thread_id, preview: true, hold: 3600 });
    for (const kind of ['approval', 'clarify', 'turn_error'] as const) {
      expect(deliveryPolicy({ ...notification, kind }, device, false, 0)).toMatchObject({ type: 'banner', interruption: 'time-sensitive' });
    }
  });
  it('lets an activity replace reply and error banners but not attention requests, and never banners progress', () => {
    for (const kind of ['reply', 'turn_error'] as const) expect(deliveryPolicy({ ...notification, kind }, device, true, 0)).toEqual({ type: 'none' });
    for (const [kind, hold] of [['approval', 300], ['clarify', 3600]] as const) {
      expect(deliveryPolicy({ ...notification, kind }, device, true, 0)).toEqual({ type: 'banner', interruption: 'time-sensitive', collapseId: notification.collapse_id, threadId: notification.thread_id, preview: true, hold });
    }
    expect(deliveryPolicy({ ...notification, kind: 'approval', is_subagent: true }, device, true, 0)).toEqual({ type: 'none' });
    expect(deliveryPolicy(progress, device, true, 0)).toEqual({ type: 'activity' });
    expect(deliveryPolicy(progress, device, false, 0)).toEqual({ type: 'none' });
  });
  it('applies reply, subagent, and preview preferences separately', () => {
    const muted = { ...device, prefs: { ...device.prefs, replies: false, previews: false } };
    expect(deliveryPolicy(notification, muted, false, 0)).toEqual({ type: 'none' });
    expect(deliveryPolicy({ ...notification, kind: 'approval' }, muted, false, 0)).toMatchObject({ type: 'banner', preview: false });
    expect(deliveryPolicy({ ...notification, is_subagent: true }, device, false, 0)).toEqual({ type: 'none' });
    expect(deliveryPolicy({ ...notification, is_subagent: true }, { ...device, prefs: { ...device.prefs, mute_subagents: false } }, false, 0)).toMatchObject({ type: 'banner' });
  });
  it('suppresses replies only for an opted-in, present session; activities keep updating', () => {
    const present = { ...device, prefs: { ...device.prefs, presence_suppression: true, active_session_id: notification.session_id, active_until: 100 } };
    expect(deliveryPolicy(notification, present, false, 99)).toEqual({ type: 'none' });
    expect(deliveryPolicy(notification, present, false, 100)).toMatchObject({ type: 'banner' });
    expect(deliveryPolicy({ ...notification, session_id: 'other' }, present, false, 99)).toMatchObject({ type: 'banner' });
    expect(deliveryPolicy({ ...notification, kind: 'approval' }, present, false, 99)).toMatchObject({ type: 'banner' });
    expect(deliveryPolicy(progress, present, true, 99)).toEqual({ type: 'activity' });
  });
  it('holds routine progress for one second, but always sends status changes at priority 10', () => {
    const previous = { status: 'running', lastSent: 1000 } as const;
    expect(activityTiming(progress, undefined, 1000)).toEqual({ send: true, priority: '10' });
    expect(activityTiming(progress, previous, 1999)).toEqual({ send: false, priority: '5' });
    expect(activityTiming(progress, previous, 2000)).toEqual({ send: true, priority: '5' });
    for (const status of ['waiting', 'done', 'failed'] as const) expect(activityTiming({ ...progress, status }, previous, 1001)).toEqual({ send: true, priority: '10' });
  });
});

// `done` follows the reply banner's rules and `failed` the turn-error banner's (hermex#888).
describe('end alert', () => {
  const present = { presence_suppression: true, active_session_id: progress.session_id, active_until: 100 };
  it.each([
    ['done', {}, false, 'complete'],
    ['done', { replies: false }, false, null],
    ['done', {}, true, null],
    ['done', { mute_subagents: false }, true, 'complete'],
    ['done', present, false, null],
    ['failed', {}, false, 'failed'],
    ['failed', { replies: false }, false, 'failed'],
    ['failed', {}, true, null],
    ['failed', { mute_subagents: false }, true, 'failed'],
    ['failed', present, false, 'failed'],
  ] as const)('%s with prefs %o and subagent %s alerts %s', (status, prefs, isSubagent, expected) => {
    expect(endAlert({ ...progress, status, is_subagent: isSubagent }, { ...device, prefs: { ...device.prefs, ...prefs } }, 99)).toBe(expected);
  });
  it('never alerts running or waiting progress, and a lapsed presence lease no longer silences done', () => {
    for (const status of ['running', 'waiting'] as const) expect(endAlert({ ...progress, status }, device, 0)).toBeNull();
    expect(endAlert({ ...progress, status: 'done' }, { ...device, prefs: { ...device.prefs, ...present } }, 100)).toBe('complete');
  });
});

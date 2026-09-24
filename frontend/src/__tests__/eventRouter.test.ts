/**
 * EventRouter 测试：WS 事件 → SessionStore 投影路由、seq 去重、断线补洞。
 * （U8：替代随死钩子 useAgentEvents 删除的旧测试——本文件覆盖的是真实存活管线，
 * 也是 TUI 复用 frontend 状态层的行为契约。）
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { eventRouter } from '../store/EventRouter';
import { sessionStore } from '../store/SessionStore';

const route = (event: Record<string, unknown>) =>
  (eventRouter as unknown as { routeEvent(e: Record<string, unknown>): void }).routeEvent(event);

let counter = 0;
const nextSession = () => `test-s-${Date.now()}-${counter++}`;

describe('EventRouter', () => {
  const received: Record<string, unknown>[] = [];
  let unsub: () => void;

  beforeEach(() => {
    received.length = 0;
    unsub = eventRouter.subscribe((e) => received.push(e));
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    unsub();
  });

  it('routes session/created and turn projections', () => {
    const s = nextSession();
    route({ type: 'session/created', session_id: s, cwd: '/tmp/x', seq: 0 });
    const state = sessionStore.getOrCreate(s);
    expect(state.projections.cwd).toBe('/tmp/x');
    expect(state.projections.running).toBe(true);

    route({ type: 'turn/end', session_id: s, seq: 1, turn: 1, reason: 'completed' });
    expect(sessionStore.getOrCreate(s).projections.running).toBe(false);
  });

  it('turn/end must not clobber title projection (event carries no title)', () => {
    const s = nextSession();
    route({ type: 'session/title', session_id: s, seq: 0, title: 'T' });
    route({ type: 'turn/end', session_id: s, seq: 1, turn: 1, reason: 'completed' });
    expect(sessionStore.getOrCreate(s).projections.title).toBe('T');
  });

  it('dedupes replayed events by seq', () => {
    const s = nextSession();
    route({ type: 'turn/start', session_id: s, seq: 5 });
    received.length = 0;
    route({ type: 'text', session_id: s, seq: 3, content: 'stale' });
    expect(received.length).toBe(0);
    expect(sessionStore.getOrCreate(s).lastSeq).toBe(5);
  });

  it('ignores events without session_id', () => {
    route({ type: 'eval/run_started', seq: 0 });
    expect(received.length).toBe(0);
  });

  it('detects seq gap and repairs via HTTP replay', async () => {
    const s = nextSession();
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        events: [{ type: 'text', session_id: s, seq: 1, content: 'missed' }],
      }),
    }));
    vi.stubGlobal('fetch', fetchMock);

    route({ type: 'turn/start', session_id: s, seq: 0 });
    route({ type: 'text', session_id: s, seq: 3, content: 'live' });

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `/api/sessions/${s}/events?from_seq=1&to_seq=3`,
    );
    await vi.waitFor(() =>
      expect(received.some((e) => e.content === 'missed')).toBe(true),
    );
  });
});

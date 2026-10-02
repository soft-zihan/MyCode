/**
 * 缺陷 B：待审批状态从历史恢复（derivePendingPermission）。
 *
 * 每条都对着「把实现改坏，这条会不会红」写：
 * - 无条件恢复的实现 → 「有 resolved」与「running=false」两条会红；
 * - 从不恢复的实现 → 第一条会红；
 * - 用第一条 request（而非最后一条）的实现 → 「取最后一条」会红；
 * - 不看 rpc_id 配对、见 resolved 就清空的实现 → 「不同 rpc_id」会红；
 * - 恢复结果形状不对（缺 request_id/message 兜底）→ 字段断言会红。
 * 「切会话清空」钉住 useChat 历史加载分支的写回语义：派生结果为 undefined 时
 * 也必须显式 setPendingPermission(sessionId, undefined)，B 槽位绝不带 A 的对话框。
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { derivePendingPermission } from '../pages/ChatPage/hooks/restorePendingPermission';
import { sessionStore } from '../store/SessionStore';
import type { PermissionRequest } from '../store/SessionStore';

const req = (rpc: string, over: Record<string, any> = {}) => ({
  type: 'permission/request',
  rpc_id: rpc,
  request_id: rpc,
  command: 'rm -rf /',
  tool_name: 'shell',
  ...over,
});
const resolved = (rpc: string, outcome = 'allowed') => ({
  type: 'permission/resolved',
  rpc_id: rpc,
  outcome,
});

describe('derivePendingPermission', () => {
  it('未决议的 request + running=true → 恢复成 pending（形状与实时分支一致）', () => {
    const p = derivePendingPermission(
      [{ type: 'user_message' }, req('r1'), { type: 'text' }],
      true,
    );
    expect(p).toBeDefined();
    expect(p).toEqual({
      rpc_id: 'r1',
      request_id: 'r1',
      command: 'rm -rf /',
      tool_name: 'shell',
      message: '',
      sub_agent_id: undefined,
    } satisfies PermissionRequest);
  });

  it('有同 rpc_id 的 resolved → 不恢复（无条件恢复的实现在这里红）', () => {
    expect(derivePendingPermission([req('r1'), resolved('r1')], true)).toBeUndefined();
    expect(derivePendingPermission([req('r1'), resolved('r1', 'auto')], true)).toBeUndefined();
    expect(derivePendingPermission([req('r1'), resolved('r1', 'aborted')], true)).toBeUndefined();
  });

  it('running=false → 不恢复（会话没在跑，agent 不可能还在等）', () => {
    expect(derivePendingPermission([req('r1')], false)).toBeUndefined();
  });

  it('没有 request → undefined', () => {
    expect(derivePendingPermission([{ type: 'text' }], true)).toBeUndefined();
    expect(derivePendingPermission([], true)).toBeUndefined();
  });

  it('取最后一条 request；更早的已决议请求不干扰', () => {
    const p = derivePendingPermission([req('r1'), resolved('r1'), req('r2')], true);
    expect(p?.rpc_id).toBe('r2');
  });

  it('不同 rpc_id 的 resolved 不清空新请求（配对看 rpc_id，不是见 resolved 就清）', () => {
    const p = derivePendingPermission([req('r1'), resolved('r1'), req('r2'), resolved('rX')], true);
    expect(p?.rpc_id).toBe('r2');
  });

  it('request_id 缺失时回退 rpc_id；message/sub_agent_id 原样带过', () => {
    const p = derivePendingPermission(
      [req('r9', { request_id: undefined, message: 'allow?', sub_agent_id: 'sub-1' })],
      true,
    );
    expect(p?.request_id).toBe('r9');
    expect(p?.message).toBe('allow?');
    expect(p?.sub_agent_id).toBe('sub-1');
  });
});

describe('切会话清空（useChat 历史加载分支的写回语义）', () => {
  beforeEach(() => {
    sessionStore.setPendingPermission('A', undefined);
    sessionStore.setPendingPermission('B', undefined);
  });

  it('从有待审批的会话 A 切到已决议/空闲的会话 B：B 的 pending 被显式清空，A 的槽位不受影响', () => {
    const pendingA: PermissionRequest = {
      rpc_id: 'rA', request_id: 'rA', command: 'rm -rf /', tool_name: 'shell',
    };
    sessionStore.setPendingPermission('A', pendingA);
    expect(sessionStore.getPendingPermission('A')).toBeDefined();

    // useChat 切到 B：loadSessionEvents 之后无条件写回派生结果（可能是 undefined）
    const eventsB = [req('rB'), resolved('rB')];
    sessionStore.setPendingPermission('B', derivePendingPermission(eventsB, true));

    expect(sessionStore.getPendingPermission('B')).toBeUndefined();
    // A 的状态住在 A 自己的槽位里；当前视图读的是 B 的槽位，对话框不会跟过来
    expect(sessionStore.getPendingPermission('A')).toEqual(pendingA);
  });

  it('切到 running=false 的会话：即使历史里有未决议 request 也不恢复', () => {
    sessionStore.setPendingPermission('C', derivePendingPermission([req('rC')], false));
    expect(sessionStore.getPendingPermission('C')).toBeUndefined();
  });
});

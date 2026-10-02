import type { PermissionRequest } from '../../../store/SessionStore';

/**
 * 缺陷 B 修复：从历史事件恢复「仍在等待的审批」。
 *
 * 刷新页面 / 换标签页 / 换设备打开时，会话可能恰恰是活跃的（projections.running
 * === true）、agent 正阻塞等审批——旧代码直接丢弃历史里的 permission/request，
 * 于是审批对话框永远点不出来。
 *
 * 判定规则：找**最后一条** `permission/request`；若其后没有同 `rpc_id` 的
 * `permission/resolved`（resolved 是 INTERNAL 事件、不走 WS，但历史 HTTP 读取
 * 能看到），且会话仍在运行，就恢复成 pending；否则返回 undefined（调用方要
 * **显式清空** store，不然从会话 A 切到会话 B 会把 A 的对话框带过来）。
 *
 * running=false 时一律不恢复：会话没在跑，agent 不可能还在等，遗留的
 * request 只是历史残影。
 */
export function derivePendingPermission(
  events: Array<Record<string, any>>,
  running: boolean,
): PermissionRequest | undefined {
  if (!running) return undefined;

  let lastRequest: Record<string, any> | undefined;
  for (const ev of events) {
    if (ev && ev.type === 'permission/request') lastRequest = ev;
  }
  if (!lastRequest) return undefined;

  const rpcId = lastRequest.rpc_id;
  for (const ev of events) {
    // rpc_id 每次请求都是新 uuid，全量扫描等价于「其后」扫描。
    if (ev && ev.type === 'permission/resolved' && ev.rpc_id === rpcId) return undefined;
  }

  // 字段形状与 useChat 实时分支（eventType === 'permission/request'）保持一致。
  return {
    rpc_id: lastRequest.rpc_id,
    request_id: lastRequest.request_id || lastRequest.rpc_id,
    command: lastRequest.command,
    tool_name: lastRequest.tool_name,
    message: lastRequest.message || '',
    sub_agent_id: lastRequest.sub_agent_id,
  };
}

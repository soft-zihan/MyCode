import { memo, useState } from 'react';
import { Bot, Loader2, XCircle } from 'lucide-react';
import { cancelSubagent } from '../../api/client';
import type { SubAgentNode } from './nodes/types';

interface BackgroundTasksPanelProps {
  tasks: SubAgentNode[];
  sessionId?: string;
}

/** U3a：后台任务面板——纯从事件派生（sub_agent/start + 后台标记 + 未见 sub_agent/end），
 *  零专用 API（v2 background.ts:21-33 模式）。完成后任务自动消失，结果以完成卡片出现在会话流中。
 *  U4：每行加硬中止按钮（软 abort + 硬 cancel，子会话仍可续跑）。 */
export const BackgroundTasksPanel = memo(function BackgroundTasksPanel({ tasks, sessionId }: BackgroundTasksPanelProps) {
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  if (tasks.length === 0) return null;

  const handleCancel = async (agentId: string) => {
    if (!sessionId) return;
    setCancelling(prev => new Set(prev).add(agentId));
    try {
      const res = await cancelSubagent(sessionId, agentId);
      if (!res.success) console.warn('[SUBAGENT] cancel rejected:', res.message);
      // 成功路径不乐观更新：sub_agent/end + subagent/completed 事件流会驱动面板与卡片
    } catch (err) {
      console.error('[SUBAGENT] cancel failed:', err);
    } finally {
      setCancelling(prev => {
        const next = new Set(prev);
        next.delete(agentId);
        return next;
      });
    }
  };

  return (
    <div className="border-t border-blue-200 bg-blue-50/50 px-4 py-2">
      <div className="flex items-center gap-2 mb-1">
        <Bot className="w-3.5 h-3.5 text-blue-600" />
        <span className="text-xs font-medium text-blue-800">
          后台任务 ({tasks.length})
        </span>
        <span className="text-xs text-blue-500">完成后自动通知并注入会话</span>
      </div>
      <div className="space-y-1">
        {tasks.map(task => (
          <div key={task.key} className="flex items-center gap-2 text-xs text-gray-700">
            <Loader2 className="w-3 h-3 text-blue-500 animate-spin shrink-0" />
            <span className="font-medium shrink-0">{task.agentType}</span>
            <span className="truncate flex-1">{task.description}</span>
            <button
              onClick={() => handleCancel(task.agentId)}
              disabled={cancelling.has(task.agentId)}
              className="flex items-center gap-0.5 text-xs px-1.5 py-0.5 text-red-600 hover:bg-red-100 rounded transition-colors shrink-0 disabled:opacity-50"
              title="硬中止：立即停止该子代理（会话已落盘，之后仍可续跑）"
            >
              <XCircle className="w-3 h-3" />
              <span>{cancelling.has(task.agentId) ? '中止中' : '中止'}</span>
            </button>
          </div>
        ))}
      </div>
    </div>
  );
});

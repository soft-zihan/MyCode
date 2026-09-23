import { memo } from 'react';
import { Bot, Loader2 } from 'lucide-react';
import type { SubAgentNode } from './nodes/types';

interface BackgroundTasksPanelProps {
  tasks: SubAgentNode[];
}

/** U3a：后台任务面板——纯从事件派生（sub_agent/start + 后台标记 + 未见 sub_agent/end），
 *  零专用 API（v2 background.ts:21-33 模式）。完成后任务自动消失，结果以完成卡片出现在会话流中。 */
export const BackgroundTasksPanel = memo(function BackgroundTasksPanel({ tasks }: BackgroundTasksPanelProps) {
  if (tasks.length === 0) return null;
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
            <span className="truncate">{task.description}</span>
          </div>
        ))}
      </div>
    </div>
  );
});

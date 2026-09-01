import React from 'react';
import { FileText, GitBranch, Shield, ShieldCheck } from 'lucide-react';

interface ChatControlsProps {
  sessionId: string | null;
  isStreaming: boolean;
  onTogglePlanMode: () => void;
  onFork: () => void;
  onToggleYolo: () => void;
  planMode?: boolean;
  yoloMode?: boolean;
}

export const ChatControls: React.FC<ChatControlsProps> = ({
  sessionId,
  onTogglePlanMode,
  onFork,
  onToggleYolo,
  planMode = false,
  yoloMode = false,
}) => {
  if (!sessionId) return null;

  const buttonClass = (active: boolean) =>
    `flex items-center gap-1.5 px-2 py-1 text-xs font-medium rounded transition-colors ${
      active
        ? 'text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800'
        : 'text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700'
    }`;

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={onTogglePlanMode}
        className={buttonClass(planMode)}
        title="切换计划模式"
      >
        <FileText className="w-3 h-3" />
        {planMode ? 'Plan' : 'Normal'}
      </button>
      
      <button
        onClick={onFork}
        className="flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
        title="从当前会话创建分支"
      >
        <GitBranch className="w-3 h-3" />
        Fork
      </button>
      
      <button
        onClick={onToggleYolo}
        className={`flex items-center gap-1.5 px-2 py-1 text-xs font-medium rounded transition-colors ${
          yoloMode
            ? 'text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800'
            : 'text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700'
        }`}
        title={yoloMode ? 'YOLO模式：自动允许所有操作' : '默认模式：需要确认'}
      >
        {yoloMode ? <ShieldCheck className="w-3 h-3" /> : <Shield className="w-3 h-3" />}
        {yoloMode ? 'YOLO' : 'Default'}
      </button>
    </div>
  );
};

export default ChatControls;

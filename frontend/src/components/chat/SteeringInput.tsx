import React, { useState } from 'react';
import { Send } from 'lucide-react';

interface SteeringInputProps {
  sessionId: string | null;
  isStreaming: boolean;
  onSteer: (message: string) => void;
  onFollowUp: (message: string) => void;
}

export const SteeringInput: React.FC<SteeringInputProps> = ({
  sessionId,
  isStreaming,
  onSteer,
  onFollowUp,
}) => {
  const [message, setMessage] = useState('');
  const [mode, setMode] = useState<'steer' | 'follow-up'>('steer');

  if (!sessionId || !isStreaming) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!message.trim()) return;

    if (mode === 'steer') {
      onSteer(message);
    } else {
      onFollowUp(message);
    }
    setMessage('');
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <div className="border-t border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20 px-4 py-2">
      <form onSubmit={handleSubmit} className="flex items-start gap-2">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs font-medium text-blue-700 dark:text-blue-300">
              {mode === 'steer' ? '🎯 中途注入' : '📝 后续指令'}
            </span>
            <div className="flex gap-1">
              <button
                type="button"
                onClick={() => setMode('steer')}
                className={`px-2 py-0.5 text-xs rounded transition-colors ${
                  mode === 'steer'
                    ? 'bg-blue-600 text-white'
                    : 'bg-white dark:bg-gray-800 text-blue-600 dark:text-blue-400 hover:bg-blue-100 dark:hover:bg-blue-900/40'
                }`}
              >
                立即生效
              </button>
              <button
                type="button"
                onClick={() => setMode('follow-up')}
                className={`px-2 py-0.5 text-xs rounded transition-colors ${
                  mode === 'follow-up'
                    ? 'bg-blue-600 text-white'
                    : 'bg-white dark:bg-gray-800 text-blue-600 dark:text-blue-400 hover:bg-blue-100 dark:hover:bg-blue-900/40'
                }`}
              >
                结束后执行
              </button>
            </div>
          </div>
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              mode === 'steer'
                ? '输入指令，将在下次工具调用后生效...'
                : '输入指令，将在当前任务完成后执行...'
            }
            className="w-full resize-none border border-blue-300 dark:border-blue-700 rounded px-2 py-1 text-sm bg-white dark:bg-gray-900 focus:outline-none focus:ring-1 focus:ring-blue-500"
            rows={2}
          />
        </div>
        <button
          type="submit"
          disabled={!message.trim()}
          className="p-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          title="发送"
        >
          <Send className="w-4 h-4" />
        </button>
      </form>
      <div className="mt-1 text-xs text-blue-600 dark:text-blue-400">
        {mode === 'steer'
          ? '💡 提示：中途注入会在当前工具调用完成后，下一次 LLM 调用前生效'
          : '💡 提示：后续指令会在当前任务完成后，作为新的用户消息追加'}
      </div>
    </div>
  );
};

export default SteeringInput;

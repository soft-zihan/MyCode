import { useState } from 'react';
import { ThumbsDown, X } from 'lucide-react';

interface ThumbsDownDialogProps {
  sessionId: string;
  turnNumber: number;
  stepNumber?: number;
  toolName?: string;
  hasToolCalls: boolean;
  onSubmit: (feedback: ThumbsDownFeedback) => void;
  onClose: () => void;
}

export interface ThumbsDownFeedback {
  reason: 'wrong_tool' | 'incomplete' | 'memory_not_used' | 'other';
  expectedTool?: string;
  comment?: string;
}

const REASON_OPTIONS = [
  { value: 'wrong_tool', label: '回答错误（记忆/skill 未使用）' },
  { value: 'incomplete', label: '任务提前结束' },
  { value: 'memory_not_used', label: '记忆/skill 未被使用' },
  { value: 'other', label: '其他' },
] as const;

export function ThumbsDownDialog({
  hasToolCalls,
  onSubmit,
  onClose,
}: Omit<ThumbsDownDialogProps, 'sessionId' | 'turnNumber' | 'stepNumber' | 'toolName'>) {
  const [reason, setReason] = useState<ThumbsDownFeedback['reason']>('wrong_tool');
  const [expectedTool, setExpectedTool] = useState('');
  const [comment, setComment] = useState('');

  const handleSubmit = () => {
    onSubmit({
      reason,
      expectedTool: expectedTool || undefined,
      comment: comment || undefined,
    });
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-md p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            反馈问题
          </h3>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
          >
            <X className="w-5 h-5 text-gray-500" />
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              请选择原因：
            </label>
            <div className="space-y-2">
              {REASON_OPTIONS.map((option) => (
                <label
                  key={option.value}
                  className="flex items-center gap-2 cursor-pointer"
                >
                  <input
                    type="radio"
                    name="reason"
                    value={option.value}
                    checked={reason === option.value}
                    onChange={() => setReason(option.value)}
                    className="w-4 h-4 text-blue-600"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    {option.label}
                  </span>
                </label>
              ))}
            </div>
          </div>

          {hasToolCalls && (
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                期望调用的工具（可选）：
              </label>
              <input
                type="text"
                value={expectedTool}
                onChange={(e) => setExpectedTool(e.target.value)}
                placeholder="例如：read_file"
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              备注（可选）：
            </label>
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="请描述具体问题..."
              rows={3}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 mt-6">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            className="px-4 py-2 text-sm font-medium text-white bg-red-600 rounded-md hover:bg-red-700"
          >
            提交
          </button>
        </div>
      </div>
    </div>
  );
}

interface ThumbsDownButtonProps {
  sessionId: string;
  turnNumber: number;
  stepNumber?: number;
  toolName?: string;
  hasToolCalls: boolean;
  onSubmit: (feedback: ThumbsDownFeedback) => void;
}

export function ThumbsDownButton({
  hasToolCalls,
  onSubmit,
}: Omit<ThumbsDownButtonProps, 'sessionId' | 'turnNumber' | 'stepNumber' | 'toolName'>) {
  const [showDialog, setShowDialog] = useState(false);

  return (
    <>
      <button
        onClick={() => setShowDialog(true)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-gray-200 hover:text-red-600 transition-colors"
        title="反馈问题"
      >
        <ThumbsDown className="w-3 h-3" />
      </button>
      
      {showDialog && (
        <ThumbsDownDialog
          hasToolCalls={hasToolCalls}
          onSubmit={onSubmit}
          onClose={() => setShowDialog(false)}
        />
      )}
    </>
  );
}

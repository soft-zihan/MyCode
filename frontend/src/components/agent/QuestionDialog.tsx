import React, { useState } from 'react';
import { HelpCircle, Loader2, Send } from 'lucide-react';
import { Markdown } from '../chat/markdown';

export interface QuestionRequest {
  request_id: string;
  question: string;
  options?: string[];
  context?: string;
}

interface QuestionDialogProps {
  request: QuestionRequest;
  onRespond: (requestId: string, answer: string) => void;
}

/**
 * Agent 提问面板（ask_user / grill 阶段答题）。
 *
 * - 问题支持 markdown 渲染
 * - 选项点击即答；也可输入自定义回答（两者可组合：点选项后可编辑补充）
 */
export const QuestionDialog: React.FC<QuestionDialogProps> = ({ request, onRespond }) => {
  const [answer, setAnswer] = useState('');
  const [responding, setResponding] = useState(false);

  const handleSubmit = (response: string) => {
    if (!response.trim()) return;
    setResponding(true);
    onRespond(request.request_id, response.trim());
  };

  const hasOptions = request.options && request.options.length > 0;

  return (
    <div className="mx-4 mb-2 animate-fade-in">
      <div className="rounded-xl border border-indigo-200 border-l-4 border-l-indigo-500 bg-white shadow-sm overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-2 bg-indigo-50/60 border-b border-indigo-100">
          <span className="w-2 h-2 rounded-full bg-indigo-500 animate-pulse" />
          <HelpCircle className="w-3.5 h-3.5 text-indigo-600" />
          <span className="text-xs font-semibold text-indigo-700">Agent 提问</span>
        </div>

        <div className="px-4 py-3">
          <div className="text-sm text-gray-900">
            <Markdown content={request.question} linkifyFiles={false} compact />
          </div>
          {request.context && (
            <p className="mt-2 text-xs text-gray-500 leading-relaxed">{request.context}</p>
          )}

          {hasOptions && (
            <div className="mt-3 flex flex-wrap gap-2">
              {request.options!.map((option, idx) => (
                <button
                  key={idx}
                  onClick={() => handleSubmit(option)}
                  disabled={responding}
                  className="btn-secondary btn-sm rounded-full"
                >
                  {option}
                </button>
              ))}
            </div>
          )}

          <div className="mt-3 flex gap-2">
            <input
              type="text"
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && answer.trim()) {
                  handleSubmit(answer);
                }
              }}
              placeholder={hasOptions ? '或输入自定义回答...' : '输入你的回答...'}
              disabled={responding}
              className="input-base flex-1"
              autoFocus
            />
            <button
              onClick={() => handleSubmit(answer)}
              disabled={responding || !answer.trim()}
              className="btn-primary px-3"
            >
              {responding ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default QuestionDialog;

import React, { useState } from 'react';
import { HelpCircle, Loader2 } from 'lucide-react';

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

export const QuestionDialog: React.FC<QuestionDialogProps> = ({ request, onRespond }) => {
  const [answer, setAnswer] = useState('');
  const [responding, setResponding] = useState(false);

  const handleSubmit = (response: string) => {
    setResponding(true);
    onRespond(request.request_id, response);
  };

  const hasOptions = request.options && request.options.length > 0;

  return (
    <div className="mx-4 mb-2">
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 border-l-4 border-l-blue-400 bg-white dark:bg-gray-900 shadow-sm overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-1.5 bg-blue-50 dark:bg-blue-900/20 border-b border-blue-100 dark:border-blue-800/30">
          <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse" />
          <HelpCircle className="w-3.5 h-3.5 text-blue-600 dark:text-blue-400" />
          <span className="text-xs font-medium text-blue-700 dark:text-blue-300">
            Agent 提问
          </span>
        </div>

        <div className="px-3 py-2.5">
          <p className="text-sm text-gray-900 dark:text-gray-100 font-medium">
            {request.question}
          </p>
          {request.context && (
            <p className="mt-1.5 text-xs text-gray-500 dark:text-gray-400">
              {request.context}
            </p>
          )}

          {hasOptions ? (
            <div className="mt-3 flex flex-wrap gap-2">
              {request.options!.map((option, idx) => (
                <button
                  key={idx}
                  onClick={() => handleSubmit(option)}
                  disabled={responding}
                  className="px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  {option}
                </button>
              ))}
            </div>
          ) : (
            <div className="mt-3 flex gap-2">
              <input
                type="text"
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && answer.trim()) {
                    handleSubmit(answer.trim());
                  }
                }}
                placeholder="输入你的回答..."
                disabled={responding}
                className="flex-1 px-3 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-md text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:opacity-50"
              />
              <button
                onClick={() => answer.trim() && handleSubmit(answer.trim())}
                disabled={responding || !answer.trim()}
                className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 border border-transparent rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-1.5"
              >
                {responding && <Loader2 className="w-3 h-3 animate-spin" />}
                提交
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default QuestionDialog;

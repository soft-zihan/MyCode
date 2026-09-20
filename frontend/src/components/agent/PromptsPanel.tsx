import { useState, useEffect } from 'react';
import { FileText } from 'lucide-react';

interface PromptSummary {
  name: string;
  category: string;
  description: string;
  source: string;
  editable: boolean;
  content_length: number;
}

const categoryLabels: Record<string, string> = {
  main: '主提示词',
  subagent: '子 Agent',
  hidden: '隐藏 Agent',
  side_query: 'Side Query',
};

const categoryColors: Record<string, string> = {
  main: 'bg-purple-100 text-purple-700',
  subagent: 'bg-blue-100 text-blue-700',
  hidden: 'bg-gray-100 text-gray-700',
  side_query: 'bg-orange-100 text-orange-700',
};

interface PromptsPanelProps {
  onFileSelect?: (path: string) => void;
}

export function PromptsPanel({ onFileSelect }: PromptsPanelProps) {
  const [prompts, setPrompts] = useState<PromptSummary[]>([]);

  useEffect(() => {
    fetch('/api/prompts')
      .then(r => r.json())
      .then(setPrompts)
      .catch(console.error);
  }, []);

  const handleClick = (prompt: PromptSummary) => {
    if (onFileSelect && prompt.source && prompt.source !== 'builtin') {
      onFileSelect(prompt.source);
    }
  };

  // Group prompts by category
  const grouped = prompts.reduce((acc, p) => {
    if (!acc[p.category]) acc[p.category] = [];
    acc[p.category].push(p);
    return acc;
  }, {} as Record<string, PromptSummary[]>);

  return (
    <div className="divide-y divide-gray-100">
      {Object.entries(grouped).map(([category, items]) => (
        <div key={category}>
          <div className="px-3 py-1 flex items-center gap-1.5 bg-gray-50/50">
            <span className={`text-[9px] px-1.5 py-0.5 rounded ${categoryColors[category] || 'bg-gray-100 text-gray-600'}`}>
              {categoryLabels[category] || category}
            </span>
            <span className="text-[10px] text-gray-400">{items.length}</span>
          </div>
          {items.map(prompt => (
            <button
              key={prompt.name}
              onClick={() => handleClick(prompt)}
              className="w-full px-3 py-1.5 flex items-center gap-2 hover:bg-gray-50 text-left border-b border-gray-50 last:border-0"
              title={prompt.description}
            >
              <FileText className="w-3 h-3 text-gray-400" />
              <span className="text-xs text-gray-700 flex-1 truncate">{prompt.name}</span>
              <span className="text-[10px] text-gray-400">{prompt.content_length} chars</span>
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

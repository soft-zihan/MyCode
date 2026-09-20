import { useState, useEffect } from 'react';
import { FileText, ChevronDown, ChevronRight } from 'lucide-react';

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
  side_query: 'Side Query',
};

const categoryColors: Record<string, string> = {
  main: 'bg-purple-100 text-purple-700',
  subagent: 'bg-blue-100 text-blue-700',
  side_query: 'bg-orange-100 text-orange-700',
};

interface PromptsPanelProps {
  onFileSelect?: (path: string) => void;
}

export function PromptsPanel({ onFileSelect }: PromptsPanelProps) {
  const [prompts, setPrompts] = useState<PromptSummary[]>([]);
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set(['main', 'subagent']));

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

  const toggleCategory = (category: string) => {
    const newExpanded = new Set(expandedCategories);
    if (newExpanded.has(category)) {
      newExpanded.delete(category);
    } else {
      newExpanded.add(category);
    }
    setExpandedCategories(newExpanded);
  };

  // Group prompts by category
  const grouped = prompts.reduce((acc, p) => {
    if (!acc[p.category]) acc[p.category] = [];
    acc[p.category].push(p);
    return acc;
  }, {} as Record<string, PromptSummary[]>);

  // 定义显示顺序
  const categoryOrder = ['main', 'subagent', 'side_query'];

  return (
    <div className="divide-y divide-gray-100">
      {categoryOrder.map(category => {
        const items = grouped[category];
        if (!items || items.length === 0) return null;
        
        const isExpanded = expandedCategories.has(category);
        
        return (
          <div key={category}>
            <button
              onClick={() => toggleCategory(category)}
              className="w-full px-3 py-1 flex items-center gap-1.5 bg-gray-50/50 hover:bg-gray-100/50 text-left"
            >
              {isExpanded ? (
                <ChevronDown className="w-3 h-3 text-gray-400" />
              ) : (
                <ChevronRight className="w-3 h-3 text-gray-400" />
              )}
              <span className={`text-[9px] px-1.5 py-0.5 rounded ${categoryColors[category] || 'bg-gray-100 text-gray-600'}`}>
                {categoryLabels[category] || category}
              </span>
              <span className="text-[10px] text-gray-400">{items.length}</span>
            </button>
            {isExpanded && items.map(prompt => (
              <button
                key={prompt.name}
                onClick={() => handleClick(prompt)}
                className="w-full px-3 py-1.5 pl-7 flex items-center gap-2 hover:bg-gray-50 text-left border-b border-gray-50 last:border-0"
                title={prompt.description}
              >
                <FileText className="w-3 h-3 text-gray-400" />
                <span className="text-xs text-gray-700 flex-1 truncate">{prompt.name}</span>
                <span className="text-[10px] text-gray-400">{prompt.content_length} chars</span>
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}

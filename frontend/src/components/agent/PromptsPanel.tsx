import { useState, useEffect } from 'react';
import { ChevronDown, ChevronRight, Edit2, Save, X, FileText } from 'lucide-react';

interface PromptSummary {
  name: string;
  category: string;
  description: string;
  source: string;
  editable: boolean;
  content_length: number;
}

interface PromptDetail {
  name: string;
  category: string;
  description: string;
  source: string;
  editable: boolean;
  content: string;
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

export function PromptsPanel() {
  const [prompts, setPrompts] = useState<PromptSummary[]>([]);
  const [expandedPrompt, setExpandedPrompt] = useState<string | null>(null);
  const [promptDetail, setPromptDetail] = useState<PromptDetail | null>(null);
  const [editContent, setEditContent] = useState('');
  const [isEditing, setIsEditing] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch('/api/prompts')
      .then(r => r.json())
      .then(setPrompts)
      .catch(console.error);
  }, []);

  const handleExpand = async (name: string) => {
    if (expandedPrompt === name) {
      setExpandedPrompt(null);
      setPromptDetail(null);
      setIsEditing(false);
      return;
    }
    setExpandedPrompt(name);
    setLoading(true);
    try {
      const res = await fetch(`/api/prompts/${encodeURIComponent(name)}`);
      const detail = await res.json();
      setPromptDetail(detail);
      setEditContent(detail.content);
      setIsEditing(false);
    } catch (err) {
      console.error('Failed to load prompt:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    if (!promptDetail) return;
    try {
      const res = await fetch(`/api/prompts/${encodeURIComponent(promptDetail.name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editContent }),
      });
      if (res.ok) {
        setPromptDetail({ ...promptDetail, content: editContent });
        setIsEditing(false);
      }
    } catch (err) {
      console.error('Failed to save prompt:', err);
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
            <div key={prompt.name} className="border-b border-gray-50 last:border-0">
              <button
                onClick={() => handleExpand(prompt.name)}
                className="w-full px-3 py-1.5 flex items-center gap-2 hover:bg-gray-50 text-left"
              >
                {expandedPrompt === prompt.name ? (
                  <ChevronDown className="w-3 h-3 text-gray-400" />
                ) : (
                  <ChevronRight className="w-3 h-3 text-gray-400" />
                )}
                <FileText className="w-3 h-3 text-gray-400" />
                <span className="text-xs text-gray-700 flex-1 truncate">{prompt.name}</span>
                <span className="text-[10px] text-gray-400">{prompt.content_length} chars</span>
              </button>
              {expandedPrompt === prompt.name && (
                <div className="px-3 pb-2">
                  <p className="text-[10px] text-gray-500 mb-1">{prompt.description}</p>
                  {loading ? (
                    <div className="text-xs text-gray-400 py-2">Loading...</div>
                  ) : promptDetail ? (
                    <>
                      {isEditing ? (
                        <>
                          <textarea
                            value={editContent}
                            onChange={e => setEditContent(e.target.value)}
                            className="w-full h-40 px-2 py-1 text-[10px] font-mono border border-gray-200 rounded resize-y bg-white"
                          />
                          <div className="flex gap-1 mt-1">
                            <button
                              onClick={handleSave}
                              className="flex items-center gap-1 px-2 py-0.5 text-[10px] bg-green-500 text-white rounded hover:bg-green-600"
                            >
                              <Save className="w-2.5 h-2.5" />
                              Save
                            </button>
                            <button
                              onClick={() => { setIsEditing(false); setEditContent(promptDetail.content); }}
                              className="flex items-center gap-1 px-2 py-0.5 text-[10px] bg-gray-500 text-white rounded hover:bg-gray-600"
                            >
                              <X className="w-2.5 h-2.5" />
                              Cancel
                            </button>
                          </div>
                        </>
                      ) : (
                        <>
                          <pre className="text-[10px] font-mono text-gray-600 bg-gray-50 p-2 rounded max-h-32 overflow-auto whitespace-pre-wrap">
                            {promptDetail.content.slice(0, 500)}
                            {promptDetail.content.length > 500 && '...'}
                          </pre>
                          {promptDetail.editable && (
                            <button
                              onClick={() => setIsEditing(true)}
                              className="flex items-center gap-1 mt-1 px-2 py-0.5 text-[10px] text-blue-600 hover:bg-blue-50 rounded"
                            >
                              <Edit2 className="w-2.5 h-2.5" />
                              Edit
                            </button>
                          )}
                        </>
                      )}
                    </>
                  ) : null}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

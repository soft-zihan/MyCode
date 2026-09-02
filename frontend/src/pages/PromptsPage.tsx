import { useState, useEffect } from 'react';
import { fetchPrompts, fetchPrompt, savePrompt, PromptInfo } from '../api/client';
import { FileText, Save, ArrowLeft } from 'lucide-react';

export function PromptsPage() {
  const [prompts, setPrompts] = useState<PromptInfo[]>([]);
  const [selectedPrompt, setSelectedPrompt] = useState<PromptInfo | null>(null);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  useEffect(() => {
    fetchPrompts()
      .then(setPrompts)
      .catch(err => console.error('Failed to load prompts:', err))
      .finally(() => setLoading(false));
  }, []);

  const handleSelectPrompt = async (prompt: PromptInfo) => {
    setSelectedPrompt(prompt);
    setMessage(null);
    try {
      const data = await fetchPrompt(prompt.path);
      setContent(data.content);
    } catch (err) {
      console.error('Failed to load prompt:', err);
      setMessage({ type: 'error', text: 'Failed to load prompt content' });
    }
  };

  const handleSave = async () => {
    if (!selectedPrompt) return;
    setSaving(true);
    setMessage(null);
    try {
      await savePrompt(selectedPrompt.path, content);
      setMessage({ type: 'success', text: 'Prompt saved successfully' });
    } catch (err) {
      console.error('Failed to save prompt:', err);
      setMessage({ type: 'error', text: 'Failed to save prompt' });
    } finally {
      setSaving(false);
    }
  };

  const handleBack = () => {
    setSelectedPrompt(null);
    setContent('');
    setMessage(null);
  };

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-gray-500">Loading prompts...</div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200 bg-white flex items-center justify-between">
        <div className="flex items-center gap-2">
          {selectedPrompt && (
            <button
              onClick={handleBack}
              className="p-1.5 rounded hover:bg-gray-100 transition-colors"
              title="Back to list"
            >
              <ArrowLeft className="w-4 h-4" />
            </button>
          )}
          <FileText className="w-5 h-5 text-blue-500" />
          <h1 className="text-lg font-semibold text-gray-900">
            {selectedPrompt ? selectedPrompt.name : 'System Prompts'}
          </h1>
        </div>
        {selectedPrompt && (
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-500 text-white text-sm font-medium rounded hover:bg-blue-600 disabled:opacity-50 transition-colors"
          >
            <Save className="w-4 h-4" />
            {saving ? 'Saving...' : 'Save'}
          </button>
        )}
      </div>

      {/* Message */}
      {message && (
        <div className={`px-4 py-2 text-sm ${
          message.type === 'success' ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
        }`}>
          {message.text}
        </div>
      )}

      {/* Content */}
      {selectedPrompt ? (
        <div className="flex-1 flex flex-col overflow-hidden">
          <div className="px-4 py-2 bg-gray-50 border-b border-gray-200 text-sm text-gray-600">
            {selectedPrompt.description}
          </div>
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            className="flex-1 p-4 font-mono text-sm resize-none focus:outline-none bg-white"
            placeholder="Enter prompt content..."
          />
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-4">
          <div className="max-w-2xl mx-auto space-y-2">
            {prompts.map((prompt) => (
              <button
                key={prompt.id}
                onClick={() => handleSelectPrompt(prompt)}
                className="w-full text-left p-4 rounded-lg border border-gray-200 hover:border-blue-300 hover:bg-blue-50 transition-colors"
              >
                <div className="font-medium text-gray-900">{prompt.name}</div>
                <div className="text-sm text-gray-500 mt-1">{prompt.description}</div>
                <div className="text-xs text-gray-400 mt-2 font-mono">{prompt.path}</div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

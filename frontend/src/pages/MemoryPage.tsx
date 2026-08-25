import { useState, useEffect } from 'react';
import { fetchMemories, deleteMemory, Memory } from '../api/client';
import { Trash2, RefreshCw, Database, Edit3, Save, X, Info } from 'lucide-react';

export default function MemoryPage() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [showInfo, setShowInfo] = useState(false);

  const loadMemories = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMemories();
      setMemories(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load memories');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadMemories();
  }, []);

  const handleDelete = async (filename: string) => {
    if (!confirm('Are you sure you want to delete this memory?')) return;
    try {
      await deleteMemory(filename);
      setMemories(memories.filter(m => m.filename !== filename));
      if (selectedMemory?.filename === filename) {
        setSelectedMemory(null);
      }
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete memory');
    }
  };

  const handleEdit = () => {
    if (!selectedMemory) return;
    setEditName(selectedMemory.name);
    setEditDescription(selectedMemory.description);
    setEditContent(selectedMemory.content);
    setIsEditing(true);
  };

  const handleSave = async () => {
    if (!selectedMemory) return;
    setSaving(true);
    try {
      const response = await fetch(`/api/memories/${selectedMemory.filename}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: editName,
          description: editDescription,
          content: editContent,
        }),
      });
      if (response.ok) {
        const result = await response.json();
        setIsEditing(false);
        await loadMemories();
        // Reload the selected memory
        const updated = memories.find(m => m.filename === result.filename);
        if (updated) setSelectedMemory(updated);
      }
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to save memory');
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setIsEditing(false);
  };

  const getTypeColor = (type: string) => {
    const colors: Record<string, string> = {
      user: 'bg-blue-100 text-blue-800',
      feedback: 'bg-green-100 text-green-800',
      project: 'bg-purple-100 text-purple-800',
      reference: 'bg-yellow-100 text-yellow-800',
    };
    return colors[type] || 'bg-gray-100 text-gray-800';
  };

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-gray-500">Loading memories...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-red-500">{error}</div>
      </div>
    );
  }

  return (
    <div className="h-full flex bg-white">
      {/* Memory List */}
      <div className="w-96 border-r border-gray-200 flex flex-col">
        <div className="p-6 border-b border-gray-200">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold text-gray-900">Memory</h1>
              <p className="text-sm text-gray-500 mt-1">
                {memories.length} memor{memories.length !== 1 ? 'ies' : 'y'}
              </p>
            </div>
            <button
              onClick={loadMemories}
              className="flex items-center px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors"
            >
              <RefreshCw className="w-4 h-4 mr-2" />
              Refresh
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {memories.length === 0 ? (
            <div className="p-8 text-center text-gray-500">
              No memories found
            </div>
          ) : (
            <div className="divide-y divide-gray-200">
              {memories.map(memory => (
                <div
                  key={memory.filename}
                  className={`p-4 cursor-pointer hover:bg-gray-50 ${
                    selectedMemory?.filename === memory.filename ? 'bg-blue-50' : ''
                  }`}
                  onClick={() => setSelectedMemory(memory)}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className={`px-2 py-0.5 text-xs font-medium rounded ${getTypeColor(memory.type)}`}>
                          {memory.type}
                        </span>
                        <h3 className="text-sm font-medium text-gray-900 truncate">
                          {memory.name}
                        </h3>
                      </div>
                      <p className="text-xs text-gray-500 truncate">
                        {memory.description}
                      </p>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDelete(memory.filename);
                      }}
                      className="ml-2 text-red-600 hover:text-red-900"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Memory Detail */}
      <div className="flex-1 flex flex-col">
        {selectedMemory ? (
          <>
            <div className="p-6 border-b border-gray-200">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <Database className="w-5 h-5 text-gray-400" />
                  {isEditing ? (
                    <input
                      type="text"
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      className="text-xl font-bold text-gray-900 border border-gray-300 rounded px-2 py-1"
                    />
                  ) : (
                    <h2 className="text-xl font-bold text-gray-900">{selectedMemory.name}</h2>
                  )}
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => setShowInfo(!showInfo)}
                    className="flex items-center px-3 py-1.5 text-sm bg-gray-500 text-white rounded hover:bg-gray-600"
                  >
                    <Info className="w-4 h-4 mr-1" />
                    About
                  </button>
                  {isEditing ? (
                    <>
                      <button
                        onClick={handleSave}
                        disabled={saving}
                        className="flex items-center px-3 py-1.5 text-sm bg-green-500 text-white rounded hover:bg-green-600 disabled:opacity-50"
                      >
                        <Save className="w-4 h-4 mr-1" />
                        {saving ? 'Saving...' : 'Save'}
                      </button>
                      <button
                        onClick={handleCancel}
                        className="flex items-center px-3 py-1.5 text-sm bg-gray-500 text-white rounded hover:bg-gray-600"
                      >
                        <X className="w-4 h-4 mr-1" />
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={handleEdit}
                      className="flex items-center px-3 py-1.5 text-sm bg-blue-500 text-white rounded hover:bg-blue-600"
                    >
                      <Edit3 className="w-4 h-4 mr-1" />
                      Edit
                    </button>
                  )}
                </div>
              </div>
              {isEditing ? (
                <input
                  type="text"
                  value={editDescription}
                  onChange={(e) => setEditDescription(e.target.value)}
                  className="text-sm text-gray-600 border border-gray-300 rounded px-2 py-1 w-full mb-2"
                  placeholder="Description"
                />
              ) : (
                <p className="text-sm text-gray-600">{selectedMemory.description}</p>
              )}
              <div className="mt-2 flex items-center gap-2">
                <span className={`px-2 py-0.5 text-xs font-medium rounded ${getTypeColor(selectedMemory.type)}`}>
                  {selectedMemory.type}
                </span>
                <span className="text-xs text-gray-500">{selectedMemory.filename}</span>
              </div>
            </div>
            {showInfo && (
              <div className="p-4 bg-blue-50 border-b border-blue-200">
                <h3 className="text-sm font-semibold text-blue-900 mb-2">Memory System Principles</h3>
                <div className="text-xs text-blue-800 space-y-2">
                  <p><strong>What is Memory?</strong> Memory is a persistent storage system that allows the AI agent to remember important information across conversations.</p>
                  <p><strong>Types:</strong></p>
                  <ul className="list-disc list-inside ml-2 space-y-1">
                    <li><strong>User:</strong> Personal preferences and settings</li>
                    <li><strong>Feedback:</strong> Corrections and improvements from user</li>
                    <li><strong>Project:</strong> Project-specific context and decisions</li>
                    <li><strong>Reference:</strong> Important facts and documentation</li>
                  </ul>
                  <p><strong>Recall:</strong> Memories are automatically retrieved based on semantic similarity to the current conversation context using a side query model.</p>
                  <p><strong>Storage:</strong> Memories are stored as Markdown files in <code className="bg-blue-100 px-1 rounded">~/.bear-code/memories/</code></p>
                </div>
              </div>
            )}
            <div className="flex-1 overflow-y-auto p-6">
              {isEditing ? (
                <textarea
                  value={editContent}
                  onChange={(e) => setEditContent(e.target.value)}
                  className="w-full h-full text-sm text-gray-700 font-mono border border-gray-300 rounded p-3 resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
                  placeholder="Memory content..."
                />
              ) : (
                <pre className="whitespace-pre-wrap text-sm text-gray-700 font-mono">
                  {selectedMemory.content}
                </pre>
              )}
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center text-gray-400">
            <div className="text-center">
              <Database className="w-12 h-12 mx-auto mb-4 opacity-50" />
              <p>Select a memory to view details</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

import { useState, useEffect, useRef, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { 
  FolderTree, ChevronRight, ChevronDown, File, Folder, X, 
  MessageSquare, Trash2, PanelRight, Send, Clock, Plus,
  Edit3, Check, Pencil, Brain, Square
} from 'lucide-react';
import { 
  fetchWorkspaceTree, fetchWorkspaceFile, WorkspaceNode,
  fetchSessions, deleteSession, Session,
  fetchAgents, fetchConfig, Agent, AppConfig,
  fetchDirectories, DirectoryList,
  deleteWorkspaceFile, renameWorkspaceFile,
  generateSessionName, updateSessionName
} from '../api/client';
import Editor from '@monaco-editor/react';

interface FileTreeNodeProps {
  node: WorkspaceNode;
  level: number;
  onFileSelect: (path: string) => void;
  onAddToChat: (path: string) => void;
  selectedFile: string | null;
  editMode?: boolean;
  cwd?: string | null;
  onRefresh?: () => void;
}

function FileTreeNode({ node, level, onFileSelect, onAddToChat, selectedFile, editMode, cwd, onRefresh }: FileTreeNodeProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [isRenaming, setIsRenaming] = useState(false);
  const [newName, setNewName] = useState(node.name);
  const isSelected = selectedFile === node.path;

  const handleClick = () => {
    if (isRenaming) return;
    if (node.type === 'directory') {
      setIsExpanded(!isExpanded);
    } else {
      onFileSelect(node.path);
    }
  };

  const handleAddToChat = (e: React.MouseEvent) => {
    e.stopPropagation();
    onAddToChat(node.path);
  };

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm(`确定要删除 ${node.name} 吗？`)) return;
    
    try {
      await deleteWorkspaceFile(node.path, cwd || undefined);
      onRefresh?.();
    } catch (err) {
      alert(err instanceof Error ? err.message : '删除失败');
    }
  };

  const handleRenameStart = (e: React.MouseEvent) => {
    e.stopPropagation();
    setIsRenaming(true);
    setNewName(node.name);
  };

  const handleRenameConfirm = async (e: React.MouseEvent | React.KeyboardEvent) => {
    e.stopPropagation();
    if (newName === node.name || !newName.trim()) {
      setIsRenaming(false);
      return;
    }
    
    try {
      await renameWorkspaceFile(node.path, newName.trim(), cwd || undefined);
      setIsRenaming(false);
      onRefresh?.();
    } catch (err) {
      alert(err instanceof Error ? err.message : '重命名失败');
    }
  };

  const handleRenameCancel = (e: React.MouseEvent | React.KeyboardEvent) => {
    e.stopPropagation();
    setIsRenaming(false);
    setNewName(node.name);
  };

  return (
    <div>
      <div
        className={`flex items-center px-2 py-1 cursor-pointer hover:bg-gray-100 group ${
          isSelected ? 'bg-blue-50 text-blue-700' : ''
        }`}
        style={{ paddingLeft: `${level * 16 + 8}px` }}
        onClick={handleClick}
      >
        {node.type === 'directory' ? (
          <>
            {isExpanded ? (
              <ChevronDown className="w-4 h-4 mr-1" />
            ) : (
              <ChevronRight className="w-4 h-4 mr-1" />
            )}
            <Folder className="w-4 h-4 mr-2 text-blue-500" />
          </>
        ) : (
          <>
            <span className="w-4 mr-1" />
            <File className="w-4 h-4 mr-2 text-gray-500" />
          </>
        )}
        
        {isRenaming ? (
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleRenameConfirm(e);
              if (e.key === 'Escape') handleRenameCancel(e);
            }}
            onClick={(e) => e.stopPropagation()}
            className="flex-1 px-1 py-0 text-sm border border-blue-400 rounded focus:outline-none"
            autoFocus
          />
        ) : (
          <span className="text-sm truncate flex-1">{node.name}</span>
        )}
        
        {editMode && !isRenaming && (
          <div className="opacity-0 group-hover:opacity-100 flex items-center gap-1 transition-opacity">
            <button
              onClick={handleRenameStart}
              className="p-1 hover:bg-yellow-100 rounded text-yellow-600"
              title="重命名"
            >
              <Pencil className="w-3 h-3" />
            </button>
            <button
              onClick={handleDelete}
              className="p-1 hover:bg-red-100 rounded text-red-500"
              title="删除"
            >
              <Trash2 className="w-3 h-3" />
            </button>
          </div>
        )}
        
        {isRenaming && (
          <div className="flex items-center gap-1 ml-1">
            <button
              onClick={handleRenameConfirm}
              className="p-1 hover:bg-green-100 rounded text-green-600"
              title="确认"
            >
              <Check className="w-3 h-3" />
            </button>
            <button
              onClick={handleRenameCancel}
              className="p-1 hover:bg-gray-200 rounded text-gray-600"
              title="取消"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
        
        {!editMode && !isRenaming && (
          <button
            onClick={handleAddToChat}
            className="opacity-0 group-hover:opacity-100 p-1 hover:bg-blue-100 rounded text-blue-500 transition-opacity"
            title={node.type === 'directory' ? "Add directory structure to context" : "Add to chat context"}
          >
            <Plus className="w-3 h-3" />
          </button>
        )}
      </div>
      {node.type === 'directory' && isExpanded && node.children && (
        <div>
          {node.children.map(child => (
            <FileTreeNode
              key={child.path}
              node={child}
              level={level + 1}
              onFileSelect={onFileSelect}
              onAddToChat={onAddToChat}
              selectedFile={selectedFile}
              editMode={editMode}
              cwd={cwd}
              onRefresh={onRefresh}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface FileTreeProps {
  onFileSelect: (path: string) => void;
  onAddToChat: (path: string) => void;
  selectedFile: string | null;
  cwd?: string | null;
  visible?: boolean;
}

function FileTree({ onFileSelect, onAddToChat, selectedFile, cwd, visible }: FileTreeProps) {
  const [tree, setTree] = useState<WorkspaceNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [editMode, setEditMode] = useState(false);
  const cacheKey = cwd || 'default';

  const loadTree = (forceRefresh = false) => {
    // Check cache first (unless force refresh)
    if (!forceRefresh) {
      const cached = sessionStorage.getItem(`fileTree_${cacheKey}`);
      if (cached) {
        try {
          setTree(JSON.parse(cached));
          setLoading(false);
          return;
        } catch {}
      }
    }

    setLoading(true);
    fetchWorkspaceTree(cwd || undefined)
      .then(data => {
        setTree(data);
        // Cache the result
        sessionStorage.setItem(`fileTree_${cacheKey}`, JSON.stringify(data));
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadTree();
  }, [cwd]);

  // Refresh tree when becoming visible (to catch disk changes)
  useEffect(() => {
    if (visible) {
      loadTree(true);
    }
  }, [visible]);

  if (loading && !tree) {
    return <div className="p-4 text-sm text-gray-500">Loading...</div>;
  }

  if (!tree) {
    return <div className="p-4 text-sm text-red-500">Failed to load</div>;
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="p-3 border-b border-gray-200 bg-gray-50 flex items-center justify-between">
        <div className="flex items-center text-sm font-medium text-gray-700">
          <FolderTree className="w-4 h-4 mr-2" />
          Workspace
        </div>
        <button
          onClick={() => setEditMode(!editMode)}
          className={`p-1 rounded transition-colors ${
            editMode 
              ? 'bg-yellow-100 text-yellow-700 hover:bg-yellow-200' 
              : 'hover:bg-gray-200 text-gray-600'
          }`}
          title={editMode ? '退出编辑模式' : '进入编辑模式'}
        >
          <Edit3 className="w-4 h-4" />
        </button>
      </div>
      <div className="py-2">
        {tree.children?.map(child => (
          <FileTreeNode
            key={child.path}
            node={child}
            level={0}
            onFileSelect={onFileSelect}
            onAddToChat={onAddToChat}
            selectedFile={selectedFile}
            editMode={editMode}
            cwd={cwd}
            onRefresh={loadTree}
          />
        ))}
      </div>
    </div>
  );
}

interface FileViewerProps {
  filePath: string;
  onClose: () => void;
}

function FileViewer({ filePath, onClose }: FileViewerProps) {
  const [content, setContent] = useState<string>('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetchWorkspaceFile(filePath)
      .then(data => setContent(data.content))
      .catch(err => setContent(`Error: ${err.message}`))
      .finally(() => setLoading(false));
  }, [filePath]);

  const getLanguage = (path: string) => {
    const ext = path.split('.').pop()?.toLowerCase();
    const langMap: Record<string, string> = {
      'py': 'python',
      'js': 'javascript',
      'ts': 'typescript',
      'tsx': 'typescript',
      'jsx': 'javascript',
      'json': 'json',
      'md': 'markdown',
      'html': 'html',
      'css': 'css',
      'yaml': 'yaml',
      'yml': 'yaml',
      'sh': 'shell',
      'bash': 'shell',
    };
    return langMap[ext || ''] || 'plaintext';
  };

  return (
    <div className="h-full flex flex-col bg-white">
      <div className="flex items-center justify-between px-4 py-2 border-b border-gray-200 bg-gray-50">
        <div className="text-sm font-medium text-gray-700">{filePath}</div>
        <button
          onClick={onClose}
          className="p-1 hover:bg-gray-200 rounded"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="flex-1 overflow-hidden">
        {loading ? (
          <div className="p-4 text-sm text-gray-500">Loading...</div>
        ) : (
          <Editor
            height="100%"
            language={getLanguage(filePath)}
            value={content}
            theme="vs-light"
            options={{
              readOnly: true,
              minimap: { enabled: false },
              fontSize: 13,
              lineNumbers: 'on',
              scrollBeyondLastLine: false,
            }}
          />
        )}
      </div>
    </div>
  );
}

// ── Sessions Panel ───────────────────────────────────────────────────────────

function formatRelativeTime(timeStr: string): string {
  const now = new Date();
  const date = new Date(timeStr);
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString('zh-CN');
}

interface SessionsPanelProps {
  onSessionSelect: (sessionId: string) => void;
  onNewSession: (cwd?: string) => void;
  currentSessionId: string | null;
}

// ── Directory Picker Modal ───────────────────────────────────────────────────

interface DirectoryPickerProps {
  onSelect: (path: string) => void;
  onCancel: () => void;
}

function DirectoryPicker({ onSelect, onCancel }: DirectoryPickerProps) {
  const [dirList, setDirList] = useState<DirectoryList | null>(null);
  const [loading, setLoading] = useState(true);
  const [currentPath, setCurrentPath] = useState<string>('');

  const loadDir = async (path?: string) => {
    setLoading(true);
    try {
      const data = await fetchDirectories(path);
      setDirList(data);
      setCurrentPath(data.current);
    } catch (err) {
      console.error('Failed to load directory:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadDir();
  }, []);

  const handleGoUp = () => {
    if (dirList?.parent) {
      loadDir(dirList.parent);
    }
  };

  const handleSelectCurrent = () => {
    onSelect(currentPath);
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl w-[500px] max-h-[500px] flex flex-col">
        <div className="px-4 py-3 border-b border-gray-200 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-900">Select Project Directory</h3>
          <button onClick={onCancel} className="p-1 hover:bg-gray-100 rounded">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="px-4 py-2 border-b border-gray-100 bg-gray-50">
          <div className="flex items-center gap-2">
            <button
              onClick={handleGoUp}
              disabled={!dirList?.parent}
              className="p-1 hover:bg-gray-200 rounded disabled:opacity-30"
              title="Go up"
            >
              <ChevronRight className="w-4 h-4 rotate-180" />
            </button>
            <span className="text-xs text-gray-600 truncate flex-1" title={currentPath}>
              {currentPath}
            </span>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {loading ? (
            <div className="p-4 text-center text-sm text-gray-500">Loading...</div>
          ) : dirList?.directories.length === 0 ? (
            <div className="p-4 text-center text-sm text-gray-500">No subdirectories</div>
          ) : (
            <div className="space-y-0.5">
              {dirList?.directories.map(dir => (
                <div
                  key={dir.path}
                  onClick={() => loadDir(dir.path)}
                  className="flex items-center px-3 py-2 rounded hover:bg-blue-50 cursor-pointer group"
                >
                  <Folder className="w-4 h-4 mr-2 text-blue-500" />
                  <span className="text-sm text-gray-700 flex-1">{dir.name}</span>
                  <ChevronRight className="w-4 h-4 text-gray-400 opacity-0 group-hover:opacity-100" />
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="px-4 py-3 border-t border-gray-200 flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-100 rounded"
          >
            Cancel
          </button>
          <button
            onClick={handleSelectCurrent}
            className="px-3 py-1.5 text-xs bg-green-500 text-white rounded hover:bg-green-600"
          >
            Select This Directory
          </button>
        </div>
      </div>
    </div>
  );
}

interface SessionsPanelProps {
  onSessionSelect: (id: string) => void;
  onNewSession: (cwd: string) => void;
  currentSessionId: string | null;
  visible?: boolean;
}

function SessionsPanel({ onSessionSelect, onNewSession, currentSessionId, visible }: SessionsPanelProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [showDirPicker, setShowDirPicker] = useState(false);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState<string>('');

  const loadSessions = async (forceRefresh = false) => {
    // Check cache first (unless force refresh)
    if (!forceRefresh) {
      const cached = sessionStorage.getItem('sessions');
      if (cached) {
        try {
          setSessions(JSON.parse(cached));
          setLoading(false);
          return;
        } catch {}
      }
    }

    try {
      const data = await fetchSessions();
      setSessions(data);
      // Cache the result
      sessionStorage.setItem('sessions', JSON.stringify(data));
    } catch (err) {
      console.error('Failed to load sessions:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSessions();
  }, []);

  // Refresh sessions when becoming visible
  useEffect(() => {
    if (visible) {
      loadSessions(true);
    }
  }, [visible]);

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Are you sure you want to delete this session?')) return;
    try {
      await deleteSession(id);
      const updated = sessions.filter(s => s.id !== id);
      setSessions(updated);
      // Update cache
      sessionStorage.setItem('sessions', JSON.stringify(updated));
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete');
    }
  };

  const handleStartEditName = (session: Session, e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingSessionId(session.id);
    setEditingName(session.name || formatRelativeTime(session.startTime));
  };

  const handleSaveName = async (sessionId: string) => {
    if (!editingName.trim()) {
      setEditingSessionId(null);
      return;
    }
    try {
      await updateSessionName(sessionId, editingName.trim());
      const updated = sessions.map(s => 
        s.id === sessionId ? { ...s, name: editingName.trim() } : s
      );
      setSessions(updated);
      sessionStorage.setItem('sessions', JSON.stringify(updated));
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update name');
    }
    setEditingSessionId(null);
  };

  const handleCancelEdit = () => {
    setEditingSessionId(null);
  };

  const handleDirSelect = (path: string) => {
    onNewSession(path);
    setShowDirPicker(false);
  };

  // Group sessions by cwd (project)
  const grouped = sessions.reduce((acc, session) => {
    const cwd = session.cwd || 'Unknown';
    if (!acc[cwd]) acc[cwd] = [];
    acc[cwd].push(session);
    return acc;
  }, {} as Record<string, Session[]>);

  if (loading) return <div className="p-4 text-sm text-gray-500">Loading...</div>;

  return (
    <div className="h-full overflow-y-auto">
      {/* New Project Button */}
      <div className="p-2 border-b border-gray-200">
        <button
          onClick={() => setShowDirPicker(true)}
          className="w-full flex items-center justify-center px-3 py-2 text-xs font-medium text-green-600 bg-green-50 rounded hover:bg-green-100 transition-colors"
        >
          <Plus className="w-3 h-3 mr-1" />
          New Project Session
        </button>
      </div>
      {showDirPicker && (
        <DirectoryPicker
          onSelect={handleDirSelect}
          onCancel={() => setShowDirPicker(false)}
        />
      )}
      {Object.keys(grouped).length === 0 ? (
        <div className="p-4 text-sm text-gray-500 text-center">No sessions yet.<br/>Click above to create a new session!</div>
      ) : (
        Object.entries(grouped).map(([cwd, projectSessions]) => (
          <div key={cwd} className="border-b border-gray-100">
            <div className="px-3 py-2 bg-gray-50 flex items-center justify-between">
              <div className="text-xs font-medium text-gray-600 truncate flex-1" title={cwd}>
                📁 {cwd.split('/').pop() || cwd}
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => onNewSession(cwd)}
                  className="p-1 hover:bg-gray-200 rounded text-gray-500 transition-colors"
                  title="New session in this project"
                >
                  <Plus className="w-3 h-3" />
                </button>
                <button
                  onClick={async (e) => {
                    e.stopPropagation();
                    if (!confirm(`Delete all ${projectSessions.length} session(s) in this project?`)) return;
                    try {
                      await Promise.all(projectSessions.map(s => deleteSession(s.id)));
                      const updated = sessions.filter(s => s.cwd !== cwd);
                      setSessions(updated);
                      sessionStorage.setItem('sessions', JSON.stringify(updated));
                    } catch (err) {
                      alert(err instanceof Error ? err.message : 'Failed to delete');
                    }
                  }}
                  className="p-1 hover:bg-red-100 rounded text-red-400 transition-colors"
                  title="Delete all sessions in this project"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
            </div>
            {projectSessions.map(session => (
              <div
                key={session.id}
                onClick={() => onSessionSelect(session.id)}
                className={`flex items-center justify-between px-3 py-2 cursor-pointer group ${
                  currentSessionId === session.id
                    ? 'bg-blue-50 border-l-2 border-blue-500'
                    : 'hover:bg-gray-50'
                }`}
              >
                <div className="flex items-center text-sm text-gray-700 flex-1 min-w-0">
                  <Clock className="w-3 h-3 mr-2 text-gray-400 flex-shrink-0" />
                  {editingSessionId === session.id ? (
                    <input
                      type="text"
                      value={editingName}
                      onChange={(e) => setEditingName(e.target.value)}
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') handleSaveName(session.id);
                        if (e.key === 'Escape') handleCancelEdit();
                      }}
                      className="flex-1 px-1 py-0 text-sm border border-blue-400 rounded focus:outline-none"
                      autoFocus
                    />
                  ) : (
                    <span 
                      className="truncate cursor-text"
                      onDoubleClick={(e) => handleStartEditName(session, e)}
                      title={session.name || formatRelativeTime(session.startTime)}
                    >
                      {session.name || formatRelativeTime(session.startTime)}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={(e) => handleStartEditName(session, e)}
                    className="p-1 hover:bg-blue-100 rounded text-blue-500"
                    title="Edit name"
                  >
                    <Pencil className="w-3 h-3" />
                  </button>
                  <button
                    onClick={(e) => handleDelete(session.id, e)}
                    className="p-1 hover:bg-red-100 rounded text-red-500"
                    title="Delete session"
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  );
}

// ── Chat Message ─────────────────────────────────────────────────────────────

interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  contextFiles?: string[];
  agent?: string;
  model?: string;
  isEditing?: boolean;
}

// Parse thinking content from assistant message
function parseThinkingContent(content: string): { thinking: string | null; mainContent: string; isThinkingComplete: boolean } {
  const thinkingMatch = content.match(/<thinking>([\s\S]*?)(<\/thinking>|$)/);
  
  if (!thinkingMatch) {
    return { thinking: null, mainContent: content, isThinkingComplete: false };
  }
  
  const thinking = thinkingMatch[1].trim();
  const isThinkingComplete = content.includes('</thinking>');
  const mainContent = content.replace(/<thinking>[\s\S]*?(<\/thinking>|$)/, '').trim();
  
  return { thinking, mainContent, isThinkingComplete };
}

// Thinking display component - defined OUTSIDE parent to preserve state across re-renders
function ThinkingBlock({ thinking, isStreaming, isComplete }: { thinking: string; isStreaming: boolean; isComplete: boolean }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevIsStreamingRef = useRef(isStreaming);
  
  // Auto-expand when streaming starts, auto-collapse when streaming stops
  useEffect(() => {
    const wasStreaming = prevIsStreamingRef.current;
    
    if (isStreaming && !isComplete) {
      // Streaming started or continuing: expand
      setIsExpanded(true);
    } else if (wasStreaming && !isStreaming) {
      // Streaming just stopped: collapse
      setIsExpanded(false);
    }
    
    prevIsStreamingRef.current = isStreaming;
  }, [isStreaming, isComplete]);
  
  // Auto-scroll when streaming
  useEffect(() => {
    if (isStreaming && !isComplete && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [thinking, isStreaming, isComplete]);
  
  // Handle manual toggle
  const handleToggle = () => {
    setIsExpanded(!isExpanded);
  };
  
  // During streaming, show expanded with scroll
  if (isStreaming && !isComplete) {
    return (
      <div className="mb-3 border border-purple-200 rounded-lg bg-purple-50/50">
        <div className="flex items-center gap-2 px-3 py-1.5 border-b border-purple-200 bg-purple-100/50 rounded-t-lg">
          <Brain className="w-3.5 h-3.5 text-purple-600" />
          <span className="text-xs font-medium text-purple-700">Thinking...</span>
          <div className="flex-1" />
          <div className="flex gap-1">
            <div className="w-1.5 h-1.5 bg-purple-500 rounded-full animate-pulse" />
            <div className="w-1.5 h-1.5 bg-purple-500 rounded-full animate-pulse" style={{ animationDelay: '0.2s' }} />
            <div className="w-1.5 h-1.5 bg-purple-500 rounded-full animate-pulse" style={{ animationDelay: '0.4s' }} />
          </div>
        </div>
        <div 
          ref={scrollRef}
          className="px-3 py-2 text-xs text-gray-600 overflow-y-auto font-mono whitespace-pre-wrap"
          style={{ maxHeight: '150px' }}
        >
          {thinking}
        </div>
      </div>
    );
  }
  
  // Collapsed state - show as clickable button
  return (
    <div className="mb-3">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-purple-600 bg-purple-50 hover:bg-purple-100 border border-purple-200 rounded-lg transition-colors"
      >
        <Brain className="w-3.5 h-3.5" />
        <span>Thinking</span>
        {isExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
      </button>
      {isExpanded && (
        <div className="mt-2 border border-purple-200 rounded-lg bg-purple-50/50">
          <div className="px-3 py-2 text-xs text-gray-600 overflow-y-auto font-mono whitespace-pre-wrap max-h-60">
            {thinking}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Main Chat Page ───────────────────────────────────────────────────────────

export default function ChatPage() {
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [sidebarTab, setSidebarTab] = useState<'files' | 'sessions'>('files');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [contextFiles, setContextFiles] = useState<string[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string>('');
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [currentProject, setCurrentProject] = useState<string | null>(null);
  const [currentCwd, setCurrentCwd] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Load agents and config
    fetchAgents().then(setAgents).catch(console.error);
    fetchConfig().then(setConfig).catch(console.error);
  }, []);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleAddToChat = (path: string) => {
    if (!contextFiles.includes(path)) {
      setContextFiles([...contextFiles, path]);
    }
  };

  const handleRemoveContext = (path: string) => {
    setContextFiles(contextFiles.filter(f => f !== path));
  };

  const handleSessionSelect = async (sessionId: string) => {
    setCurrentSessionId(sessionId);
    try {
      const response = await fetch(`/api/sessions/${sessionId}`);
      if (response.ok) {
        const data = await response.json();
        // Load session messages - try openaiMessages first, then messages
        const rawMessages = data.openaiMessages || data.messages || [];
        const loadedMessages: ChatMessage[] = rawMessages
          .filter((msg: any) => msg.role !== 'system') // Skip system messages
          .map((msg: any) => ({
            role: msg.role as 'user' | 'assistant',
            content: msg.content,
            timestamp: msg.timestamp || new Date().toISOString(),
          }));
        setMessages(loadedMessages);
        // Set current project from session metadata
        if (data.metadata?.cwd) {
          setCurrentProject(data.metadata.cwd.split('/').pop() || data.metadata.cwd);
          setCurrentCwd(data.metadata.cwd);
        }
      }
    } catch (err) {
      console.error('Failed to load session:', err);
    }
  };

  const handleNewSession = (cwd?: string) => {
    // Start a new session - preserve current project if no cwd provided
    const projectCwd = cwd || currentCwd || '';
    if (!projectCwd) {
      alert('Please select a project first');
      return;
    }
    setCurrentSessionId(null);
    setMessages([]);
    setInputValue('');
    setContextFiles([]);
    setCurrentProject(projectCwd.split('/').pop() || projectCwd);
    setCurrentCwd(projectCwd);
    setSidebarTab('sessions');
  };

  const handleSendMessage = async () => {
    if (!inputValue.trim()) return;
    
    const isFirstMessage = messages.length === 0;
    const userMessageContent = inputValue.trim();
    
    const userMessage: ChatMessage = {
      role: 'user',
      content: userMessageContent,
      timestamp: new Date().toISOString(),
      contextFiles: [...contextFiles], // Bind context to message
      agent: selectedAgent || undefined,
      model: selectedModel || undefined,
    };
    
    setMessages(prev => [...prev, userMessage]);
    setInputValue('');
    setContextFiles([]); // Clear context after sending

    // Add empty assistant message for streaming
    const assistantMessageIndex = messages.length + 1;
    const assistantMessage: ChatMessage = {
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
    };
    setMessages(prev => [...prev, assistantMessage]);

    // Generate session name for first message in new session
    if (isFirstMessage && !currentSessionId) {
      try {
        const name = await generateSessionName(userMessageContent);
        // Update session name in backend after session is created
        // We'll do this after the streaming completes
        setTimeout(async () => {
          if (currentSessionId) {
            try {
              await updateSessionName(currentSessionId, name);
              // Refresh sessions list
              sessionStorage.removeItem('sessions');
            } catch (err) {
              console.error('Failed to update session name:', err);
            }
          }
        }, 2000);
      } catch (err) {
        console.error('Failed to generate session name:', err);
      }
    }

    // Set up abort controller for stop functionality
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    setIsStreaming(true);

    try {
      // Call backend streaming API
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: userMessage.content,
          session_id: currentSessionId,
          context_files: userMessage.contextFiles,
          agent: userMessage.agent,
          model: userMessage.model,
        }),
        signal: abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      if (!response.body) {
        throw new Error('No response body');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let accumulatedContent = '';
      let accumulatedThinking = '';
      let thinkingClosed = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split('\n');

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            try {
              const parsed = JSON.parse(data);
              
              // Handle thinking content
              if (parsed.thinking) {
                accumulatedThinking += parsed.thinking;
                // Build display content: thinking (unclosed if still streaming)
                const thinkingBlock = thinkingClosed 
                  ? `<thinking>${accumulatedThinking}</thinking>`
                  : `<thinking>${accumulatedThinking}`;
                const displayContent = accumulatedContent 
                  ? `${thinkingBlock}\n\n${accumulatedContent}`
                  : thinkingBlock;
                
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: displayContent,
                  };
                  return newMessages;
                });
              }
              
              // Handle main content
              if (parsed.chunk) {
                // When we receive main content, close the thinking tag
                if (!thinkingClosed && accumulatedThinking) {
                  thinkingClosed = true;
                }
                
                accumulatedContent += parsed.chunk;
                
                // Build display content
                let displayContent = accumulatedContent;
                if (accumulatedThinking) {
                  const thinkingBlock = `<thinking>${accumulatedThinking}</thinking>`;
                  displayContent = `${thinkingBlock}\n\n${accumulatedContent}`;
                }
                
                // Update the assistant message with accumulated content
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: displayContent,
                  };
                  return newMessages;
                });
              }
            } catch {
              // Ignore parse errors for incomplete JSON
            }
          }
        }
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        // User stopped the stream - keep whatever content we have
        console.log('Stream aborted by user');
      } else {
        const errorMessage = `Error: ${error instanceof Error ? error.message : 'Failed to get response'}`;
        setMessages(prev => {
          const newMessages = [...prev];
          newMessages[assistantMessageIndex] = {
            ...newMessages[assistantMessageIndex],
            content: errorMessage,
          };
          return newMessages;
        });
      }
    } finally {
      setIsStreaming(false);
      abortControllerRef.current = null;
    }
  };

  const handleStopStreaming = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
      setIsStreaming(false);
    }
  }, []);

  const handleEditMessage = (index: number) => {
    const msg = messages[index];
    if (msg.role !== 'user') return;
    
    // Load message data into input fields
    setInputValue(msg.content);
    setContextFiles(msg.contextFiles || []);
    setSelectedAgent(msg.agent || '');
    setSelectedModel(msg.model || '');
    
    // Remove this message and all subsequent messages
    setMessages(messages.slice(0, index));
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  return (
    <div className="h-full flex">
      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col bg-white min-w-0">
        {/* Chat Header */}
        <div className="px-3 md:px-4 py-3 border-b border-gray-200 bg-white flex items-center justify-between">
          <div className="flex items-center min-w-0 flex-1">
            <MessageSquare className="w-5 h-5 mr-2 text-blue-500 flex-shrink-0" />
            <h1 className="text-base md:text-lg font-semibold text-gray-900 truncate">Chat</h1>
            {currentProject && (
              <span className="ml-2 px-2 py-0.5 bg-blue-50 text-blue-700 text-xs font-medium rounded border border-blue-200 truncate max-w-[120px] md:max-w-none">
                {currentProject}
              </span>
            )}
            {currentSessionId && (
              <span className="ml-2 px-2 py-0.5 bg-gray-50 text-gray-500 text-xs rounded border border-gray-200 hidden md:inline">
                #{currentSessionId}
              </span>
            )}
          </div>
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className={`p-2 rounded hover:bg-gray-100 transition-colors flex-shrink-0 ${
              sidebarOpen ? 'text-blue-600' : 'text-gray-500'
            }`}
            title={sidebarOpen ? 'Close sidebar' : 'Open sidebar'}
          >
            <PanelRight className="w-5 h-5" />
          </button>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {messages.length === 0 ? (
            <div className="h-full flex items-center justify-center text-gray-400">
              <div className="text-center">
                <MessageSquare className="w-12 h-12 mx-auto mb-4 opacity-30" />
                <p className="text-sm">Start a conversation</p>
                <p className="text-xs mt-1">Type a message below or use @ to reference files</p>
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              {messages.map((msg, idx) => (
                <div
                  key={idx}
                  className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'} group`}
                >
                  <div className="max-w-2xl">
                    {/* Show context files for user messages */}
                    {msg.role === 'user' && msg.contextFiles && msg.contextFiles.length > 0 && (
                      <div className="flex items-center gap-1 flex-wrap mb-1 px-2">
                        <span className="text-xs text-gray-500">Context:</span>
                        {msg.contextFiles.map(file => (
                          <span
                            key={file}
                            className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-gray-100 border border-gray-200 rounded text-xs text-gray-600"
                          >
                            <File className="w-3 h-3" />
                            {file.split('/').pop()}
                          </span>
                        ))}
                      </div>
                    )}
                    <div
                      className={`rounded-lg px-4 py-3 ${
                        msg.role === 'user'
                          ? 'bg-blue-500 text-white'
                          : 'bg-gray-100 text-gray-900'
                      }`}
                    >
                      {msg.role === 'assistant' ? (
                        <div>
                          {(() => {
                            const { thinking, mainContent, isThinkingComplete } = parseThinkingContent(msg.content);
                            // Thinking is streaming only if: overall streaming AND this is last message AND thinking not complete
                            const isThinkingStreaming = isStreaming && idx === messages.length - 1 && !isThinkingComplete;
                            
                            return (
                              <>
                                {thinking && (
                                  <ThinkingBlock 
                                    thinking={thinking} 
                                    isStreaming={isThinkingStreaming}
                                    isComplete={isThinkingComplete}
                                  />
                                )}
                                {mainContent && (
                                  <div className="prose prose-sm max-w-none prose-pre:bg-gray-800 prose-pre:text-gray-100 prose-code:bg-gray-200 prose-code:px-1 prose-code:py-0.5 prose-code:rounded">
                                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                      {mainContent}
                                    </ReactMarkdown>
                                  </div>
                                )}
                              </>
                            );
                          })()}
                        </div>
                      ) : (
                        <pre className="whitespace-pre-wrap text-sm font-sans">{msg.content}</pre>
                      )}
                      <div className={`text-xs mt-1 flex items-center gap-2 ${msg.role === 'user' ? 'text-blue-100' : 'text-gray-400'}`}>
                        <span>{new Date(msg.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span>
                        {msg.agent && <span>• Agent: {msg.agent}</span>}
                        {msg.model && <span>• Model: {msg.model}</span>}
                      </div>
                    </div>
                    {/* Edit button for user messages */}
                    {msg.role === 'user' && (
                      <button
                        onClick={() => handleEditMessage(idx)}
                        className="opacity-0 group-hover:opacity-100 mt-1 px-2 py-1 text-xs text-blue-600 hover:bg-blue-50 rounded transition-opacity"
                      >
                        Edit & Resend
                      </button>
                    )}
                  </div>
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Input Area */}
        <div className="border-t border-gray-200 bg-white px-4 py-3">
          <div className="flex items-end gap-3">
            <textarea
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type a message... (Shift+Enter for newline)"
              className="flex-1 resize-none border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              rows={2}
            />
            {isStreaming ? (
              <button
                onClick={handleStopStreaming}
                className="p-2 bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors"
                title="Stop generation"
              >
                <Square className="w-5 h-5" />
              </button>
            ) : (
              <button
                onClick={handleSendMessage}
                disabled={!inputValue.trim()}
                className="p-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                <Send className="w-5 h-5" />
              </button>
            )}
          </div>
          {/* Context Files */}
          {contextFiles.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap mt-2 px-2 py-1.5 bg-blue-50 rounded">
              <span className="text-xs font-medium text-blue-700">Context:</span>
              {contextFiles.map(file => (
                <span
                  key={file}
                  className="inline-flex items-center gap-1 px-2 py-0.5 bg-white border border-blue-200 rounded text-xs text-blue-700"
                >
                  <File className="w-3 h-3" />
                  {file.split('/').pop()}
                  <button
                    onClick={() => handleRemoveContext(file)}
                    className="hover:text-red-500"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
          {/* Agent and Model Selection */}
          <div className="flex gap-3 mt-2">
            <select
              value={selectedAgent}
              onChange={(e) => setSelectedAgent(e.target.value)}
              className="flex-1 px-2 py-1 border border-gray-300 rounded text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            >
              <option value="">Default Agent</option>
              {agents.map(agent => (
                <option key={agent.name} value={agent.name}>
                  {agent.name}
                </option>
              ))}
            </select>
            <select
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              className="flex-1 px-2 py-1 border border-gray-300 rounded text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            >
              <option value="">Default Model</option>
              {config && Object.entries(config.endpoints).map(([id, endpoint]) => (
                <option key={id} value={endpoint.model}>
                  {endpoint.model}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Right Sidebar */}
      {sidebarOpen && (
        <div className="w-72 border-l border-gray-200 bg-white flex flex-col">
          {/* Sidebar Tabs */}
          <div className="flex border-b border-gray-200">
            <button
              className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
                sidebarTab === 'files'
                  ? 'text-blue-600 border-b-2 border-blue-600 bg-blue-50'
                  : 'text-gray-500 hover:text-gray-700'
              }`}
              onClick={() => setSidebarTab('files')}
            >
              <FolderTree className="w-3 h-3 inline mr-1" />
              Files
            </button>
            <button
              className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
                sidebarTab === 'sessions'
                  ? 'text-blue-600 border-b-2 border-blue-600 bg-blue-50'
                  : 'text-gray-500 hover:text-gray-700'
              }`}
              onClick={() => setSidebarTab('sessions')}
            >
              <MessageSquare className="w-3 h-3 inline mr-1" />
              Sessions
            </button>
          </div>

          {/* Sidebar Content */}
          <div className="flex-1 overflow-hidden relative">
            <div className={`absolute inset-0 ${sidebarTab === 'files' ? 'visible' : 'invisible hidden'}`}>
              <FileTree
                onFileSelect={setSelectedFile}
                onAddToChat={handleAddToChat}
                selectedFile={selectedFile}
                cwd={currentCwd}
                visible={sidebarTab === 'files'}
              />
            </div>
            <div className={`absolute inset-0 ${sidebarTab === 'sessions' ? 'visible' : 'invisible hidden'}`}>
              <SessionsPanel
                onSessionSelect={handleSessionSelect}
                onNewSession={handleNewSession}
                currentSessionId={currentSessionId}
                visible={sidebarTab === 'sessions'}
              />
            </div>
          </div>
        </div>
      )}

      {/* File Viewer Overlay */}
      {selectedFile && (
        <div className="w-1/2 border-l border-gray-200">
          <FileViewer
            filePath={selectedFile}
            onClose={() => setSelectedFile(null)}
          />
        </div>
      )}
    </div>
  );
}

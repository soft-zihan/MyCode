import { useState, useEffect, useRef, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { 
  FolderTree, ChevronRight, ChevronDown, File, Folder, X, 
  MessageSquare, Trash2, PanelRight, Send, Clock, Plus,
  Edit3, Check, Pencil, Brain, Square, Loader2
} from 'lucide-react';
import { 
  fetchWorkspaceTree, fetchWorkspaceFile, WorkspaceNode,
  fetchSessions, deleteSession, Session,
  fetchAgents, fetchConfig, Agent, AppConfig,
  fetchDirectories, DirectoryList,
  deleteWorkspaceFile, renameWorkspaceFile, createWorkspaceFile,
  generateSessionName, updateSessionName, saveSessionMessages
} from '../api/client';
import { ReviewPanel, FileSnapshot } from '../components/ReviewPanel';
import { DiffViewer } from '../components/DiffViewer';
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
  refreshTrigger?: number;
}

function FileTree({ onFileSelect, onAddToChat, selectedFile, cwd, visible, refreshTrigger }: FileTreeProps) {
  const [tree, setTree] = useState<WorkspaceNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [editMode, setEditMode] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [newFileName, setNewFileName] = useState('');
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

  // Refresh tree when trigger changes (after file modifications)
  useEffect(() => {
    if (refreshTrigger && refreshTrigger > 0) {
      loadTree(true);
    }
  }, [refreshTrigger]);

  const handleCreateFile = async () => {
    if (!newFileName.trim()) {
      setIsCreating(false);
      return;
    }
    
    try {
      await createWorkspaceFile(newFileName.trim(), cwd || undefined);
      setIsCreating(false);
      setNewFileName('');
      loadTree(true);
    } catch (err) {
      alert(err instanceof Error ? err.message : '创建失败');
    }
  };

  const handleCreateCancel = () => {
    setIsCreating(false);
    setNewFileName('');
  };

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
        <div className="flex items-center gap-1">
          <button
            onClick={() => setIsCreating(!isCreating)}
            className={`p-1 rounded transition-colors ${
              isCreating 
                ? 'bg-green-100 text-green-700 hover:bg-green-200' 
                : 'hover:bg-gray-200 text-gray-600'
            }`}
            title={isCreating ? '取消新建' : '新建文件'}
          >
            <Plus className="w-4 h-4" />
          </button>
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
      </div>
      {isCreating && (
        <div className="px-3 py-2 border-b border-gray-200 bg-green-50">
          <div className="flex items-center gap-2">
            <File className="w-4 h-4 text-green-600" />
            <input
              type="text"
              value={newFileName}
              onChange={(e) => setNewFileName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleCreateFile();
                if (e.key === 'Escape') handleCreateCancel();
              }}
              placeholder="输入文件名（如 src/new.ts）"
              className="flex-1 px-2 py-1 text-sm border border-green-400 rounded focus:outline-none focus:ring-1 focus:ring-green-500"
              autoFocus
            />
            <button
              onClick={handleCreateFile}
              className="p-1 hover:bg-green-200 rounded text-green-700"
              title="确认"
            >
              <Check className="w-4 h-4" />
            </button>
            <button
              onClick={handleCreateCancel}
              className="p-1 hover:bg-gray-200 rounded text-gray-600"
              title="取消"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}
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

// ── File Diff Viewer ─────────────────────────────────────────────────────────

interface FileDiffViewerProps {
  filePath: string;
  oldContent: string;
  newContent: string;
  isNew: boolean;
  onClose: () => void;
}

function FileDiffViewer({ filePath, oldContent, newContent, isNew, onClose }: FileDiffViewerProps) {
  const fileName = filePath.split('/').pop() || filePath;
  
  return (
    <div className="h-full flex flex-col bg-white">
      <div className="flex items-center justify-between px-4 py-2 border-b border-gray-200 bg-gray-50">
        <div className="flex items-center gap-2">
          <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
            isNew ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'
          }`}>
            {isNew ? 'NEW' : 'EDIT'}
          </span>
          <span className="text-sm font-medium text-gray-700">{fileName}</span>
          <span className="text-xs text-gray-400">{filePath}</span>
        </div>
        <button
          onClick={onClose}
          className="p-1 hover:bg-gray-200 rounded"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="flex-1 overflow-hidden p-3">
        <DiffViewer
          oldContent={oldContent}
          newContent={newContent}
          maxHeight={800}
        />
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
  if (!content || typeof content !== 'string') {
    return { thinking: null, mainContent: content || '', isThinkingComplete: false };
  }
  
  // Find ALL thinking blocks and concatenate them
  const thinkingBlocks: string[] = [];
  const tagRegex = /<thinking>([\s\S]*?)<\/thinking>/g;
  let match;
  while ((match = tagRegex.exec(content)) !== null) {
    thinkingBlocks.push(match[1].trim());
  }
  
  const isThinkingComplete = content.includes('</thinking>');
  
  // Check for unclosed thinking at the end
  const lastOpenTag = content.lastIndexOf('<thinking>');
  const lastCloseTag = content.lastIndexOf('</thinking>');
  if (lastOpenTag > lastCloseTag) {
    // There's an unclosed thinking block
    thinkingBlocks.push(content.slice(lastOpenTag + 10).trim());
  }
  
  const thinking = thinkingBlocks.length > 0 ? thinkingBlocks.join('\n\n') : null;
  
  // Remove ALL thinking blocks to get main content
  const mainContent = content.replace(/<thinking>[\s\S]*?<\/thinking>/g, '').replace(/<thinking>[\s\S]*$/, '').trim();
  
  return { thinking, mainContent, isThinkingComplete };
}

// Parse content into independent sections: thinking blocks are separate, not merged
interface ContentSection {
  type: 'thinking' | 'text';
  content: string;
  isComplete: boolean;
}

function parseContentSections(content: string): ContentSection[] {
  if (!content || typeof content !== 'string') {
    return [{ type: 'text', content: content || '', isComplete: true }];
  }
  
  const sections: ContentSection[] = [];
  let remaining = content;
  const isThinkingComplete = content.includes('</thinking>');
  
  // Split by thinking tags
  while (remaining.length > 0) {
    const openIdx = remaining.indexOf('<thinking>');
    if (openIdx === -1) {
      // Rest is normal text
      const text = remaining.trim();
      if (text) sections.push({ type: 'text', content: text, isComplete: true });
      break;
    }
    
    // Text before thinking block
    const beforeText = remaining.substring(0, openIdx).trim();
    if (beforeText) sections.push({ type: 'text', content: beforeText, isComplete: true });
    
    const closeIdx = remaining.indexOf('</thinking>', openIdx + 10);
    let thinkingContent: string;
    let thinkingComplete: boolean;
    
    if (closeIdx === -1) {
      // Unclosed thinking at end
      thinkingContent = remaining.substring(openIdx + 10).trim();
      thinkingComplete = false;
      sections.push({ type: 'thinking', content: thinkingContent, isComplete: false });
      remaining = '';
    } else {
      thinkingContent = remaining.substring(openIdx + 10, closeIdx).trim();
      thinkingComplete = true;
      sections.push({ type: 'thinking', content: thinkingContent, isComplete: true });
      remaining = remaining.substring(closeIdx + 11).trimStart();
    }
  }
  
  return sections.length > 0 ? sections : [{ type: 'text', content: '', isComplete: true }];
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

interface ToolCollapsibleProps {
  label: string;
  icon: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}

function ToolCollapsible({ label, icon, defaultOpen = false, children }: ToolCollapsibleProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  
  // Auto-collapse when new content arrives (when defaultOpen changes from true to false)
  useEffect(() => {
    setIsOpen(defaultOpen);
  }, [defaultOpen]);
  
  return (
    <details
      open={isOpen}
      onToggle={(e) => setIsOpen((e.target as HTMLDetailsElement).open)}
      className="my-1 border border-gray-200 rounded-lg bg-gray-50 overflow-hidden group/details"
    >
      <summary className="flex items-center gap-2 px-3 py-1.5 cursor-pointer hover:bg-gray-100 transition-colors select-none text-sm font-medium text-gray-700 list-none [&::-webkit-details-marker]:hidden">
        <span className="text-xs">{icon}</span>
        <span className="flex-1">{label}</span>
        <ChevronDown className="w-3 h-3 text-gray-400 transition-transform group-open/details:rotate-180" />
      </summary>
      <div className="px-3 pb-2 border-t border-gray-200">
        {children}
      </div>
    </details>
  );
}

// Split markdown content into alternating normal/tool sections
function simplifyToolResult(toolName: string, rawResult: string): string {
  if (!rawResult) return `**✅ 工具结果:** \`${toolName}\` — (empty)`;
  
  // write_file: show file path with hyperlink
  if (toolName === 'write_file') {
    const pathMatch = rawResult.match(/Successfully wrote to (.+?) \((\d+) lines\)/);
    if (pathMatch) {
      const filePath = pathMatch[1];
      const lineCount = pathMatch[2];
      const fileName = filePath.split('/').pop() || filePath;
      return `**✅ 工具结果:** 新建了 [${fileName}](${filePath}) (+${lineCount} lines)`;
    }
    return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${rawResult}\n\`\`\``;
  }
  
  // edit_file: show file path with hyperlink and diff summary
  if (toolName === 'edit_file') {
    const pathMatch = rawResult.match(/Successfully edited (.+?)(?:\s*\(matched via|$)/m);
    if (pathMatch) {
      const filePath = pathMatch[1].trim();
      const fileName = filePath.split('/').pop() || filePath;
      // Count added/removed lines from diff
      const addedLines = (rawResult.match(/^\+.*$/gm) || []).length;
      const removedLines = (rawResult.match(/^-.*$/gm) || []).length;
      const diffInfo = [];
      if (addedLines > 0) diffInfo.push(`+${addedLines}`);
      if (removedLines > 0) diffInfo.push(`-${removedLines}`);
      const summary = diffInfo.length > 0 ? ` (${diffInfo.join(' ')})` : '';
      return `**✅ 工具结果:** 编辑了 [${fileName}](${filePath})${summary}`;
    }
    return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${rawResult}\n\`\`\``;
  }
  
  // read_file: show file path and line count
  if (toolName === 'read_file') {
    const lineMatch = rawResult.match(/\[Lines (\d+)-(\d+) of (\d+) total\]/);
    const numberedMatch = rawResult.match(/^(\d+)\s*\|/m);
    
    let lineInfo = '';
    if (lineMatch) {
      lineInfo = `(${lineMatch[1]}-${lineMatch[2]}/${lineMatch[3]} lines)`;
    } else if (numberedMatch) {
      const lines = rawResult.split('\n').filter(l => l.match(/^\d+\s*\|/));
      lineInfo = `(${lines.length} lines)`;
    }
    
    return `**✅ 工具结果:** \`${toolName}\` — 文件内容 ${lineInfo}\n\n<details><summary>点击查看完整内容</summary>\n\n\`\`\`\n${rawResult}\n\`\`\`\n\n</details>`;
  }
  
  // grep_search: show match count, simplify output
  if (toolName === 'grep_search') {
    const lines = rawResult.split('\n').filter(l => l.trim());
    if (rawResult === 'No matches found.') {
      return `**✅ 工具结果:** \`${toolName}\` — 无匹配结果`;
    }
    const matchCount = lines.length;
    const preview = lines.slice(0, 10).join('\n');
    const truncated = matchCount > 10 ? `\n\n... (共 ${matchCount} 条结果，显示前 10 条)` : '';
    return `**✅ 工具结果:** \`${toolName}\` — ${matchCount} 条匹配\n\n\`\`\`\n${preview}${truncated}\n\`\`\``;
  }
  
  // list_files: just show file list
  if (toolName === 'list_files') {
    const files = rawResult.split('\n').filter(f => f.trim());
    return `**✅ 工具结果:** \`${toolName}\` — ${files.length} 个文件\n\n\`\`\`\n${rawResult}\n\`\`\``;
  }
  
  // run_shell: show command output
  if (toolName === 'run_shell') {
    const preview = rawResult.length > 300 ? rawResult.substring(0, 300) + '...' : rawResult;
    return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${preview}\n\`\`\``;
  }
  
  // Default: show truncated result
  const preview = rawResult.length > 500 ? rawResult.substring(0, 500) + '...' : rawResult;
  return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${preview}\n\`\`\``;
}

function simplifyToolCall(toolName: string, input: Record<string, unknown>): string {
  if (toolName === 'read_file') {
    const p = String(input.file_path || '');
    const name = p.split('/').pop() || p;
    const offset = input.offset ? ` [${input.offset}+]` : '';
    return `📄 Read [${name}](${p})${offset}`;
  }
  if (toolName === 'write_file') {
    const p = String(input.file_path || '');
    const name = p.split('/').pop() || p;
    return `📝 Write [${name}](${p})`;
  }
  if (toolName === 'edit_file') {
    const p = String(input.file_path || '');
    const name = p.split('/').pop() || p;
    const replaceAll = input.replaceAll ? ' (all)' : '';
    return `✏️ Edit [${name}](${p})${replaceAll}`;
  }
  if (toolName === 'run_shell') {
    const cmd = String(input.command || '');
    const display = cmd.length > 60 ? cmd.substring(0, 60) + '...' : cmd;
    return `▶️ \`${display}\``;
  }
  if (toolName === 'list_files') {
    const p = input.path || '.';
    return `📂 List ${p}`;
  }
  if (toolName === 'grep_search') {
    return `🔍 Search \`${input.pattern}\``;
  }
  if (toolName === 'webfetch') {
    return `🌐 Fetch ${input.url}`;
  }
  
  return `🔧 ${toolName}`;
}

function splitToolSections(content: string): Array<{ type: 'normal' | 'tool'; text: string; label?: string; icon?: string }> {
  const sections: Array<{ type: 'normal' | 'tool'; text: string; label?: string; icon?: string }> = [];

  // First handle :::tool-block markers (combined call+result blocks)
  const toolBlockRegex = /(?:^|\n\n):::tool-block\n([\s\S]*?)\n:::(?:\n\n|$)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = toolBlockRegex.exec(content)) !== null) {
    const normalText = content.substring(lastIndex, match.index).trim();
    if (normalText) {
      sections.push({ type: 'normal', text: normalText });
    }
    const blockContent = match[1].trim();
    // Extract tool name from first line (simplifyToolCall output)
    const firstLine = blockContent.split('\n')[0] || '';
    const toolName = firstLine.replace(/^[📄📝✏️▶📂🔍🌐⏳🔧]\s*/, '').split(' ')[0] || 'tool';
    sections.push({ type: 'tool', text: blockContent, label: toolName, icon: '🔧' });
    lastIndex = match.index + match[0].length;
  }

  // Then handle legacy tool blocks: starts with bold emoji marker, may include following code block
  const toolRegex = /(?:^|\n\n)(\*\*(?:\u{1F527}|\u2705|[\u{1F916}]|\u2713|\u2139\uFE0F)[^*]*\*\*(?:\s*\n(?:```\w*\n[\s\S]*?```|[\s\S]*?))?(?=\n\n|\n*$))/gu;

  while ((match = toolRegex.exec(content)) !== null) {
    // Add normal text before this tool block
    const normalText = content.substring(lastIndex, match.index).trim();
    if (normalText) {
      sections.push({ type: 'normal', text: normalText });
    }

    // Parse the tool block
    const block = match[0].trim();
    const toolCallMatch = block.match(/^\*\*\u{1F527}[^*]*\*[^`]*`([^`]+)`/u);
    if (toolCallMatch) {
      const codeBlockMatch = block.match(/```[\s\S]*?```/);
      sections.push({ type: 'tool', text: codeBlockMatch ? codeBlockMatch[0] : '', label: `\u8C03\u7528\u5DE5\u5177: ${toolCallMatch[1]}`, icon: '\u{1F527}' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    const toolResultMatch = block.match(/^\*\*\u2705[^*]*\*[^`]*`([^`]+)`/u);
    if (toolResultMatch) {
      const codeBlockMatch = block.match(/```[\s\S]*?```/);
      const result = codeBlockMatch ? codeBlockMatch[0] : '';
      const preview = result.length > 120 ? result.substring(0, 120) + '...' : result;
      sections.push({ type: 'tool', text: result, label: `\u5DE5\u5177\u7ED3\u679C: ${toolResultMatch[1]} \u2014 ${preview}`, icon: '\u2705' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    const subAgentStartMatch = block.match(/^\*\*\u{1F916}[^*]*\*[^`]*`([^`]+)`\s*-\s*(.*)/u);
    if (subAgentStartMatch) {
      sections.push({ type: 'tool', text: '', label: `\u542F\u52A8\u5B50Agent: ${subAgentStartMatch[1]} \u2014 ${subAgentStartMatch[2]}`, icon: '\u{1F916}' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    const subAgentEndMatch = block.match(/^\*\*\u2713[^*]*\*[^`]*`([^`]+)`/u);
    if (subAgentEndMatch) {
      sections.push({ type: 'tool', text: '', label: `\u5B50Agent\u5B8C\u6210: ${subAgentEndMatch[1]}`, icon: '\u2713' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    const infoMatch = block.match(/^\*\*\u2139\uFE0F[^*]*\*\*\s*(.*)/u);
    if (infoMatch) {
      sections.push({ type: 'tool', text: infoMatch[1], label: '\u4FE1\u606F', icon: '\u2139\uFE0F' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    // Not a recognized tool block — treat as normal
    sections.push({ type: 'normal', text: block });
    lastIndex = match.index + match[0].length;
  }

  // Remaining normal text
  const remaining = content.substring(lastIndex).trim();
  if (remaining) {
    if (sections.length > 0 && sections[sections.length - 1].type === 'normal') {
      sections[sections.length - 1].text += '\n\n' + remaining;
    } else {
      sections.push({ type: 'normal', text: remaining });
    }
  }

  return sections;
}

// Helper function to build display content from parts
function buildDisplayContent(
  contentParts: Array<{type: 'thinking' | 'text', content: string, complete?: boolean}>,
  accumulatedContent: string,
  currentThinking: string | null
): string {
  let result = '';
  
  // Add completed thinking blocks
  for (const part of contentParts) {
    if (part.type === 'thinking') {
      result += `<thinking>${part.content}</thinking>\n\n`;
    } else {
      result += part.content;
    }
  }
  
  // Add current thinking if exists
  if (currentThinking !== null) {
    result += `<thinking>${currentThinking}`;
  }
  
  // Add accumulated content
  if (accumulatedContent) {
    if (currentThinking !== null) {
      result += `\n\n${accumulatedContent}`;
    } else {
      result += accumulatedContent;
    }
  }
  
  return result.trim();
}

// Main Chat Page

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
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef(true);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const messagesRef = useRef<ChatMessage[]>([]);
  const [fileSnapshots, setFileSnapshots] = useState<FileSnapshot[]>([]);
  const [pendingToolCalls, setPendingToolCalls] = useState<Map<string, string>>(new Map());
  const pendingToolCallRef = useRef<{ name: string; input: Record<string, unknown> } | null>(null);
  const [fileTreeRefreshTrigger, setFileTreeRefreshTrigger] = useState(0);
  const isSubAgentActiveRef = useRef(false);
  const subAgentTypeRef = useRef('');
  const subAgentOutputRef = useRef('');
  const [subAgentOutputs, setSubAgentOutputs] = useState<Array<{id: string, type: string, output: string}>>([]);
  // Cache messages for each session so switching doesn't unload
  const sessionMessagesCache = useRef<Map<string, ChatMessage[]>>(new Map());

  useEffect(() => {
    // Load agents and config
    fetchAgents().then(setAgents).catch(console.error);
    fetchConfig().then(setConfig).catch(console.error);
  }, []);

  // Keep messagesRef in sync for saving after streaming
  useEffect(() => {
    messagesRef.current = messages;
    // Also update cache for current session
    if (currentSessionId) {
      sessionMessagesCache.current.set(currentSessionId, messages);
    }
  }, [messages]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    isNearBottomRef.current = true;
    setShowScrollBtn(false);
  };

  const handleScroll = () => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
    isNearBottomRef.current = atBottom;
    setShowScrollBtn(!atBottom);
  };

  useEffect(() => {
    if (isNearBottomRef.current) {
      scrollToBottom();
    }
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
    // Save current session messages to cache before switching
    if (currentSessionId && messages.length > 0) {
      sessionMessagesCache.current.set(currentSessionId, messages);
    }
    
    setCurrentSessionId(sessionId);
    
    // Check cache first - instant switch
    const cached = sessionMessagesCache.current.get(sessionId);
    if (cached && cached.length > 0) {
      setMessages(cached);
      return;
    }
    
    // Not in cache, load from backend
    try {
      const response = await fetch(`/api/sessions/${sessionId}`);
      if (response.ok) {
        const data = await response.json();
        
        // Use frontendMessages if available (lossless)
        if (data.frontendMessages && data.frontendMessages.length > 0) {
          setMessages(data.frontendMessages);
        } else {
          // Transform raw OpenAI messages to frontend format
          const rawMessages = data.openaiMessages || data.messages || [];
          const loadedMessages: ChatMessage[] = [];
          const pendingSessionToolCalls: Array<{ name: string; input: Record<string, unknown> }> = [];
          
          for (const msg of rawMessages) {
            if (msg.role === 'system') continue;
            
            if (msg.role === 'assistant') {
              let content = '';
              
              // Embed tool_calls as combined blocks (paired with next tool result)
              if (msg.tool_calls && msg.tool_calls.length > 0) {
                for (const tc of msg.tool_calls) {
                  const fn = tc.function || {};
                  const name = fn.name || 'unknown';
                  let input: Record<string, unknown> = {};
                  try { input = JSON.parse(fn.arguments || '{}'); } catch {}
                  // Store for later pairing with tool result
                  pendingSessionToolCalls.push({ name, input });
                }
              }
              
              // Add text content
              if (typeof msg.content === 'string' && msg.content) {
                content = msg.content;
              } else if (Array.isArray(msg.content)) {
                // Anthropic format
                const parts: string[] = [];
                for (const block of msg.content) {
                  if (block.type === 'thinking' && block.thinking) {
                    parts.push(`<thinking>${block.thinking}</thinking>`);
                  } else if (block.type === 'text' && block.text) {
                    parts.push(block.text);
                  }
                }
                content = parts.join('\n\n');
              }
              
              if (content) {
                loadedMessages.push({
                  role: 'assistant',
                  content: content.trim(),
                  timestamp: msg.timestamp || new Date().toISOString(),
                });
              }
            } else if (msg.role === 'tool') {
              // Build combined tool call + result block
              const rawResult = typeof msg.content === 'string' ? msg.content : '';
              const pendingCall = pendingSessionToolCalls.shift();
              
              let combinedContent: string;
              if (pendingCall) {
                const callLine = simplifyToolCall(pendingCall.name, pendingCall.input);
                const resultBlock = simplifyToolResult(msg.name || '', rawResult);
                combinedContent = `:::tool-block\n${callLine}\n\n${resultBlock}\n:::`;
              } else {
                combinedContent = simplifyToolResult(msg.name || '', rawResult);
              }
              
              loadedMessages.push({
                role: 'assistant',
                content: combinedContent,
                timestamp: msg.timestamp || new Date().toISOString(),
              });
            } else if (msg.role === 'user') {
              let content = '';
              if (typeof msg.content === 'string') {
                content = msg.content;
              } else if (Array.isArray(msg.content)) {
                content = msg.content
                  .filter((b: any) => b.type === 'text')
                  .map((b: any) => b.text)
                  .join('\n');
              }
              // Strip <system-reminder> blocks — these are injected context, not user speech
              content = content.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '').trim();
              if (content) {
                loadedMessages.push({
                  role: 'user',
                  content,
                  timestamp: msg.timestamp || new Date().toISOString(),
                });
              }
            }
          }
          
          setMessages(loadedMessages);
        }
        
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
    // messages.length is the current length (before either setMessages takes effect)
    // After both appends: [...existing, userMessage, assistantMessage]
    // assistantMessage index = messages.length + 1
    const assistantMessageIndex = messages.length + 1;
    const assistantMessage: ChatMessage = {
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
    };
    setMessages(prev => [...prev, assistantMessage]);

    // Generate session name for first message in new session (non-blocking)
    if (isFirstMessage && !currentSessionId) {
      // Don't await - let it run in background
      generateSessionName(userMessageContent)
        .then(name => {
          // Update after streaming completes
          setTimeout(async () => {
            if (currentSessionId) {
              try {
                await updateSessionName(currentSessionId, name);
                sessionStorage.removeItem('sessions');
              } catch (err) {
                console.error('Failed to update session name:', err);
              }
            }
          }, 2000);
        })
        .catch(err => console.error('Failed to generate session name:', err));
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
      // Build content as an array of parts for proper interleaving of thinking/text blocks
      // Each part is either {type:'thinking', content:string, complete:bool} or {type:'text', content:string}
      let contentParts: Array<{type: 'thinking' | 'text', content: string, complete?: boolean}> = [];
      let currentThinking: string | null = null; // null means not in thinking mode
      let accumulatedContent = ''; // plain text content (non-thinking)

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
                if (currentThinking === null) {
                  // Start a new thinking block
                  currentThinking = parsed.thinking;
                } else {
                  // Append to current thinking block
                  currentThinking += parsed.thinking;
                }
                
                // Build display content from parts
                const displayContent = buildDisplayContent(contentParts, accumulatedContent, currentThinking);
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
                // If we were in thinking mode, close the thinking block and save it
                if (currentThinking !== null) {
                  contentParts.push({type: 'thinking', content: currentThinking, complete: true});
                  currentThinking = null;
                }
                
                // Route to sub-agent output if active, otherwise main content
                if (isSubAgentActiveRef.current) {
                  subAgentOutputRef.current += parsed.chunk;
                } else {
                  accumulatedContent += parsed.chunk;
                }
                
                // Build display content from parts
                const displayContent = buildDisplayContent(contentParts, accumulatedContent, currentThinking);
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: displayContent,
                  };
                  return newMessages;
                });
              }
              
              // Handle tool call events - 存到 ref，等 tool_result 到了一起渲染
              if (parsed.tool_call) {
                const toolCall = parsed.tool_call;
                pendingToolCallRef.current = { name: toolCall.name, input: toolCall.input };
                setPendingToolCalls(prev => new Map(prev).set(toolCall.id || toolCall.name, toolCall.name));
                
                // Show loading indicator in content
                const toolCallDisplay = `\n\n⏳ ${toolCall.name}...\n`;
                accumulatedContent += toolCallDisplay;
                
                const displayContent = buildDisplayContent(contentParts, accumulatedContent, currentThinking);
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: displayContent,
                  };
                  return newMessages;
                });
              }
              
              // Handle tool result events - 合并 tool_call + result 到一个块
              if (parsed.tool_result) {
                const toolResult = parsed.tool_result;
                setPendingToolCalls(prev => {
                  const next = new Map(prev);
                  next.delete(toolResult.id || toolResult.name);
                  return next;
                });
                
                // Build combined tool block
                const pending = pendingToolCallRef.current;
                pendingToolCallRef.current = null;
                
                // Remove the loading indicator from accumulatedContent
                accumulatedContent = accumulatedContent.replace(/\n\n⏳ [^\n]+\n$/, '');
                
                let combinedBlock: string;
                if (pending && pending.name === toolResult.name) {
                  // Build combined call+result block
                  const callLine = simplifyToolCall(pending.name, pending.input);
                  const resultText = simplifyToolResult(toolResult.name, toolResult.result);
                  combinedBlock = `\n\n:::tool-block\n${callLine}\n\n${resultText}\n:::\n\n`;
                } else {
                  combinedBlock = `\n\n${simplifyToolResult(toolResult.name, toolResult.result)}\n\n`;
                }
                accumulatedContent += combinedBlock;
                
                // Capture file snapshot for review
                if (toolResult.snapshot) {
                  const snap = toolResult.snapshot;
                  if (snap.old_content !== undefined && snap.new_content !== undefined) {
                    setFileSnapshots(prev => {
                      // Deduplicate by file_path
                      const filtered = prev.filter(s => s.file_path !== snap.file_path);
                      return [...filtered, {
                        file_path: snap.file_path,
                        is_new: snap.is_new,
                        old_content: snap.old_content,
                        new_content: snap.new_content,
                      }];
                    });
                  }
                }
                
                // Always refresh file tree after any tool that might affect files
                if (['write_file', 'edit_file', 'run_shell'].includes(toolResult.name)) {
                  setFileTreeRefreshTrigger(prev => prev + 1);
                }
                
                const displayContent = buildDisplayContent(contentParts, accumulatedContent, currentThinking);
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: displayContent,
                  };
                  return newMessages;
                });
              }
              
              // Handle sub-agent start events - begin collecting output in scrollable box
              if (parsed.sub_agent_start) {
                const subAgent = parsed.sub_agent_start;
                isSubAgentActiveRef.current = true;
                subAgentTypeRef.current = subAgent.agent_type;
                subAgentOutputRef.current = '';
                const startMarker = `\n\n**🤖 启动子Agent:** \`${subAgent.agent_type}\` - ${subAgent.description}\n\n:::sub-agent-start\n`;
                accumulatedContent += startMarker;
                
                const displayContent = buildDisplayContent(contentParts, accumulatedContent, currentThinking);
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: displayContent,
                  };
                  return newMessages;
                });
              }
              
              // Handle sub-agent end events - close the scrollable box
              if (parsed.sub_agent_end) {
                const subAgent = parsed.sub_agent_end;
                isSubAgentActiveRef.current = false;
                const subOutput = subAgentOutputRef.current;
                const endMarker = `:::sub-agent-end\n**✓ 子Agent完成:** \`${subAgent.agent_type}\`\n`;
                accumulatedContent += endMarker;
                
                // Store sub-agent output for rendering
                if (subOutput) {
                  setSubAgentOutputs(prev => [...prev, {
                    id: `sub-${Date.now()}`,
                    type: subAgent.agent_type,
                    output: subOutput,
                  }]);
                }
                
                const displayContent = buildDisplayContent(contentParts, accumulatedContent, currentThinking);
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: displayContent,
                  };
                  return newMessages;
                });
              }
              
              // Handle info events
              if (parsed.info) {
                const infoDisplay = `\n\n**ℹ️ 信息:** ${parsed.info}\n\n`;
                accumulatedContent += infoDisplay;
                
                const displayContent = buildDisplayContent(contentParts, accumulatedContent, currentThinking);
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: displayContent,
                  };
                  return newMessages;
                });
              }
              
              // Handle error events
              if (parsed.error) {
                const errorDisplay = `\n\n**❌ 错误:** ${parsed.error}\n\n`;
                accumulatedContent += errorDisplay;
                
                setMessages(prev => {
                  const newMessages = [...prev];
                  newMessages[assistantMessageIndex] = {
                    ...newMessages[assistantMessageIndex],
                    content: accumulatedContent,
                  };
                  return newMessages;
                });
              }
              
              // Handle done event - save session ID and persist messages
              if (parsed.done) {
                const doneSessionId = parsed.session_id || currentSessionId;
                if (doneSessionId) {
                  setCurrentSessionId(doneSessionId);
                  // Save pure conversation only (no context/skills/MCP)
                  const snapshot = messagesRef.current;
                  if (snapshot.length > 0) {
                    // Strip context files and system data - only save role + content
                    const pureMessages = snapshot.map(msg => ({
                      role: msg.role,
                      content: typeof msg.content === 'string' 
                        ? msg.content.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '').trim()
                        : msg.content,
                      timestamp: msg.timestamp,
                    }));
                    sessionMessagesCache.current.set(doneSessionId, snapshot);
                    saveSessionMessages(doneSessionId, pureMessages).catch(err =>
                      console.error('Failed to save session messages:', err)
                    );
                  }
                }
              }
            } catch {
              // Ignore parse errors for incomplete JSON
            }
          }
        }
      }
      
      // Stream ended - finalize
      setPendingToolCalls(new Map()); // Clear any pending tool calls
      
      // Close unclosed thinking block if any
      if (currentThinking !== null) {
        contentParts.push({type: 'thinking', content: currentThinking, complete: true});
        currentThinking = null;
      }
      const finalContent = buildDisplayContent(contentParts, accumulatedContent, null);
      setMessages(prev => {
        const newMessages = [...prev];
        if (newMessages[assistantMessageIndex]) {
          newMessages[assistantMessageIndex] = {
            ...newMessages[assistantMessageIndex],
            content: finalContent,
          };
        }
        return newMessages;
      });
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

  // File review handlers
  const handleAcceptFile = useCallback((filePath: string) => {
    setFileSnapshots(prev => prev.filter(s => s.file_path !== filePath));
  }, []);

  const handleRejectFile = useCallback(async (filePath: string) => {
    const snap = fileSnapshots.find(s => s.file_path === filePath);
    if (!snap) return;
    try {
      const res = await fetch('/api/revert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: filePath, old_content: snap.old_content }),
      });
      if (res.ok) {
        setFileSnapshots(prev => prev.filter(s => s.file_path !== filePath));
        // Trigger file tree refresh
        setFileTreeRefreshTrigger(prev => prev + 1);
      }
    } catch (err) {
      console.error('Failed to revert file:', err);
    }
  }, [fileSnapshots]);

  const handleAcceptAll = useCallback(() => {
    setFileSnapshots([]);
  }, []);

  const handleOpenFile = useCallback((filePath: string) => {
    setSelectedFile(filePath);
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
        <div ref={scrollContainerRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-4 py-4">
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
                            const sections = parseContentSections(msg.content);
                            const isLastMessage = idx === messages.length - 1;
                            
                            // Find the last tool section for streaming expand
                            const mainContent = sections.filter(s => s.type === 'text').map(s => s.content).join('\n\n');
                            const toolSections = mainContent ? splitToolSections(mainContent) : [];
                            let lastToolIdx = -1;
                            for (let i = toolSections.length - 1; i >= 0; i--) {
                              if (toolSections[i].type === 'tool') { lastToolIdx = i; break; }
                            }
                            
                            const fileLinkRenderer = (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
                              const { href, children } = props;
                              if (href && !href.startsWith('http') && !href.startsWith('#')) {
                                return (
                                  <span 
                                    className="text-blue-600 hover:text-blue-800 underline cursor-pointer font-mono text-xs"
                                    onClick={(e) => {
                                      e.preventDefault();
                                      e.stopPropagation();
                                      setSelectedFile(href);
                                      setSidebarTab('files');
                                    }}
                                  >
                                    {children}
                                  </span>
                                );
                              }
                              return <a {...props} />;
                            };
                            
                            return (
                              <>
                                {sections.map((section, sIdx) => {
                                  if (section.type === 'thinking') {
                                    const isThinkingStreaming = isStreaming && isLastMessage && !section.isComplete;
                                    return (
                                      <ThinkingBlock 
                                        key={`think-${sIdx}`}
                                        thinking={section.content} 
                                        isStreaming={isThinkingStreaming}
                                        isComplete={section.isComplete}
                                      />
                                    );
                                  }
                                  // Text section — render with tool sections
                                  const textSections = splitToolSections(section.content);
                                  if (textSections.length === 0) return null;
                                  return (
                                    <div key={`text-${sIdx}`} className="prose prose-sm max-w-none">
                                      {textSections.map((ts, tIdx) => {
                                        const globalToolIdx = tIdx; // within this text section
                                        if (ts.type === 'normal') {
                                          return <ReactMarkdown key={tIdx} remarkPlugins={[remarkGfm]} components={{ a: fileLinkRenderer }}>{ts.text}</ReactMarkdown>;
                                        }
                                        const isLastTool = globalToolIdx === lastToolIdx;
                                        const defaultOpen = isStreaming && isLastMessage && isLastTool;
                                        return (
                                          <ToolCollapsible key={tIdx} label={ts.label || 'Tool'} icon={ts.icon || '🔧'} defaultOpen={defaultOpen}>
                                            {ts.text ? (
                                              <div className="prose prose-xs max-w-none">
                                                <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: fileLinkRenderer }}>{ts.text}</ReactMarkdown>
                                              </div>
                                            ) : null}
                                          </ToolCollapsible>
                                        );
                                      })}
                                    </div>
                                  );
                                })}
                                {/* Sub-agent output in fixed-height scrollable boxes */}
                                {subAgentOutputs.map((sa) => (
                                  <div key={sa.id} className="mt-2 border border-purple-200 rounded-lg overflow-hidden bg-purple-50/30">
                                    <div className="flex items-center gap-2 px-3 py-1.5 bg-purple-100 border-b border-purple-200 text-xs text-purple-700 font-medium">
                                      <span>🤖</span>
                                      <span>子Agent: {sa.type}</span>
                                    </div>
                                    <div className="h-64 overflow-y-auto p-3 text-xs font-mono whitespace-pre-wrap text-gray-700">
                                      {sa.output}
                                    </div>
                                  </div>
                                ))}
                                {/* Streaming indicator */}
                                {isStreaming && isLastMessage && !mainContent && sections.length === 0 && (
                                  <div className="flex items-center gap-2 text-gray-400 text-sm">
                                    <div className="flex gap-1">
                                      <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" />
                                      <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" style={{ animationDelay: '0.2s' }} />
                                      <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" style={{ animationDelay: '0.4s' }} />
                                    </div>
                                    <span>Thinking...</span>
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
                    {/* Delete button for all messages */}
                    <button
                      onClick={() => {
                        setMessages(prev => prev.filter((_, i) => i !== idx));
                      }}
                      className="opacity-0 group-hover:opacity-100 mt-1 px-2 py-1 text-xs text-red-500 hover:bg-red-50 rounded transition-opacity"
                    >
                      Delete
                    </button>
                  </div>
                </div>
              ))}
              {/* Tool execution loading indicator */}
              {pendingToolCalls.size > 0 && (
                <div className="flex justify-start">
                  <div className="max-w-2xl">
                    <div className="rounded-xl px-4 py-2 bg-gray-100 text-gray-600 text-sm flex items-center gap-2">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      <span>
                        {Array.from(pendingToolCalls.values()).join(', ')}...
                      </span>
                    </div>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>
          )}
          {/* Scroll to bottom button */}
          {showScrollBtn && (
            <button
              onClick={scrollToBottom}
              className="sticky bottom-2 float-right mr-2 px-3 py-1.5 bg-gray-800 text-white text-xs rounded-full shadow-lg hover:bg-gray-700 transition-colors z-10 flex items-center gap-1"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7m0 0l-7-7m7 7V3" />
              </svg>
              Bottom
            </button>
          )}
        </div>

        {/* Review Panel - shows changed files with accept/reject */}
        <ReviewPanel
          snapshots={fileSnapshots}
          onAccept={handleAcceptFile}
          onReject={handleRejectFile}
          onAcceptAll={handleAcceptAll}
          onOpenFile={handleOpenFile}
        />

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
                refreshTrigger={fileTreeRefreshTrigger}
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
      {selectedFile && (() => {
        // Check if this file has a snapshot (was edited in this session)
        const snapshot = fileSnapshots.find(s => s.file_path === selectedFile);
        if (snapshot) {
          return (
            <div className="w-1/2 border-l border-gray-200">
              <FileDiffViewer
                filePath={snapshot.file_path}
                oldContent={snapshot.old_content}
                newContent={snapshot.new_content}
                isNew={snapshot.is_new}
                onClose={() => setSelectedFile(null)}
              />
            </div>
          );
        }
        return (
          <div className="w-1/2 border-l border-gray-200">
            <FileViewer
              filePath={selectedFile}
              onClose={() => setSelectedFile(null)}
            />
          </div>
        );
      })()}
    </div>
  );
}

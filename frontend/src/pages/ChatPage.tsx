import { useState, useEffect, useRef, useCallback } from 'react';
import { 
  FolderTree, ChevronRight, ChevronDown, File, Folder, X, 
  MessageSquare, Trash2, PanelRight, Send, Clock, Plus,
  Edit3, Check, Pencil, Square, Zap
} from 'lucide-react';
import {
  fetchWorkspaceTree, fetchWorkspaceFile, WorkspaceNode,
  fetchSessions, deleteSession, Session,
  fetchAgents, fetchConfig, Agent, AppConfig,
  fetchDirectories, DirectoryList,
  deleteWorkspaceFile, renameWorkspaceFile, createWorkspaceFile,
  generateSessionName, updateSessionName, saveSessionMessages,
  compactSession, updatePermissionMode,
  forkSession, respondToPermission, truncateSession, PermissionRequest
} from '../api/client';
import { ReviewPanel, FileSnapshot } from '../components/ReviewPanel';
import { DiffViewer } from '../components/DiffViewer';
import Editor from '@monaco-editor/react';
import { ChatView, useChatNodes } from '../components/chat/nodes';

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
  onNewSession: (cwd?: string) => void;
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

// Convert node snapshot to ChatMessage[] for saving
function snapshotToMessages(snapshot: { order: string[]; nodes: Map<string, any> }): ChatMessage[] {
  const messages: ChatMessage[] = [];
  for (const key of snapshot.order) {
    const node = snapshot.nodes.get(key);
    if (!node) continue;
    
    switch (node.kind) {
      case 'user':
        messages.push({
          role: 'user',
          content: node.content,
          timestamp: node.timestamp,
          contextFiles: node.contextFiles,
          agent: node.agent,
          model: node.model,
        });
        break;
      case 'assistant':
        let content = '';
        if (node.thinking) {
          content += `<thinking>${node.thinking}</thinking>\n\n`;
        }
        content += node.content || '';
        if (content.trim()) {
          messages.push({
            role: 'assistant',
            content: content.trim(),
            timestamp: node.timestamp,
          });
        }
        break;
      case 'tool-call':
        // Tool calls are embedded in assistant messages or shown separately
        // For simplicity, skip them here (they're in the node system)
        break;
      case 'error':
        messages.push({
          role: 'assistant',
          content: `**Error:** ${node.message}`,
          timestamp: new Date().toISOString(),
        });
        break;
      // Skip thinking, sub-agent, turn-status nodes
    }
  }
  return messages;
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
  const currentSessionIdRef = useRef<string | null>(null);
  const [currentProject, setCurrentProject] = useState<string | null>(null);
  const [currentCwd, setCurrentCwd] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  const [fileSnapshots, setFileSnapshots] = useState<FileSnapshot[]>([]);
  const [fileTreeRefreshTrigger, setFileTreeRefreshTrigger] = useState(0);
  const pendingSessionNameRef = useRef<string | null>(null);
  // Cache messages for each session so switching doesn't unload
  const sessionMessagesCache = useRef<Map<string, ChatMessage[]>>(new Map());
  
  // Session control states
  const [yoloMode, setYoloMode] = useState(true);  // Default to YOLO for web
  const contextUsed = 0;  // TODO: Track from agent
  const contextTotal = 128000;
  
  // Permission request state
  const [pendingPermission, setPendingPermission] = useState<PermissionRequest | null>(null);
  
  // Keep ref in sync with state
  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);
  
  // New node-based rendering
  const { snapshot: chatSnapshot, handleSSEEvent: handleNodeEvent, addUserMessage, resetNodes, loadMessages } = useChatNodes();

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
    
    // Reset state for new session
    resetNodes();
    pendingSessionNameRef.current = null;
    setPendingPermission(null);
    setFileSnapshots([]);  // Clear file snapshots from previous session
    setCurrentSessionId(sessionId);
    
    // Check cache first - instant switch
    const cached = sessionMessagesCache.current.get(sessionId);
    if (cached && cached.length > 0) {
      setMessages(cached);
      loadMessages(cached);
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
          loadMessages(loadedMessages);
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
    // Add user message to node system
    addUserMessage(userMessageContent, contextFiles.length > 0 ? contextFiles : undefined, selectedAgent || undefined, selectedModel || undefined);
    setInputValue('');
    setContextFiles([]);

    // Generate session name for first message in new session (non-blocking)
    if (isFirstMessage && !currentSessionId) {
      // Store the user message for later naming
      pendingSessionNameRef.current = userMessageContent;
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
              
              // Process through node system (handles display)
              handleNodeEvent(parsed);
              
              // Capture file snapshots for review
              if (parsed.tool_result?.snapshot) {
                const snap = parsed.tool_result.snapshot;
                if (snap.old_content !== undefined && snap.new_content !== undefined) {
                  setFileSnapshots(prev => {
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
              
              // Refresh file tree after file-modifying tools
              if (parsed.tool_result && ['write_file', 'edit_file', 'run_shell'].includes(parsed.tool_result.name)) {
                setFileTreeRefreshTrigger(prev => prev + 1);
              }
              
              // Handle permission request event
              if (parsed.permission_request) {
                const permReq = parsed.permission_request as PermissionRequest;
                setPendingPermission(permReq);
              }
              
              // Handle done event - save session ID and generate name if needed
              if (parsed.done) {
                const doneSessionId = parsed.session_id || currentSessionId;
                if (doneSessionId) {
                  setCurrentSessionId(doneSessionId);
                  
                  // Save frontend messages for lossless restore
                  const messagesToSave = snapshotToMessages(chatSnapshot);
                  saveSessionMessages(doneSessionId, messagesToSave).catch(err => {
                    console.error('Failed to save session messages:', err);
                  });
                  
                  // Generate session name for first message in new session
                  if (pendingSessionNameRef.current) {
                    const userMessage = pendingSessionNameRef.current;
                    pendingSessionNameRef.current = null;
                    
                    generateSessionName(userMessage)
                      .then(async name => {
                        try {
                          await updateSessionName(doneSessionId, name);
                          sessionStorage.removeItem('sessions');
                        } catch (err) {
                          console.error('Failed to update session name:', err);
                        }
                      })
                      .catch(err => console.error('Failed to generate session name:', err));
                  }
                }
              }
            } catch {
              // Ignore parse errors for incomplete JSON
            }
          }
        }
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        console.log('Stream aborted by user');
        // Save messages even on abort to preserve progress
        if (currentSessionId) {
          const messagesToSave = snapshotToMessages(chatSnapshot);
          saveSessionMessages(currentSessionId, messagesToSave).catch(err => {
            console.error('Failed to save session messages on abort:', err);
          });
        }
      } else {
        console.error('Streaming error:', error);
        // Save messages on error to preserve progress
        if (currentSessionId) {
          const messagesToSave = snapshotToMessages(chatSnapshot);
          saveSessionMessages(currentSessionId, messagesToSave).catch(err => {
            console.error('Failed to save session messages on error:', err);
          });
        }
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

  // Session control handlers
  const handleCompactSession = useCallback(async () => {
    if (!currentSessionId) return;
    try {
      await compactSession(currentSessionId);
    } catch (err) {
      console.error('Failed to compact session:', err);
    }
  }, [currentSessionId]);

  const handleToggleYoloMode = useCallback(async () => {
    if (!currentSessionId) return;
    const newMode = !yoloMode;
    setYoloMode(newMode);
    try {
      await updatePermissionMode(currentSessionId, newMode ? 'bypassPermissions' : 'default');
    } catch (err) {
      console.error('Failed to update permission mode:', err);
    }
  }, [currentSessionId, yoloMode]);

  const handleForkSession = useCallback(async () => {
    if (!currentSessionId) return;
    try {
      const result = await forkSession(currentSessionId);
      if (result.new_session_id) {
        // Clear cache for the new session to force reload from backend
        sessionMessagesCache.current.delete(result.new_session_id);
        // Switch to the new session using handleSessionSelect
        await handleSessionSelect(result.new_session_id);
        // Refresh session list
        sessionStorage.removeItem('sessions');
      }
    } catch (err) {
      console.error('Failed to fork session:', err);
    }
  }, [currentSessionId, handleSessionSelect]);

  const handleEditMessage = useCallback(async (index: number) => {
    const msg = messages[index];
    if (msg.role !== 'user') return;
    
    // Count how many user messages to keep (up to but not including this one)
    let keepUserMessages = 0;
    for (let i = 0; i < index; i++) {
      if (messages[i].role === 'user') {
        keepUserMessages++;
      }
    }
    
    // Load message data into input fields
    setInputValue(msg.content);
    setContextFiles(msg.contextFiles || []);
    setSelectedAgent(msg.agent || '');
    setSelectedModel(msg.model || '');
    
    // Truncate backend session
    if (currentSessionId) {
      try {
        await truncateSession(currentSessionId, keepUserMessages);
      } catch (err) {
        console.error('Failed to truncate session:', err);
      }
    }
    
    // Remove this message and all subsequent messages from frontend
    const remainingMessages = messages.slice(0, index);
    setMessages(remainingMessages);
    
    // Rebuild nodes from remaining messages
    loadMessages(remainingMessages);
  }, [messages, loadMessages, currentSessionId]);

  // Permission handlers
  const handlePermissionApprove = useCallback(async () => {
    if (!pendingPermission || !currentSessionId) return;
    try {
      await respondToPermission(currentSessionId, pendingPermission.request_id, true);
      setPendingPermission(null);
    } catch (err) {
      console.error('Failed to approve permission:', err);
    }
  }, [pendingPermission, currentSessionId]);

  const handlePermissionDeny = useCallback(async () => {
    if (!pendingPermission || !currentSessionId) return;
    try {
      await respondToPermission(currentSessionId, pendingPermission.request_id, false);
      setPendingPermission(null);
    } catch (err) {
      console.error('Failed to deny permission:', err);
    }
  }, [pendingPermission, currentSessionId]);

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
          
          {/* Session Controls */}
          <div className="flex items-center gap-2 mr-2">
            {/* Context Progress Bar */}
            <div className="flex items-center gap-2">
              <div className="w-32 h-2 bg-gray-200 rounded-full overflow-hidden">
                <div 
                  className={`h-full transition-all ${
                    (contextUsed / contextTotal) >= 0.9 ? 'bg-red-500' :
                    (contextUsed / contextTotal) >= 0.7 ? 'bg-yellow-500' :
                    'bg-green-500'
                  }`}
                  style={{ width: `${Math.min((contextUsed / contextTotal) * 100, 100)}%` }}
                />
              </div>
              <span className="text-xs text-gray-500">
                {Math.round(contextUsed / 1000)}k / {Math.round(contextTotal / 1000)}k
              </span>
              {currentSessionId && !isStreaming && (
                <button
                  onClick={handleCompactSession}
                  className="p-1 text-gray-600 hover:text-blue-600 hover:bg-blue-50 rounded transition-colors"
                  title="压缩上下文"
                >
                  <Zap className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
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

        {/* Messages - using new node-based rendering */}
        <ChatView 
          snapshot={chatSnapshot} 
          isStreaming={isStreaming}
          onEditMessage={handleEditMessage}
          onFileClick={(path) => {
            setSelectedFile(path);
            setSidebarTab('files');
          }}
        />

        {/* Review Panel - shows changed files with accept/reject */}
        <ReviewPanel
          snapshots={fileSnapshots}
          onAccept={handleAcceptFile}
          onReject={handleRejectFile}
          onAcceptAll={handleAcceptAll}
          onOpenFile={handleOpenFile}
        />

        {/* Permission Request Dialog */}
        {pendingPermission && (
          <div className="border-t border-yellow-300 bg-yellow-50 px-4 py-3">
            <div className="flex items-start gap-3">
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-yellow-800">⚠️ 需要授权</span>
                  <span className="text-xs px-1.5 py-0.5 bg-yellow-200 text-yellow-800 rounded">
                    {pendingPermission.tool_name}
                  </span>
                </div>
                <pre className="text-xs text-gray-700 bg-white border border-yellow-200 rounded p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap font-mono">
                  {pendingPermission.command}
                </pre>
              </div>
              <div className="flex gap-2 flex-shrink-0">
                <button
                  onClick={handlePermissionDeny}
                  className="px-3 py-1.5 text-sm font-medium text-red-700 bg-white border border-red-300 rounded hover:bg-red-50 transition-colors"
                >
                  拒绝
                </button>
                <button
                  onClick={handlePermissionApprove}
                  className="px-3 py-1.5 text-sm font-medium text-white bg-green-600 border border-green-700 rounded hover:bg-green-700 transition-colors"
                >
                  批准
                </button>
              </div>
            </div>
          </div>
        )}

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
            {/* YOLO Mode Toggle */}
            <button
              onClick={handleToggleYoloMode}
              className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
                yoloMode
                  ? 'text-green-600 bg-green-50 border border-green-200 hover:bg-green-100'
                  : 'text-gray-600 bg-white border border-gray-200 hover:bg-gray-50'
              }`}
              title={yoloMode ? 'YOLO模式：自动允许所有操作' : '默认模式：需要确认'}
            >
              {yoloMode ? '🚀 YOLO' : '🛡️ Default'}
            </button>
            {/* Fork Session */}
            {currentSessionId && (
              <button
                onClick={handleForkSession}
                className="px-3 py-1 text-xs font-medium text-gray-600 bg-white border border-gray-200 rounded hover:bg-gray-50 transition-colors"
                title="分叉会话"
              >
                🔀 Fork
              </button>
            )}
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

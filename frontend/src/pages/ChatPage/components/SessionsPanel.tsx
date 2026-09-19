import { useState, useEffect } from 'react';
import { ChevronRight, Folder, Clock, Plus, Pencil, Trash2, X, ChevronDown } from 'lucide-react';
import {
  fetchSessions, deleteSession, Session,
  fetchDirectories, DirectoryList,
  updateSessionName,
  fetchProjects, deleteProject, registerProject, Project,
} from '../../../api/client';

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
  onSessionSelect?: (id: string) => void;
  onNewSession?: (cwd?: string) => void;
  currentSessionId?: string | null;
  refreshTrigger?: number;
}

let sessionsCache: Session[] | null = null;
let projectsCache: Project[] | null = null;

export function SessionsPanel({ onSessionSelect, onNewSession, currentSessionId, refreshTrigger }: SessionsPanelProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [showDirPicker, setShowDirPicker] = useState(false);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState<string>('');
  const [collapsedProjects, setCollapsedProjects] = useState<Set<string>>(new Set());

  const toggleProjectCollapse = (cwd: string) => {
    setCollapsedProjects(prev => {
      const next = new Set(prev);
      if (next.has(cwd)) {
        next.delete(cwd);
      } else {
        next.add(cwd);
      }
      return next;
    });
  };

  const loadData = async (forceRefresh = false) => {
    if (!forceRefresh && sessionsCache && projectsCache) {
      setSessions(sessionsCache);
      setProjects(projectsCache);
      setLoading(false);
      return;
    }

    try {
      const [sessionsData, projectsData] = await Promise.all([
        fetchSessions(),
        fetchProjects(),
      ]);
      setSessions(sessionsData);
      setProjects(projectsData);
      sessionsCache = sessionsData;
      projectsCache = projectsData;
    } catch (err) {
      console.error('Failed to load data:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  useEffect(() => {
    loadData(true);
  }, [refreshTrigger]);

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Are you sure you want to delete this session?')) return;
    try {
      await deleteSession(id);
      const updated = sessions.filter(s => s.id !== id);
      setSessions(updated);
      sessionsCache = updated;
      
      if (currentSessionId === id) {
        onNewSession?.();
      }
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
      sessionsCache = updated;
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update name');
    }
    setEditingSessionId(null);
  };

  const handleCancelEdit = () => {
    setEditingSessionId(null);
  };

  const handleDirSelect = async (path: string) => {
    try {
      await registerProject(path);
    } catch (err) {
      console.error('Failed to register project:', err);
    }
    onNewSession?.(path);
    setShowDirPicker(false);
    loadData(true);
  };

  const grouped: Record<string, Session[]> = {};
  for (const project of projects) {
    grouped[project.cwd] = [];
  }
  for (const session of sessions) {
    const cwd = session.cwd || 'Unknown';
    if (!grouped[cwd]) grouped[cwd] = [];
    grouped[cwd].push(session);
  }

  // 按项目最新 session 时间排序
  const sortedProjects = Object.entries(grouped).sort(([, a], [, b]) => {
    const aLatest = a.length > 0 ? (a[0].startTime || 0) : 0;
    const bLatest = b.length > 0 ? (b[0].startTime || 0) : 0;
    return bLatest - aLatest;
  });

  return (
    <div className="flex flex-col h-full">
      <div className="p-2 border-b border-gray-200">
        <button
          onClick={() => setShowDirPicker(true)}
          className="w-full flex items-center justify-center px-3 py-1.5 text-xs font-medium text-green-600 bg-green-50 rounded hover:bg-green-100 transition-colors"
        >
          <Plus className="w-3 h-3 mr-1" />
          New Project
        </button>
      </div>
      {showDirPicker && (
        <DirectoryPicker
          onSelect={handleDirSelect}
          onCancel={() => setShowDirPicker(false)}
        />
      )}
      {loading ? (
        <div className="p-4 text-sm text-gray-500">Loading...</div>
      ) : projects.length === 0 ? (
        <div className="p-4 text-sm text-gray-500 text-center">No projects yet.</div>
      ) : (
        sortedProjects.map(([cwd, projectSessions]) => {
          const isCollapsed = collapsedProjects.has(cwd);
          return (
            <div key={cwd} className="border-b border-gray-100">
              <div 
                className="px-2 py-1.5 bg-gray-50 flex items-center justify-between cursor-pointer hover:bg-gray-100"
                onClick={() => toggleProjectCollapse(cwd)}
              >
                <div className="flex items-center flex-1 min-w-0">
                  {isCollapsed ? (
                    <ChevronRight className="w-3 h-3 mr-1 text-gray-400 flex-shrink-0" />
                  ) : (
                    <ChevronDown className="w-3 h-3 mr-1 text-gray-400 flex-shrink-0" />
                  )}
                  <div className="text-xs font-medium text-gray-600 truncate" title={cwd}>
                    {cwd.split('/').pop() || cwd}
                    {projectSessions.length > 0 && (
                      <span className="ml-1 text-gray-400">({projectSessions.length})</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-0.5" onClick={e => e.stopPropagation()}>
                  <button
                    onClick={() => onNewSession?.(cwd)}
                    className="p-0.5 hover:bg-gray-200 rounded text-gray-500 transition-colors"
                    title="New session"
                  >
                    <Plus className="w-3 h-3" />
                  </button>
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      const msg = projectSessions.length > 0
                        ? `Delete project and all ${projectSessions.length} session(s)?`
                        : `Delete this project?`;
                      if (!confirm(msg)) return;
                      try {
                        await Promise.all(projectSessions.map(s => deleteSession(s.id)));
                        await deleteProject(cwd);
                        loadData(true);
                        if (currentSessionId && projectSessions.some(s => s.id === currentSessionId)) {
                          onNewSession?.();
                        }
                      } catch (err) {
                        alert(err instanceof Error ? err.message : 'Failed to delete');
                      }
                    }}
                    className="p-0.5 hover:bg-red-100 rounded text-red-400 transition-colors"
                    title="Delete project"
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              </div>
              {!isCollapsed && projectSessions.map(session => (
                <div
                  key={session.id}
                  onClick={() => onSessionSelect?.(session.id)}
                  className={`flex items-center justify-between px-2 py-1.5 cursor-pointer group text-xs ${
                    currentSessionId === session.id
                      ? 'bg-blue-50 border-l-2 border-blue-500'
                      : 'hover:bg-gray-50'
                  }`}
                >
                  <div className="flex items-center text-gray-700 flex-1 min-w-0">
                    <Clock className="w-3 h-3 mr-1.5 text-gray-400 flex-shrink-0" />
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
                        className="flex-1 px-1 py-0 text-xs border border-blue-400 rounded focus:outline-none"
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
                  <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={(e) => handleStartEditName(session, e)}
                      className="p-0.5 hover:bg-blue-100 rounded text-blue-500"
                      title="Edit name"
                    >
                      <Pencil className="w-3 h-3" />
                    </button>
                    <button
                      onClick={(e) => handleDelete(session.id, e)}
                      className="p-0.5 hover:bg-red-100 rounded text-red-500"
                      title="Delete"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          );
        })
      )}
    </div>
  );
}

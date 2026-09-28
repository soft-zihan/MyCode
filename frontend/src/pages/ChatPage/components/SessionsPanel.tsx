import { useState, useEffect } from 'react';
import { ChevronRight, Folder, Clock, Plus, Pencil, Trash2, X, ChevronDown, GitBranch } from 'lucide-react';
import {
  fetchSessions, deleteSession, Session,
  fetchDirectories, DirectoryList,
  updateSessionName,
  fetchProjects, deleteProject, registerProject, Project,
  fetchWorktrees, createWorktree, removeWorktree, WorktreeRemoveError, WorktreeEntry,
} from '../../../api/client';
import { sessionStore, useSessionStore } from '../../../store';

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
                  className="flex items-center px-3 py-2 rounded hover:bg-indigo-50 cursor-pointer group"
                >
                  <Folder className="w-4 h-4 mr-2 text-indigo-500" />
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
  // U11 worktree：undefined = 非 git 项目（不显示 worktree 入口）
  const [worktreesByProject, setWorktreesByProject] = useState<Record<string, WorktreeEntry[] | undefined>>({});
  const [expandedWorktrees, setExpandedWorktrees] = useState<Set<string>>(new Set());

  const allProjections = useSessionStore(() => sessionStore.getAllProjections());

  const displaySessions = sessions.map(s => {
    const proj = allProjections.get(s.id);
    if (!proj) return s;
    return {
      ...s,
      name: proj.title || s.name,
      startTime: proj.updatedAt ? new Date(proj.updatedAt).toISOString() : s.startTime,
    };
  });

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

  const refreshWorktrees = async (cwd: string) => {
    try {
      const { worktrees, git } = await fetchWorktrees(cwd);
      setWorktreesByProject(prev => ({ ...prev, [cwd]: git ? worktrees : undefined }));
    } catch {
      setWorktreesByProject(prev => ({ ...prev, [cwd]: undefined }));
    }
  };

  const loadWorktrees = async (projectsData: Project[]) => {
    const results = await Promise.all(
      projectsData.map(async p => {
        try {
          const { worktrees, git } = await fetchWorktrees(p.cwd);
          return [p.cwd, git ? worktrees : undefined] as const;
        } catch {
          return [p.cwd, undefined] as const;
        }
      })
    );
    setWorktreesByProject(Object.fromEntries(results));
  };

  const handleCreateWorktree = async (projectCwd: string) => {
    try {
      await createWorktree(projectCwd);
      await refreshWorktrees(projectCwd);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to create worktree');
    }
  };

  const handleRemoveWorktree = async (entry: WorktreeEntry, projectCwd: string) => {
    const label = entry.name || entry.directory.split('/').pop() || entry.directory;
    try {
      await removeWorktree(entry.directory);
    } catch (err) {
      if (err instanceof WorktreeRemoveError && err.forceRequired) {
        if (!confirm(`${label} has uncommitted changes. Force delete?`)) return;
        try {
          await removeWorktree(entry.directory, true);
        } catch (err2) {
          alert(err2 instanceof Error ? err2.message : 'Failed to remove worktree');
          return;
        }
      } else {
        alert(err instanceof Error ? err.message : 'Failed to remove worktree');
        return;
      }
    }
    await refreshWorktrees(projectCwd);
  };

  const toggleWorktreePanel = (cwd: string) => {
    setExpandedWorktrees(prev => {
      const next = new Set(prev);
      if (next.has(cwd)) next.delete(cwd);
      else next.add(cwd);
      return next;
    });
  };

  const loadData = async (forceRefresh = false) => {
    if (!forceRefresh && sessionsCache && projectsCache) {
      setSessions(sessionsCache);
      setProjects(projectsCache);
      setLoading(false);
      loadWorktrees(projectsCache);
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
      loadWorktrees(projectsData);
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

  // U11：worktree 目录 → 父项目映射（worktree 会话归入父项目组，不另立项目——v2 UI 教训）
  const worktreeDirToProject: Record<string, { project: string; name: string | null }> = {};
  for (const [cwd, entries] of Object.entries(worktreesByProject)) {
    for (const e of entries || []) {
      if (e.kind === 'linked') {
        worktreeDirToProject[e.directory] = { project: cwd, name: e.name ?? null };
      }
    }
  }

  const grouped: Record<string, Session[]> = {};
  for (const project of projects) {
    grouped[project.cwd] = [];
  }
  for (const session of displaySessions) {
    const rawCwd = session.cwd || 'Unknown';
    const wtParent = worktreeDirToProject[rawCwd];
    const cwd = wtParent ? wtParent.project : rawCwd;
    if (!grouped[cwd]) grouped[cwd] = [];
    grouped[cwd].push(session);
  }

  // 按项目最新 session 时间排序
  const sortedProjects = Object.entries(grouped).sort(([, a], [, b]) => {
    const aLatest = a.length > 0 ? new Date(a[0].startTime).getTime() : 0;
    const bLatest = b.length > 0 ? new Date(b[0].startTime).getTime() : 0;
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
                  {worktreesByProject[cwd] && (
                    <button
                      onClick={() => toggleWorktreePanel(cwd)}
                      className={`p-0.5 rounded transition-colors ${
                        expandedWorktrees.has(cwd)
                          ? 'bg-slate-200 text-slate-700'
                          : 'hover:bg-gray-200 text-gray-500'
                      }`}
                      title="Worktrees"
                    >
                      <GitBranch className="w-3 h-3" />
                      {worktreesByProject[cwd]!.filter(e => e.kind === 'linked').length > 0 && (
                        <span className="ml-0.5 text-[9px] text-slate-500">
                          {worktreesByProject[cwd]!.filter(e => e.kind === 'linked').length}
                        </span>
                      )}
                    </button>
                  )}
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
              {!isCollapsed && expandedWorktrees.has(cwd) && (
                <div className="px-2 py-1 bg-slate-50 border-y border-slate-100">
                  {(worktreesByProject[cwd] || []).filter(e => e.kind === 'linked').length === 0 && (
                    <div className="text-[10px] text-gray-400 py-0.5">No worktrees</div>
                  )}
                  {(worktreesByProject[cwd] || []).filter(e => e.kind === 'linked').map(wt => (
                    <div key={wt.directory} className="flex items-center justify-between py-0.5 group/wt">
                      <div className="flex items-center min-w-0 text-[11px] text-gray-600">
                        <GitBranch className="w-3 h-3 mr-1 text-slate-400 flex-shrink-0" />
                        <span className="truncate" title={wt.directory}>
                          {wt.name || wt.directory.split('/').pop()}
                        </span>
                        {!wt.managed && (
                          <span className="ml-1 px-0.5 rounded bg-gray-200 text-gray-500 text-[9px] flex-shrink-0">ext</span>
                        )}
                      </div>
                      <div className="flex items-center gap-0.5 opacity-0 group-hover/wt:opacity-100 transition-opacity">
                        <button
                          onClick={() => onNewSession?.(wt.directory)}
                          className="p-0.5 hover:bg-gray-200 rounded text-gray-500"
                          title="New session in worktree"
                        >
                          <Plus className="w-3 h-3" />
                        </button>
                        <button
                          onClick={() => handleRemoveWorktree(wt, cwd)}
                          className="p-0.5 hover:bg-red-100 rounded text-red-400"
                          title="Remove worktree"
                        >
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </div>
                    </div>
                  ))}
                  <button
                    onClick={() => handleCreateWorktree(cwd)}
                    className="mt-0.5 flex items-center text-[11px] text-slate-500 hover:text-slate-700"
                  >
                    <Plus className="w-3 h-3 mr-0.5" /> New worktree
                  </button>
                </div>
              )}
              {!isCollapsed && projectSessions.map(session => (
                <div
                  key={session.id}
                  onClick={() => onSessionSelect?.(session.id)}
                  className={`flex items-center justify-between px-2 py-1.5 cursor-pointer group text-xs ${
                    currentSessionId === session.id
                      ? 'bg-indigo-50 border-l-2 border-indigo-500'
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
                        className="flex-1 px-1 py-0 text-xs border border-indigo-400 rounded focus:outline-none"
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
                    {session.cwd && worktreeDirToProject[session.cwd] && (
                      <span
                        className="ml-1 px-1 rounded bg-slate-100 text-slate-500 text-[9px] flex-shrink-0"
                        title={`worktree: ${worktreeDirToProject[session.cwd].name || session.cwd}`}
                      >
                        wt
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={(e) => handleStartEditName(session, e)}
                      className="p-0.5 hover:bg-indigo-100 rounded text-indigo-500"
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

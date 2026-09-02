import { useState, useEffect } from 'react';
import { 
  FolderTree, ChevronRight, ChevronDown, File, Folder, X, 
  Plus, Edit3, Check, Pencil, Trash2
} from 'lucide-react';
import {
  fetchWorkspaceTree, WorkspaceNode,
  deleteWorkspaceFile, renameWorkspaceFile, createWorkspaceFile,
} from '../../../api/client';

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

// In-memory cache for file trees (sessionStorage has ~5MB quota)
const fileTreeCache = new Map<string, WorkspaceNode>();

export function FileTree({ onFileSelect, onAddToChat, selectedFile, cwd, visible, refreshTrigger }: FileTreeProps) {
  const [tree, setTree] = useState<WorkspaceNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [editMode, setEditMode] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [newFileName, setNewFileName] = useState('');
  const cacheKey = cwd || 'default';

  const loadTree = (forceRefresh = false) => {
    if (!forceRefresh) {
      const cached = fileTreeCache.get(cacheKey);
      if (cached) {
        setTree(cached);
        setLoading(false);
        return;
      }
    }

    setLoading(true);
    fetchWorkspaceTree(cwd || undefined)
      .then(data => {
        setTree(data);
        fileTreeCache.set(cacheKey, data);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadTree();
  }, [cwd]);

  useEffect(() => {
    if (visible) {
      loadTree(true);
    }
  }, [visible]);

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

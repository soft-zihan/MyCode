import { useState, useEffect, useCallback } from 'react';
import { 
  FolderTree, ChevronRight, ChevronDown, File, Folder, X, 
  Plus, Edit3, Check, Pencil, Trash2, FolderPlus
} from 'lucide-react';
import {
  fetchWorkspaceTree, WorkspaceNode,
  deleteWorkspaceFile, renameWorkspaceFile, createWorkspaceFile,
  moveWorkspaceFile,
} from '../../../api/client';

interface FileTreeNodeProps {
  node: WorkspaceNode;
  level: number;
  onFileSelect: (path: string) => void;
  onAddToChat: (path: string) => void;
  selectedPath: string | null;
  onSelectPath: (path: string | null) => void;
  editMode?: boolean;
  cwd?: string | null;
  onRefresh?: () => void;
  onDragStart?: (path: string, type: 'file' | 'directory') => void;
  onDragEnd?: () => void;
  dragTarget?: string | null;
  onDragOver?: (path: string) => void;
  onDragLeave?: () => void;
  onDrop?: (targetPath: string) => void;
  creatingInPath: string | null;
  creatingType: 'file' | 'folder' | null;
  onCreated: (fullPath: string) => void;
}

function InlineCreateInput({ type, onCreated, onCancel, level }: {
  type: 'file' | 'folder';
  onCreated: (name: string) => void;
  onCancel: () => void;
  level: number;
}) {
  const [name, setName] = useState('');

  const handleConfirm = () => {
    if (name.trim()) onCreated(name.trim());
  };

  return (
    <div
      className={`flex items-center px-2 py-1 ${type === 'folder' ? 'bg-purple-50' : 'bg-green-50'}`}
      style={{ paddingLeft: `${level * 16 + 8}px` }}
    >
      {type === 'folder' ? (
        <FolderPlus className="w-4 h-4 mr-2 text-purple-500 flex-shrink-0" />
      ) : (
        <File className="w-4 h-4 mr-2 text-green-500 flex-shrink-0" />
      )}
      <input
        type="text"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === 'Enter') handleConfirm();
          if (e.key === 'Escape') onCancel();
        }}
        onClick={(e) => e.stopPropagation()}
        placeholder={type === 'folder' ? '文件夹名' : '文件名'}
        className={`flex-1 px-1 py-0 text-sm border rounded focus:outline-none ${
          type === 'folder' ? 'border-purple-400' : 'border-green-400'
        }`}
        autoFocus
      />
      <button onClick={handleConfirm} className="p-0.5 ml-1 hover:bg-green-100 rounded text-green-600">
        <Check className="w-3 h-3" />
      </button>
      <button onClick={onCancel} className="p-0.5 ml-0.5 hover:bg-gray-200 rounded text-gray-500">
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}

function FileTreeNode({ 
  node, level, onFileSelect, onAddToChat, selectedPath, onSelectPath,
  editMode, cwd, onRefresh, onDragStart, onDragEnd, dragTarget, onDragOver, onDragLeave, onDrop,
  creatingInPath, creatingType, onCreated
}: FileTreeNodeProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [isRenaming, setIsRenaming] = useState(false);
  const [newName, setNewName] = useState(node.name);
  const isSelected = selectedPath === node.path;
  const isDragTarget = dragTarget === node.path;
  const isCreatingHere = creatingInPath === node.path;

  useEffect(() => {
    if (isCreatingHere && !isExpanded) {
      setIsExpanded(true);
    }
  }, [isCreatingHere, isExpanded]);

  const handleClick = (e: React.MouseEvent) => {
    if (isRenaming) return;
    e.stopPropagation();
    
    if (node.type === 'directory') {
      setIsExpanded(!isExpanded);
      onSelectPath(isSelected ? null : node.path);
    } else {
      onFileSelect(node.path);
      onSelectPath(node.path);
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
      if (isSelected) onSelectPath(null);
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

  const handleDragStart = (e: React.DragEvent) => {
    e.stopPropagation();
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', node.path);
    onDragStart?.(node.path, node.type as 'file' | 'directory');
  };

  const handleDragEnd = (e: React.DragEvent) => {
    e.stopPropagation();
    onDragEnd?.();
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (node.type === 'directory') {
      e.dataTransfer.dropEffect = 'move';
      onDragOver?.(node.path);
    }
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    onDragLeave?.();
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (node.type === 'directory') {
      onDrop?.(node.path);
    }
  };

  const handleCreateConfirm = (itemName: string) => {
    const prefix = node.path === '.' ? '' : node.path + '/';
    onCreated(prefix + itemName);
  };

  return (
    <div>
      <div
        className={`flex items-center px-2 py-1 cursor-pointer hover:bg-gray-100 group transition-colors ${
          isSelected ? 'bg-blue-50 text-blue-700' : ''
        } ${isDragTarget ? 'bg-green-100 ring-1 ring-green-400' : ''}`}
        style={{ paddingLeft: `${level * 16 + 8}px` }}
        onClick={handleClick}
        draggable={!editMode && !isRenaming}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {node.type === 'directory' ? (
          <>
            {isExpanded ? (
              <ChevronDown className="w-4 h-4 mr-1 flex-shrink-0" />
            ) : (
              <ChevronRight className="w-4 h-4 mr-1 flex-shrink-0" />
            )}
            <Folder className="w-4 h-4 mr-2 text-blue-500 flex-shrink-0" />
          </>
        ) : (
          <>
            <span className="w-4 mr-1 flex-shrink-0" />
            <File className="w-4 h-4 mr-2 text-gray-500 flex-shrink-0" />
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
      {node.type === 'directory' && isExpanded && (
        <div>
          {node.children?.map(child => (
            <FileTreeNode
              key={child.path}
              node={child}
              level={level + 1}
              onFileSelect={onFileSelect}
              onAddToChat={onAddToChat}
              selectedPath={selectedPath}
              onSelectPath={onSelectPath}
              editMode={editMode}
              cwd={cwd}
              onRefresh={onRefresh}
              onDragStart={onDragStart}
              onDragEnd={onDragEnd}
              dragTarget={dragTarget}
              onDragOver={onDragOver}
              onDragLeave={onDragLeave}
              onDrop={onDrop}
              creatingInPath={creatingInPath}
              creatingType={creatingType}
              onCreated={onCreated}
            />
          ))}
          {isCreatingHere && creatingType && (
            <InlineCreateInput
              type={creatingType}
              level={level + 1}
              onCreated={handleCreateConfirm}
              onCancel={() => onCreated('')}
            />
          )}
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

const fileTreeCache = new Map<string, WorkspaceNode>();

export function FileTree({ onFileSelect, onAddToChat, selectedFile, cwd, visible, refreshTrigger }: FileTreeProps) {
  const [tree, setTree] = useState<WorkspaceNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [editMode, setEditMode] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(selectedFile);
  const [dragSource, setDragSource] = useState<{ path: string; type: 'file' | 'directory' } | null>(null);
  const [dragTarget, setDragTarget] = useState<string | null>(null);
  const [creatingInPath, setCreatingInPath] = useState<string | null>(null);
  const [creatingType, setCreatingType] = useState<'file' | 'folder' | null>(null);
  const cacheKey = cwd || 'default';

  const loadTree = useCallback((forceRefresh = false) => {
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
  }, [cacheKey, cwd]);

  useEffect(() => {
    loadTree();
  }, [loadTree]);

  useEffect(() => {
    if (visible) {
      loadTree(true);
    }
  }, [visible, loadTree]);

  useEffect(() => {
    if (refreshTrigger && refreshTrigger > 0) {
      loadTree(true);
    }
  }, [refreshTrigger, loadTree]);

  useEffect(() => {
    setSelectedPath(selectedFile);
  }, [selectedFile]);

  const findNode = (root: WorkspaceNode | null, path: string): WorkspaceNode | null => {
    if (!root) return null;
    if (root.path === path) return root;
    if (root.children) {
      for (const child of root.children) {
        const found = findNode(child, path);
        if (found) return found;
      }
    }
    return null;
  };

  const getTargetDir = (): string => {
    if (!selectedPath) return tree?.path || '.';
    const node = findNode(tree, selectedPath);
    if (node?.type === 'directory') return node.path;
    return selectedPath.split('/').slice(0, -1).join('/') || tree?.path || '.';
  };

  const startCreate = (type: 'file' | 'folder') => {
    const dir = getTargetDir();
    setCreatingInPath(dir);
    setCreatingType(type);
    const node = findNode(tree, dir);
    if (node?.type === 'directory') {
      // auto-expand handled below
    }
  };

  const handleCreated = async (fullPath: string) => {
    setCreatingInPath(null);
    setCreatingType(null);
    if (!fullPath) return;
    try {
      await createWorkspaceFile(fullPath, cwd || undefined);
      loadTree(true);
    } catch (err) {
      alert(err instanceof Error ? err.message : '创建失败');
    }
  };

  const handleDragStart = (path: string, type: 'file' | 'directory') => {
    setDragSource({ path, type });
  };

  const handleDragEnd = () => {
    setDragSource(null);
    setDragTarget(null);
  };

  const handleDragOver = (path: string) => {
    if (dragSource && dragSource.path !== path) {
      setDragTarget(path);
    }
  };

  const handleDragLeave = () => {
    setDragTarget(null);
  };

  const handleDrop = async (targetPath: string) => {
    if (!dragSource) return;
    
    const sourcePath = dragSource.path;
    if (sourcePath === targetPath) {
      handleDragEnd();
      return;
    }

    const sourceName = sourcePath.split('/').pop() || '';
    const targetFullPath = targetPath === '.' ? sourceName : `${targetPath}/${sourceName}`;

    try {
      await moveWorkspaceFile(sourcePath, targetFullPath, cwd || undefined);
      loadTree(true);
    } catch (err) {
      alert(err instanceof Error ? err.message : '移动失败');
    }
    
    handleDragEnd();
  };

  const handleRootCreateConfirm = (name: string) => {
    handleCreated(name);
  };

  if (loading && !tree) {
    return <div className="p-4 text-sm text-gray-500">Loading...</div>;
  }

  if (!tree) {
    return <div className="p-4 text-sm text-red-500">Failed to load</div>;
  }

  const isCreatingAtRoot = creatingInPath === tree.path;

  return (
    <div className="h-full overflow-y-auto flex flex-col">
      <div className="p-3 border-b border-gray-200 bg-gray-50 flex items-center justify-between">
        <div className="flex items-center text-sm font-medium text-gray-700">
          <FolderTree className="w-4 h-4 mr-2" />
          Workspace
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => startCreate('file')}
            className={`p-1 rounded transition-colors ${
              creatingInPath && creatingType === 'file'
                ? 'bg-green-100 text-green-700 hover:bg-green-200' 
                : 'hover:bg-gray-200 text-gray-600'
            }`}
            title="新建文件"
          >
            <Plus className="w-4 h-4" />
          </button>
          <button
            onClick={() => startCreate('folder')}
            className={`p-1 rounded transition-colors ${
              creatingInPath && creatingType === 'folder'
                ? 'bg-purple-100 text-purple-700 hover:bg-purple-200' 
                : 'hover:bg-gray-200 text-gray-600'
            }`}
            title="新建文件夹"
          >
            <FolderPlus className="w-4 h-4" />
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
      <div className="py-2 flex-1">
        {tree.children?.map(child => (
          <FileTreeNode
            key={child.path}
            node={child}
            level={0}
            onFileSelect={onFileSelect}
            onAddToChat={onAddToChat}
            selectedPath={selectedPath}
            onSelectPath={setSelectedPath}
            editMode={editMode}
            cwd={cwd}
            onRefresh={() => loadTree(true)}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
            dragTarget={dragTarget}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            creatingInPath={creatingInPath}
            creatingType={creatingType}
            onCreated={handleCreated}
          />
        ))}
        {isCreatingAtRoot && creatingType && (
          <InlineCreateInput
            type={creatingType}
            level={0}
            onCreated={handleRootCreateConfirm}
            onCancel={() => { setCreatingInPath(null); setCreatingType(null); }}
          />
        )}
      </div>
    </div>
  );
}

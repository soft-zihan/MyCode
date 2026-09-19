import { useState, useEffect, useCallback } from 'react';
import { 
  FolderTree, RefreshCw, FileText, BookOpen, Settings, Zap, Bot, 
  MessageSquare, Lightbulb, User
} from 'lucide-react';
import { fetchWorkspaceTree, WorkspaceNode } from '../../api/client';

interface WikiPanelProps {
  cwd: string | null;
  onFileSelect: (path: string) => void;
  selectedFile: string | null;
}

const WIKI_CATEGORIES = [
  {
    name: 'Knowledge',
    icon: BookOpen,
    color: 'text-blue-500',
    bgColor: 'bg-blue-50',
    path: '.mycode/wiki/knowledge',
    description: '项目知识',
  },
  {
    name: 'Lessons',
    icon: Lightbulb,
    color: 'text-yellow-500',
    bgColor: 'bg-yellow-50',
    path: '.mycode/wiki/self_improvement',
    description: '经验教训',
  },
  {
    name: 'User',
    icon: User,
    color: 'text-purple-500',
    bgColor: 'bg-purple-50',
    path: '.mycode/wiki/user',
    description: '用户偏好',
  },
  {
    name: 'Patterns',
    icon: Zap,
    color: 'text-orange-500',
    bgColor: 'bg-orange-50',
    path: '.mycode/wiki/workflow_pattern',
    description: '工作流模式',
  },
  {
    name: 'Session Notes',
    icon: MessageSquare,
    color: 'text-cyan-500',
    bgColor: 'bg-cyan-50',
    path: '.mycode/wiki/task_notes',
    description: '会话笔记',
  },
];

const CONFIG_CATEGORIES = [
  {
    name: 'Rules',
    icon: Settings,
    color: 'text-red-500',
    bgColor: 'bg-red-50',
    path: '.mycode/RULES.md',
    description: '项目规则',
  },
  {
    name: 'Skills',
    icon: Zap,
    color: 'text-purple-500',
    bgColor: 'bg-purple-50',
    path: '.mycode/skills',
    description: '技能定义',
  },
  {
    name: 'Agents',
    icon: Bot,
    color: 'text-orange-500',
    bgColor: 'bg-orange-50',
    path: '.mycode/agents',
    description: '子代理配置',
  },
];

interface FileNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  children?: FileNode[];
  size?: number;
}

function filterTreeByPath(tree: WorkspaceNode | null, targetPath: string): FileNode[] {
  if (!tree || !tree.children) return [];
  
  const result: FileNode[] = [];
  
  for (const child of tree.children) {
    // 处理 .mycode 下的直接子节点
    const childPath = child.path.startsWith('.mycode/') ? child.path : `.mycode/${child.path}`;
    
    if (childPath === targetPath || childPath.startsWith(targetPath + '/')) {
      result.push(convertToFileNode(child));
    }
  }
  
  return result;
}

function convertToFileNode(node: WorkspaceNode): FileNode {
  return {
    name: node.name,
    path: node.path,
    type: node.type as 'file' | 'directory',
    size: node.size,
    children: node.children?.map(convertToFileNode),
  };
}

function TreeNode({ node, level, onFileSelect, selectedPath }: {
  node: FileNode;
  level: number;
  onFileSelect: (path: string) => void;
  selectedPath: string | null;
}) {
  const [expanded, setExpanded] = useState(level < 2);
  const isSelected = selectedPath === node.path;
  
  const handleClick = () => {
    if (node.type === 'directory') {
      setExpanded(!expanded);
    } else {
      onFileSelect(node.path);
    }
  };
  
  return (
    <div>
      <div
        className={`flex items-center gap-1.5 px-2 py-1 cursor-pointer text-xs hover:bg-gray-100 transition-colors ${
          isSelected ? 'bg-blue-50 text-blue-700 font-medium' : 'text-gray-700'
        }`}
        style={{ paddingLeft: `${level * 12 + 8}px` }}
        onClick={handleClick}
      >
        {node.type === 'directory' ? (
          <>
            <span className="text-gray-400 w-3 text-[10px]">{expanded ? '▼' : '▶'}</span>
            <FolderTree className="w-3.5 h-3.5 text-gray-500" />
          </>
        ) : (
          <>
            <span className="w-3" />
            <FileText className="w-3.5 h-3.5 text-blue-400" />
          </>
        )}
        <span className="truncate flex-1">{node.name}</span>
        {node.type === 'file' && node.size && (
          <span className="text-[10px] text-gray-400">{formatSize(node.size)}</span>
        )}
      </div>
      {node.type === 'directory' && expanded && node.children && (
        <div>
          {node.children
            .sort((a, b) => {
              if (a.type === b.type) return a.name.localeCompare(b.name);
              return a.type === 'directory' ? -1 : 1;
            })
            .map(child => (
              <TreeNode
                key={child.path}
                node={child}
                level={level + 1}
                onFileSelect={onFileSelect}
                selectedPath={selectedPath}
              />
            ))}
        </div>
      )}
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

function CategorySection({ 
  category, 
  files, 
  expanded, 
  onToggle, 
  onFileSelect, 
  selectedFile 
}: {
  category: typeof WIKI_CATEGORIES[0];
  files: FileNode[];
  expanded: boolean;
  onToggle: () => void;
  onFileSelect: (path: string) => void;
  selectedFile: string | null;
}) {
  const Icon = category.icon;
  
  if (files.length === 0) return null;
  
  return (
    <div className="border-b border-gray-100">
      <div
        className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-gray-50 transition-colors"
        onClick={onToggle}
      >
        <span className="text-gray-400 text-[10px] w-3">{expanded ? '▼' : '▶'}</span>
        <Icon className={`w-4 h-4 ${category.color}`} />
        <span className="text-sm font-medium text-gray-700 flex-1">{category.name}</span>
        <span className="text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">
          {files.length}
        </span>
      </div>
      
      {expanded && (
        <div className="pb-1">
          {files
            .sort((a, b) => {
              if (a.type === b.type) return a.name.localeCompare(b.name);
              return a.type === 'directory' ? -1 : 1;
            })
            .map(file => (
              <TreeNode
                key={file.path}
                node={file}
                level={0}
                onFileSelect={onFileSelect}
                selectedPath={selectedFile}
              />
            ))}
        </div>
      )}
    </div>
  );
}

export function WikiPanel({ cwd, onFileSelect, selectedFile }: WikiPanelProps) {
  const [tree, setTree] = useState<WorkspaceNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(
    new Set(['Plans', 'Knowledge', 'Rules'])
  );
  
  const fetchTree = useCallback(async () => {
    if (!cwd) return;
    setLoading(true);
    try {
      const result = await fetchWorkspaceTree(cwd);
      setTree(result);
    } catch (err) {
      console.error('Failed to fetch wiki tree:', err);
    } finally {
      setLoading(false);
    }
  }, [cwd]);
  
  useEffect(() => {
    fetchTree();
  }, [fetchTree]);
  
  const toggleCategory = (name: string) => {
    setExpandedCategories(prev => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  };
  
  if (!cwd) {
    return <div className="p-4 text-sm text-gray-500">No workspace</div>;
  }
  
  if (loading) {
    return (
      <div className="p-4 flex items-center justify-center">
        <RefreshCw className="w-4 h-4 animate-spin text-gray-400" />
      </div>
    );
  }
  
  // 计算各类别文件数
  const wikiFiles = WIKI_CATEGORIES.map(cat => ({
    category: cat,
    files: filterTreeByPath(tree, cat.path),
  }));
  
  const configFiles = CONFIG_CATEGORIES.map(cat => ({
    category: cat,
    files: filterTreeByPath(tree, cat.path),
  }));
  
  const totalWikiFiles = wikiFiles.reduce((sum, w) => sum + w.files.length, 0);
  const totalConfigFiles = configFiles.reduce((sum, c) => sum + c.files.length, 0);
  
  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200 bg-gradient-to-r from-blue-50 to-purple-50">
        <div className="flex items-center gap-2">
          <BookOpen className="w-4 h-4 text-blue-500" />
          <span className="text-sm font-semibold text-gray-700">Wiki</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-gray-500">
            {totalWikiFiles} docs · {totalConfigFiles} configs
          </span>
          <button
            onClick={fetchTree}
            className="p-1 hover:bg-white/50 rounded transition-colors"
            title="刷新"
          >
            <RefreshCw className="w-3.5 h-3.5 text-gray-500" />
          </button>
        </div>
      </div>
      
      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {/* 用户文档 - Agent 产出的知识 */}
        {totalWikiFiles > 0 && (
          <div className="border-b border-gray-200">
            <div className="px-3 py-1.5 bg-gray-50 text-[10px] font-medium text-gray-500 uppercase tracking-wide">
              用户文档
            </div>
            {wikiFiles.map(({ category, files }) => (
              <CategorySection
                key={category.name}
                category={category}
                files={files}
                expanded={expandedCategories.has(category.name)}
                onToggle={() => toggleCategory(category.name)}
                onFileSelect={onFileSelect}
                selectedFile={selectedFile}
              />
            ))}
          </div>
        )}
        
        {/* 系统配置 - 项目维护的配置文件 */}
        {totalConfigFiles > 0 && (
          <div>
            <div className="px-3 py-1.5 bg-gray-50 text-[10px] font-medium text-gray-500 uppercase tracking-wide">
              系统配置
            </div>
            {configFiles.map(({ category, files }) => (
              <CategorySection
                key={category.name}
                category={category}
                files={files}
                expanded={expandedCategories.has(category.name)}
                onToggle={() => toggleCategory(category.name)}
                onFileSelect={onFileSelect}
                selectedFile={selectedFile}
              />
            ))}
          </div>
        )}
        
        {/* 空状态 */}
        {totalWikiFiles === 0 && totalConfigFiles === 0 && (
          <div className="p-8 text-center text-sm text-gray-400">
            <BookOpen className="w-8 h-8 mx-auto mb-2 opacity-30" />
            <div>暂无文档</div>
            <div className="text-xs mt-1">与 Agent 对话以创建文档</div>
          </div>
        )}
      </div>
    </div>
  );
}

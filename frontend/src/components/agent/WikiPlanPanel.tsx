import { useState, useEffect } from 'react';
import { BookOpen, Target, Settings, FileText, FolderOpen, ChevronDown, ChevronRight } from 'lucide-react';
import { WikiPanel } from './WikiPanel';
import { PlanControlPanel } from './PlanControlPanel';
import { getPlanArtifacts, fetchWorkspaceTree, WorkspaceNode } from '../../api/client';

interface WikiPlanPanelProps {
  cwd: string | null;
  sessionId: string;
  planSlug: string;
  onFileSelect: (path: string) => void;
  selectedFile: string | null;
}

type SubTab = 'docs' | 'plan-config' | 'plan-artifacts';

interface PlanFile {
  name: string;
  path: string;
  size: number;
}

function PlanArtifactsPanel({ sessionId, planSlug, cwd, onFileSelect }: {
  sessionId: string;
  planSlug: string;
  cwd: string | null;
  onFileSelect: (path: string) => void;
}) {
  const [artifacts, setArtifacts] = useState<Record<string, string>>({});
  const [planFiles, setPlanFiles] = useState<PlanFile[]>([]);
  const [expandedArtifact, setExpandedArtifact] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!planSlug || !sessionId) return;
    setLoading(true);
    
    // Try API first
    getPlanArtifacts(sessionId, planSlug)
      .then(res => {
        if (res.success && res.data) {
          setArtifacts(res.data);
        } else {
          // Fallback: scan workspace tree for plan files
          if (cwd) {
            fetchWorkspaceTree(cwd)
              .then(tree => {
                const files = findPlanFiles(tree, planSlug);
                setPlanFiles(files);
              })
              .catch(() => {});
          }
        }
      })
      .catch(() => {
        // Fallback: scan workspace tree for plan files
        if (cwd) {
          fetchWorkspaceTree(cwd)
            .then(tree => {
              const files = findPlanFiles(tree, planSlug);
              setPlanFiles(files);
            })
            .catch(() => {});
        }
      })
      .finally(() => setLoading(false));
  }, [sessionId, planSlug, cwd]);

  if (!planSlug) {
    return (
      <div className="p-6 text-center text-sm text-gray-400">
        <FolderOpen className="w-8 h-8 mx-auto mb-2 opacity-30" />
        <div>暂无活跃 Plan</div>
        <div className="text-xs mt-1">使用 Plan 模式创建计划</div>
      </div>
    );
  }

  if (loading) {
    return <div className="p-4 text-sm text-gray-500">加载中...</div>;
  }

  const artifactEntries = Object.entries(artifacts);

  if (artifactEntries.length === 0 && planFiles.length === 0) {
    return (
      <div className="p-6 text-center text-sm text-gray-400">
        <FileText className="w-8 h-8 mx-auto mb-2 opacity-30" />
        <div>Plan 暂无产物</div>
        <div className="text-xs mt-1">Plan: {planSlug}</div>
      </div>
    );
  }

  return (
    <div className="p-2 space-y-1">
      <div className="px-2 py-1.5 text-xs font-medium text-gray-700 flex items-center gap-1.5">
        <Target className="w-3.5 h-3.5 text-green-500" />
        <span>{planSlug}</span>
        <span className="text-[10px] text-gray-400 ml-auto">
          {artifactEntries.length + planFiles.length} 个文件
        </span>
      </div>
      
      {artifactEntries.map(([filename, content]) => {
        const wikiPath = `.mycode/plans/${planSlug}/${filename}`;
        const isExpanded = expandedArtifact === filename;
        return (
          <div key={filename} className="border border-gray-200 rounded">
            <div className="flex items-center">
              <button
                onClick={() => setExpandedArtifact(isExpanded ? null : filename)}
                className="flex-1 flex items-center gap-2 px-2 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
              >
                {isExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                <FileText className="w-3 h-3 text-blue-500" />
                {filename}
                <span className="text-gray-400 ml-auto text-[10px]">{content.length} chars</span>
              </button>
              <button
                onClick={() => onFileSelect(wikiPath)}
                className="px-2 py-1.5 text-[10px] text-blue-600 hover:bg-blue-50 border-l border-gray-200"
                title="在编辑器中打开"
              >
                打开
              </button>
            </div>
            {isExpanded && (
              <div className="border-t border-gray-200 p-2 max-h-48 overflow-auto">
                <pre className="text-xs text-gray-600 whitespace-pre-wrap font-mono">{content}</pre>
              </div>
            )}
          </div>
        );
      })}
      
      {planFiles.map(file => {
        return (
          <div key={file.path} className="border border-gray-200 rounded">
            <div className="flex items-center">
              <button
                onClick={() => onFileSelect(file.path)}
                className="flex-1 flex items-center gap-2 px-2 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
              >
                <FileText className="w-3 h-3 text-blue-500" />
                {file.name}
                <span className="text-gray-400 ml-auto text-[10px]">{file.size}B</span>
              </button>
              <button
                onClick={() => onFileSelect(file.path)}
                className="px-2 py-1.5 text-[10px] text-blue-600 hover:bg-blue-50 border-l border-gray-200"
                title="在编辑器中打开"
              >
                打开
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function findPlanFiles(tree: WorkspaceNode, planSlug: string): PlanFile[] {
  const files: PlanFile[] = [];
  const planDirPath = `.mycode/plans/${planSlug}`;
  
  function walk(node: WorkspaceNode, currentPath: string) {
    const nodePath = currentPath ? `${currentPath}/${node.name}` : node.name;
    
    if (node.type === 'file' && nodePath.startsWith(planDirPath)) {
      files.push({
        name: node.name,
        path: nodePath,
        size: node.size || 0,
      });
    } else if (node.type === 'directory' && node.children) {
      for (const child of node.children) {
        walk(child, nodePath);
      }
    }
  }
  
  walk(tree, '');
  return files;
}

export function WikiPlanPanel({ cwd, sessionId, planSlug, onFileSelect, selectedFile }: WikiPlanPanelProps) {
  const [subTab, setSubTab] = useState<SubTab>('docs');

  // Fallback: derive plan slug from session ID if not provided
  const effectivePlanSlug = planSlug || (sessionId ? `plan-${sessionId}` : '');

  return (
    <div className="flex flex-col h-full">
      <div className="flex border-b border-gray-200 bg-gray-50">
        <button
          onClick={() => setSubTab('docs')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'docs' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <BookOpen className="w-3 h-3 inline mr-1" />
          文档
        </button>
        <button
          onClick={() => setSubTab('plan-config')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'plan-config' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <Settings className="w-3 h-3 inline mr-1" />
          Plan 配置
        </button>
        <button
          onClick={() => setSubTab('plan-artifacts')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'plan-artifacts' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <FileText className="w-3 h-3 inline mr-1" />
          Plan 产物
        </button>
      </div>

      <div className="flex-1 overflow-auto">
        {subTab === 'docs' && (
          <WikiPanel cwd={cwd} onFileSelect={onFileSelect} selectedFile={selectedFile} />
        )}
        {subTab === 'plan-config' && (
          <PlanControlPanel sessionId={sessionId} planSlug="" />
        )}
        {subTab === 'plan-artifacts' && (
          <PlanArtifactsPanel sessionId={sessionId} planSlug={effectivePlanSlug} cwd={cwd} onFileSelect={onFileSelect} />
        )}
      </div>
    </div>
  );
}

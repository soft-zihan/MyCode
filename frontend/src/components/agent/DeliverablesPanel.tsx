import { useState } from 'react';
import { FileText, FilePlus, ChevronRight, ChevronDown, Folder } from 'lucide-react';

export interface FileSnapshot {
  file_path: string;
  is_new: boolean;
  old_content?: string;
  new_content: string;
}

interface DeliverablesPanelProps {
  snapshots: FileSnapshot[];
  onOpenFile: (filePath: string) => void;
}

interface TreeNode {
  name: string;
  path: string;
  isFile: boolean;
  isNew: boolean;
  children: TreeNode[];
}

function buildTree(snapshots: FileSnapshot[]): TreeNode[] {
  const root: TreeNode[] = [];

  for (const snap of snapshots) {
    const parts = snap.file_path.split('/').filter(Boolean);
    let current = root;

    for (let i = 0; i < parts.length; i++) {
      const name = parts[i];
      const path = parts.slice(0, i + 1).join('/');
      const isFile = i === parts.length - 1;

      let existing = current.find(n => n.path === path);
      if (!existing) {
        existing = {
          name,
          path,
          isFile,
          isNew: isFile ? snap.is_new : false,
          children: [],
        };
        current.push(existing);
      }

      if (!isFile) {
        current = existing.children;
      }
    }
  }

  const sortTree = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      if (a.isFile !== b.isFile) return a.isFile ? 1 : -1;
      return a.name.localeCompare(b.name);
    });
    nodes.forEach(n => sortTree(n.children));
  };
  sortTree(root);

  return root;
}

function TreeNodeItem({ node, level, onOpenFile }: {
  node: TreeNode;
  level: number;
  onOpenFile: (path: string) => void;
}) {
  const [isExpanded, setIsExpanded] = useState(true);

  const handleClick = () => {
    if (node.isFile) {
      onOpenFile(node.path);
    } else {
      setIsExpanded(!isExpanded);
    }
  };

  return (
    <>
      <div
        className="flex items-center gap-1 px-2 py-1 hover:bg-gray-100 cursor-pointer text-sm"
        style={{ paddingLeft: `${level * 12 + 8}px` }}
        onClick={handleClick}
      >
        {node.isFile ? (
          <>
            {node.isNew ? (
              <FilePlus className="w-4 h-4 text-green-500 flex-shrink-0" />
            ) : (
              <FileText className="w-4 h-4 text-blue-500 flex-shrink-0" />
            )}
            <span className="truncate flex-1">{node.name}</span>
            {node.isNew && (
              <span className="text-[9px] px-1 py-0.5 bg-green-100 text-green-700 rounded">NEW</span>
            )}
          </>
        ) : (
          <>
            {isExpanded ? (
              <ChevronDown className="w-3 h-3 text-gray-400 flex-shrink-0" />
            ) : (
              <ChevronRight className="w-3 h-3 text-gray-400 flex-shrink-0" />
            )}
            <Folder className="w-4 h-4 text-yellow-500 flex-shrink-0" />
            <span className="truncate flex-1">{node.name}</span>
          </>
        )}
      </div>
      {!node.isFile && isExpanded && node.children.map(child => (
        <TreeNodeItem key={child.path} node={child} level={level + 1} onOpenFile={onOpenFile} />
      ))}
    </>
  );
}

export function DeliverablesPanel({ snapshots, onOpenFile }: DeliverablesPanelProps) {
  if (snapshots.length === 0) {
    return (
      <div className="p-4 text-center text-gray-400 text-sm">
        <FileText className="w-8 h-8 mx-auto mb-2 opacity-50" />
        <p>No file changes yet</p>
        <p className="text-xs mt-1">File modifications will appear here</p>
      </div>
    );
  }

  const tree = buildTree(snapshots);

  return (
    <div className="h-full overflow-y-auto">
      {tree.map(node => (
        <TreeNodeItem key={node.path} node={node} level={0} onOpenFile={onOpenFile} />
      ))}
    </div>
  );
}

export default DeliverablesPanel;

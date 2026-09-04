import { memo, useState, useRef, useEffect } from 'react';
import { File, RotateCcw, ChevronDown } from 'lucide-react';
import type { UserNode } from './types';

interface UserNodeViewProps {
  node: UserNode;
  onEdit?: (node: UserNode, restoreFiles: boolean) => void;
}

export const UserNodeView = memo(function UserNodeView({ node, onEdit }: UserNodeViewProps) {
  const [showMenu, setShowMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setShowMenu(false);
      }
    };
    if (showMenu) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showMenu]);

  return (
    <div className="flex justify-end group">
      <div className="max-w-2xl">
        {node.contextFiles && node.contextFiles.length > 0 && (
          <div className="flex items-center gap-1 flex-wrap mb-1 px-2">
            <span className="text-xs text-gray-500">Context:</span>
            {node.contextFiles.map(file => (
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
        <div className="rounded-lg px-4 py-3 bg-blue-500 text-white">
          <pre className="whitespace-pre-wrap text-sm font-sans">{node.content}</pre>
          <div className="text-xs mt-1 flex items-center gap-2 text-blue-100">
            <span>{new Date(node.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span>
            {node.agent && <span>• Agent: {node.agent}</span>}
            {node.model && <span>• Model: {node.model}</span>}
          </div>
        </div>
        {onEdit && (
          <div className="relative opacity-0 group-hover:opacity-100 transition-opacity" ref={menuRef}>
            <button
              onClick={() => setShowMenu(!showMenu)}
              className="mt-1 p-1 text-blue-600 hover:bg-blue-50 rounded flex items-center gap-0.5"
              title="回退到此消息"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              <ChevronDown className="w-3 h-3" />
            </button>
            {showMenu && (
              <div className="fixed left-auto top-auto mt-1 bg-white border border-gray-200 rounded shadow-lg z-[9999] min-w-[160px]" style={{ position: 'absolute' }}>
                <button
                  onClick={() => { onEdit(node, true); setShowMenu(false); }}
                  className="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-100 flex items-center gap-2"
                >
                  <RotateCcw className="w-3.5 h-3.5 text-blue-500" />
                  回退并恢复文件
                </button>
                <button
                  onClick={() => { onEdit(node, false); setShowMenu(false); }}
                  className="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-100 flex items-center gap-2"
                >
                  <RotateCcw className="w-3.5 h-3.5 text-gray-400" />
                  仅截断对话
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
});

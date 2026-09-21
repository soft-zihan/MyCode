import { memo, useState, useRef, useEffect } from 'react';
import { File, RotateCcw, ChevronDown, Pencil } from 'lucide-react';
import type { UserNode } from './types';

interface UserNodeViewProps {
  node: UserNode;
  sessionId?: string;
  /** 该消息在用户消息序列中的索引（回退锚点：keep_user_messages） */
  userMessageIndex?: number;
  onEdit?: (node: UserNode) => void;
  onRewind?: (userMessageIndex: number) => void;
}

export const UserNodeView = memo(function UserNodeView({ node, sessionId, userMessageIndex, onEdit, onRewind }: UserNodeViewProps) {
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
      <div className="max-w-[85%] min-w-0">
        {node.contextFiles && node.contextFiles.length > 0 && (
          <div className="flex items-center gap-1 flex-wrap mb-1 px-2">
            <span className="text-[11px] text-gray-400">Context:</span>
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
        <div className="rounded-2xl rounded-br-md px-4 py-2.5 bg-gray-100 text-gray-900">
          <div className="whitespace-pre-wrap break-words text-sm font-sans leading-relaxed">{node.content}</div>
        </div>
        <div className="hidden group-hover:flex text-[11px] mt-1 px-1 items-center gap-2 text-gray-400 justify-end">
          <span>{new Date(node.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span>
          {node.model && <span>· {node.model}</span>}
        </div>
        {(onEdit || onRewind) && (
          <div className="relative hidden group-hover:block" ref={menuRef}>
            <button
              onClick={() => setShowMenu(!showMenu)}
              className="mt-0.5 p-1 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded flex items-center gap-0.5"
              title="回退到此消息"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              <ChevronDown className="w-3 h-3" />
            </button>
            {showMenu && (
              <div className="fixed left-auto top-auto mt-1 bg-white border border-gray-200 rounded shadow-lg z-[9999] min-w-[180px]" style={{ position: 'absolute' }}>
                {onRewind && sessionId && userMessageIndex !== undefined && (
                  <button
                    onClick={() => { setShowMenu(false); onRewind(userMessageIndex); }}
                    className="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-100 flex items-center gap-2"
                  >
                    <RotateCcw className="w-3.5 h-3.5 text-indigo-500" />
                    回退到此消息（含文件）
                  </button>
                )}
                {onEdit && (
                  <button
                    onClick={() => { setShowMenu(false); onEdit(node); }}
                    className="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-100 flex items-center gap-2"
                  >
                    <Pencil className="w-3.5 h-3.5 text-gray-400" />
                    编辑并重发
                  </button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
});

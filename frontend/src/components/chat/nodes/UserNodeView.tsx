import { memo } from 'react';
import { File, RotateCcw } from 'lucide-react';
import type { UserNode } from './types';

interface UserNodeViewProps {
  node: UserNode;
  onEdit?: (index: number) => void;
  index?: number;
}

export const UserNodeView = memo(function UserNodeView({ node, onEdit, index }: UserNodeViewProps) {
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
          <button
            onClick={() => onEdit(index || 0)}
            className="opacity-0 group-hover:opacity-100 mt-1 p-1 text-blue-600 hover:bg-blue-50 rounded transition-opacity"
            title="回退到此消息并重新发送"
          >
            <RotateCcw className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  );
});

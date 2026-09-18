import React, { useState } from 'react';
import { Eye, Edit3, FileText, ChevronRight, ChevronDown, CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import type { ToolCallEvent } from '../../../hooks';

interface FileCardProps {
  call: ToolCallEvent;
}

const filePath = (call: ToolCallEvent): string => {
  return (call.input.file_path as string) || '';
};

const fileName = (path: string): string => {
  return path.split('/').pop() || path;
};

const fileDir = (path: string): string => {
  const parts = path.split('/');
  if (parts.length <= 1) return '';
  return parts.slice(0, -1).join('/');
};

const getIcon = (name: string) => {
  if (name.includes('read')) return Eye;
  if (name.includes('write') || name.includes('edit')) return Edit3;
  return FileText;
};

export const FileCard: React.FC<FileCardProps> = ({ call }) => {
  const [expanded, setExpanded] = useState(false);
  const path = filePath(call);
  const name = fileName(path);
  const dir = fileDir(path);
  const Icon = getIcon(call.name);
  const isRunning = call.status === 'pending';
  const isError = call.status === 'error';
  const isSuccess = call.status === 'success';
  const isEdit = call.name.includes('edit') || call.name.includes('write');

  return (
    <div className={`tool-card tool-card-file ${isRunning ? 'running' : ''} ${isError ? 'error' : ''}`}>
      <div
        className="tool-card-header"
        onClick={() => call.result && setExpanded(!expanded)}
      >
        <div className={`tool-card-icon ${isEdit ? 'edit' : 'read'}`}>
          <Icon className="w-3.5 h-3.5" />
        </div>
        
        <div className="tool-card-content">
          <span className="tool-card-file-name">{name}</span>
          {dir && <span className="tool-card-file-dir">{dir}</span>}
        </div>

        <div className="tool-card-status">
          {isRunning && <Loader2 className="w-3.5 h-3.5 text-blue-500 animate-spin" />}
          {isSuccess && <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />}
          {isError && <XCircle className="w-3.5 h-3.5 text-red-500" />}
          {call.result && (
            expanded ? <ChevronDown className="w-3.5 h-3.5 text-gray-400" /> : <ChevronRight className="w-3.5 h-3.5 text-gray-400" />
          )}
        </div>
      </div>

      {expanded && call.result && (
        <div className="tool-card-body">
          {isEdit ? (
            <div className="tool-card-diff">
              {call.result.split('\n').map((line, i) => {
                let className = 'tool-card-diff-line';
                if (line.startsWith('@@')) className += ' hunk';
                else if (line.startsWith('- ')) className += ' removed';
                else if (line.startsWith('+ ')) className += ' added';
                return <div key={i} className={className}>{line || ' '}</div>;
              })}
            </div>
          ) : (
            <pre className="tool-card-output">{call.result.slice(0, 3000)}</pre>
          )}
        </div>
      )}

      {call.snapshot && (
        <div className="tool-card-snapshot">
          <span className="tool-card-snapshot-badge">
            {call.snapshot.old_content ? 'Modified' : 'Created'}
          </span>
        </div>
      )}
    </div>
  );
};

import { useState } from 'react';
import { DiffViewer } from './DiffViewer';

export interface FileSnapshot {
  file_path: string;
  is_new: boolean;
  old_content?: string;
  new_content: string;
}

interface ReviewPanelProps {
  snapshots: FileSnapshot[];
  onAccept: (filePath: string) => void;
  onReject: (filePath: string) => void;
  onAcceptAll: () => void;
  onOpenFile: (filePath: string) => void;
}

export function ReviewPanel({ snapshots, onAccept, onReject, onAcceptAll, onOpenFile }: ReviewPanelProps) {
  const [expandedFile, setExpandedFile] = useState<string | null>(null);
  const [isCollapsed, setIsCollapsed] = useState(false);

  if (snapshots.length === 0) return null;

  const newFiles = snapshots.filter(s => s.is_new);
  const editedFiles = snapshots.filter(s => !s.is_new);

  return (
    <div className="border-t border-gray-200 bg-white">
      {/* Header */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => setIsCollapsed(!isCollapsed)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setIsCollapsed(!isCollapsed); } }}
        className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-gray-50 transition-colors cursor-pointer"
      >
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-700">
            Code Review
          </span>
          <span className="text-xs bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded-full">
            {snapshots.length} file{snapshots.length !== 1 ? 's' : ''}
          </span>
          {newFiles.length > 0 && (
            <span className="text-xs bg-green-100 text-green-700 px-1.5 py-0.5 rounded-full">
              +{newFiles.length} new
            </span>
          )}
          {editedFiles.length > 0 && (
            <span className="text-xs bg-yellow-100 text-yellow-700 px-1.5 py-0.5 rounded-full">
              ~{editedFiles.length} edited
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {!isCollapsed && (
            <button
              onClick={(e) => { e.stopPropagation(); onAcceptAll(); }}
              className="text-xs bg-green-500 text-white px-2.5 py-1 rounded hover:bg-green-600 transition-colors"
            >
              Accept All
            </button>
          )}
          <span className={`text-gray-400 transition-transform ${isCollapsed ? '' : 'rotate-180'}`}>
            ▲
          </span>
        </div>
      </div>

      {/* File list */}
      {!isCollapsed && (
        <div className="border-t border-gray-100 max-h-96 overflow-y-auto">
          {snapshots.map((snap) => {
            const isExpanded = expandedFile === snap.file_path;
            const fileName = snap.file_path.split('/').pop() || snap.file_path;
            const dirPath = snap.file_path.substring(0, snap.file_path.lastIndexOf('/'));
            
            return (
              <div key={snap.file_path} className="border-b border-gray-50 last:border-b-0">
                {/* File row */}
                <div className="flex items-center justify-between px-4 py-2 hover:bg-gray-50">
                  <div className="flex items-center gap-2 min-w-0">
                    <button
                      onClick={() => setExpandedFile(isExpanded ? null : snap.file_path)}
                      className="text-gray-400 hover:text-gray-600 transition-transform text-xs"
                    >
                      {isExpanded ? '▼' : '▶'}
                    </button>
                    <span className={`text-xs px-1.5 py-0.5 rounded ${
                      snap.is_new ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'
                    }`}>
                      {snap.is_new ? 'NEW' : 'EDIT'}
                    </span>
                    <button
                      onClick={() => onOpenFile(snap.file_path)}
                      className="text-sm text-indigo-600 hover:text-indigo-800 hover:underline truncate"
                      title={snap.file_path}
                    >
                      {fileName}
                    </button>
                    <span className="text-xs text-gray-400 truncate hidden sm:inline">
                      {dirPath}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 ml-2 shrink-0">
                    <button
                      onClick={() => onReject(snap.file_path)}
                      className="text-xs bg-red-50 text-red-600 px-2 py-1 rounded hover:bg-red-100 transition-colors"
                      title="Reject: revert file to previous version"
                    >
                      ✕ Reject
                    </button>
                    <button
                      onClick={() => onAccept(snap.file_path)}
                      className="text-xs bg-green-50 text-green-600 px-2 py-1 rounded hover:bg-green-100 transition-colors"
                      title="Accept: keep this change"
                    >
                      ✓ Accept
                    </button>
                  </div>
                </div>

                {/* Diff view */}
                {isExpanded && (
                  <div className="px-4 pb-3">
                    <DiffViewer
                      oldContent={snap.old_content}
                      newContent={snap.new_content}
                      maxHeight={300}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

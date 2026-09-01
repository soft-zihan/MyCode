import React, { useState } from 'react';
import { AlertTriangle, Terminal, FileEdit, PenLine, Loader2 } from 'lucide-react';

export interface PermissionRequest {
  request_id: string;
  action: string;
  resource: string;
  message: string;
}

interface PermissionDialogProps {
  request: PermissionRequest;
  onRespond: (requestId: string, allowed: boolean) => void;
}

const getActionIcon = (action: string) => {
  switch (action) {
    case 'edit': return <PenLine className="w-4 h-4" />;
    case 'bash': return <Terminal className="w-4 h-4" />;
    case 'write': return <FileEdit className="w-4 h-4" />;
    default: return <AlertTriangle className="w-4 h-4" />;
  }
};

const getActionAccent = (action: string) => {
  switch (action) {
    case 'edit': return 'border-l-amber-400';
    case 'bash': return 'border-l-red-400';
    case 'write': return 'border-l-orange-400';
    default: return 'border-l-gray-400';
  }
};

export const PermissionDialog: React.FC<PermissionDialogProps> = ({ request, onRespond }) => {
  const [responding, setResponding] = useState(false);

  const handleRespond = (allowed: boolean) => {
    setResponding(true);
    onRespond(request.request_id, allowed);
  };

  return (
    <div className="mx-4 mb-2">
      <div className={`rounded-lg border border-gray-200 dark:border-gray-700 border-l-4 ${getActionAccent(request.action)} bg-white dark:bg-gray-900 shadow-sm overflow-hidden`}>
        {/* Amber waiting strip */}
        <div className="flex items-center gap-2 px-3 py-1.5 bg-amber-50 dark:bg-amber-900/20 border-b border-amber-100 dark:border-amber-800/30">
          <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
          <span className="text-xs font-medium text-amber-700 dark:text-amber-300">
            等待审批
          </span>
        </div>

        {/* Body */}
        <div className="px-3 py-2.5">
          {/* Justification */}
          <div className="flex items-start gap-2.5">
            <div className="mt-0.5 text-gray-500 dark:text-gray-400">
              {getActionIcon(request.action)}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm text-gray-900 dark:text-gray-100">
                {request.message}
              </p>
              {request.resource && (
                <div className="mt-2 px-2 py-1.5 bg-gray-50 dark:bg-gray-800 rounded border border-gray-100 dark:border-gray-700">
                  <code className="text-xs font-mono text-gray-600 dark:text-gray-400 break-all whitespace-pre-wrap">
                    {request.resource}
                  </code>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Action row */}
        <div className="flex items-center justify-end gap-2 px-3 py-2 bg-gray-50 dark:bg-gray-800/50 border-t border-gray-100 dark:border-gray-700">
          <button
            onClick={() => handleRespond(false)}
            disabled={responding}
            className="px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            拒绝
          </button>
          <button
            onClick={() => handleRespond(true)}
            disabled={responding}
            className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 border border-transparent rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-1.5"
          >
            {responding && <Loader2 className="w-3 h-3 animate-spin" />}
            允许一次
          </button>
        </div>
      </div>
    </div>
  );
};

export default PermissionDialog;

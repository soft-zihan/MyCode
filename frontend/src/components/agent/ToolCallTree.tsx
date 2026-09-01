import { useState } from 'react';
import { ChevronDown, ChevronRight, FileText, FolderOpen, Search, Terminal, Wrench, Globe, Zap, Bot, Edit3, Eye } from 'lucide-react';
import type { ToolCallEvent } from '../../hooks';

interface ToolCallTreeProps {
  calls: Map<string, ToolCallEvent> | Array<{name: string; input: Record<string, unknown>; result?: string; status?: 'pending' | 'success' | 'error' | 'denied'}>;
  maxDisplay?: number;
}

const getToolIcon = (name: string) => {
  const iconMap: Record<string, React.FC<{className?: string}>> = {
    read_file: Eye,
    write_file: Edit3,
    edit_file: Edit3,
    list_files: FolderOpen,
    grep_search: Search,
    run_shell: Terminal,
    skill: Zap,
    agent: Bot,
    webfetch: Globe,
    file_search: FileText,
  };
  return iconMap[name] || Wrench;
};

const getStatusStyle = (status?: string) => {
  switch (status) {
    case 'success':
    case 'ok':
      return { color: 'text-green-500', bg: 'bg-green-50 dark:bg-green-900/20', border: 'border-green-200 dark:border-green-800' };
    case 'error':
      return { color: 'text-red-500', bg: 'bg-red-50 dark:bg-red-900/20', border: 'border-red-200 dark:border-red-800' };
    case 'denied':
      return { color: 'text-orange-500', bg: 'bg-orange-50 dark:bg-orange-900/20', border: 'border-orange-200 dark:border-orange-800' };
    default:
      return { color: 'text-blue-500', bg: 'bg-blue-50 dark:bg-blue-900/20', border: 'border-blue-200 dark:border-blue-800' };
  }
};

const formatInput = (name: string, input: Record<string, unknown>): string => {
  if (name === 'read_file' || name === 'write_file' || name === 'edit_file') {
    return (input.file_path as string) || '';
  }
  if (name === 'run_shell') {
    const cmd = (input.command as string) || '';
    return cmd.length > 60 ? cmd.slice(0, 60) + '...' : cmd;
  }
  if (name === 'grep_search') {
    return `"${input.pattern}" in ${input.path || '.'}`;
  }
  if (name === 'webfetch') {
    return (input.url as string) || '';
  }
  const str = JSON.stringify(input);
  return str.length > 60 ? str.slice(0, 60) + '...' : str;
};

const formatDuration = (ms?: number): string => {
  if (ms === undefined) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
};

interface ToolCallItemProps {
  call: ToolCallEvent;
}

const ToolCallItem: React.FC<ToolCallItemProps> = ({ call }) => {
  const [expanded, setExpanded] = useState(false);
  const Icon = getToolIcon(call.name);
  const statusStyle = getStatusStyle(call.status);
  const isPending = call.status === 'pending';

  return (
    <div className={`rounded-lg border ${statusStyle.border} overflow-hidden transition-all duration-200`}>
      <div
        className={`flex items-center gap-3 px-3 py-2 cursor-pointer ${statusStyle.bg} hover:opacity-80 transition-opacity`}
        onClick={() => setExpanded(!expanded)}
      >
        <div className={`flex items-center justify-center w-6 h-6 rounded ${statusStyle.bg}`}>
          <Icon className={`w-3.5 h-3.5 ${statusStyle.color}`} />
        </div>
        
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
              {call.name}
            </span>
            {isPending && (
              <span className="flex items-center gap-1">
                <span className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-pulse" />
                <span className="text-xs text-blue-500">Running...</span>
              </span>
            )}
            {call.status === 'success' && (
              <span className="text-xs text-green-500">✓ Done</span>
            )}
            {call.status === 'error' && (
              <span className="text-xs text-red-500">✗ Failed</span>
            )}
            {call.status === 'denied' && (
              <span className="text-xs text-orange-500">⊘ Denied</span>
            )}
          </div>
          <div className="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">
            {formatInput(call.name, call.input)}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {call.duration_ms !== undefined && (
            <span className="text-xs text-gray-400">{formatDuration(call.duration_ms)}</span>
          )}
          {call.result && (
            expanded ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />
          )}
        </div>
      </div>

      {expanded && call.result && (
        <div className="px-3 py-2 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
          <div className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Result:</div>
          <pre className="text-xs bg-gray-50 dark:bg-gray-800 p-2 rounded overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap text-gray-700 dark:text-gray-300">
            {call.result.slice(0, 2000)}
            {call.result.length > 2000 && '\n... (truncated)'}
          </pre>
        </div>
      )}

      {expanded && call.snapshot && (
        <div className="px-3 py-2 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
          <div className="text-xs text-blue-600 dark:text-blue-400">
            📝 File changed: {call.snapshot.file_path}
          </div>
        </div>
      )}
    </div>
  );
};

export const ToolCallTree: React.FC<ToolCallTreeProps> = ({ calls, maxDisplay = 10 }) => {
  const [showAll, setShowAll] = useState(false);

  const callsArray = Array.isArray(calls) 
    ? calls.map((c, i) => ({
        call_id: `tc_${i}`,
        name: c.name,
        input: c.input,
        result: c.result,
        status: c.status || 'pending',
      } as ToolCallEvent))
    : Array.from(calls.values());

  if (callsArray.length === 0) {
    return null;
  }

  const displayCalls = showAll ? callsArray : callsArray.slice(-maxDisplay);
  const hiddenCount = callsArray.length - displayCalls.length;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
          <Wrench className="w-4 h-4" />
          Tool Calls ({callsArray.length})
        </h3>
        {hiddenCount > 0 && (
          <button
            onClick={() => setShowAll(!showAll)}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
          >
            {showAll ? 'Show less' : `Show ${hiddenCount} more`}
          </button>
        )}
      </div>
      <div className="space-y-1.5">
        {displayCalls.map((call) => (
          <ToolCallItem key={call.call_id} call={call} />
        ))}
      </div>
    </div>
  );
};

export default ToolCallTree;

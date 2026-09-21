import { useState, type ReactNode } from 'react';
import { 
  ChevronRight,
  Eye, 
  Edit3, 
  Search, 
  Terminal, 
  Wrench, 
  Globe, 
  Zap, 
  Bot,
  XCircle,
  Loader2
} from 'lucide-react';
import type { ToolCallEvent } from '../../hooks';

interface ToolRowProps {
  call: ToolCallEvent;
}

// Tool variant determines icon and visual style
type ToolVariant = 'read' | 'write' | 'edit' | 'search' | 'bash' | 'web' | 'skill' | 'agent' | 'other';

const getToolVariant = (name: string): ToolVariant => {
  if (name.includes('read') || name === 'file_search') return 'read';
  if (name.includes('write')) return 'write';
  if (name.includes('edit')) return 'edit';
  if (name.includes('search') || name.includes('grep') || name.includes('glob')) return 'search';
  if (name.includes('shell') || name.includes('bash') || name.includes('run')) return 'bash';
  if (name.includes('web') || name.includes('fetch')) return 'web';
  if (name.includes('skill')) return 'skill';
  if (name.includes('agent') || name.includes('task')) return 'agent';
  return 'other';
};

const VARIANT_ICONS: Record<ToolVariant, ReactNode> = {
  read: <Eye className="w-3.5 h-3.5" />,
  write: <Edit3 className="w-3.5 h-3.5" />,
  edit: <Edit3 className="w-3.5 h-3.5" />,
  search: <Search className="w-3.5 h-3.5" />,
  bash: <Terminal className="w-3.5 h-3.5" />,
  web: <Globe className="w-3.5 h-3.5" />,
  skill: <Zap className="w-3.5 h-3.5" />,
  agent: <Bot className="w-3.5 h-3.5" />,
  other: <Wrench className="w-3.5 h-3.5" />,
};

const VARIANT_LABELS: Record<ToolVariant, string> = {
  read: 'Read',
  write: 'Write',
  edit: 'Edit',
  search: 'Search',
  bash: 'Shell',
  web: 'Web',
  skill: 'Skill',
  agent: 'Agent',
  other: '',
};

type RowState = 'running' | 'ok' | 'error' | 'stopped';

const getStateConfig = (status?: string): { state: RowState; dot: ReactNode } => {
  switch (status) {
    case 'success':
    case 'ok':
      return { 
        state: 'ok', 
        dot: <span className="w-2 h-2 rounded-full bg-green-500" />,
      };
    case 'error':
      return { 
        state: 'error', 
        dot: <span className="w-2 h-2 rounded-full bg-red-500" />,
      };
    case 'denied':
      return { 
        state: 'stopped', 
        dot: <span className="w-2 h-2 rounded-full bg-amber-500 ring-2 ring-amber-500/30" />,
      };
    default:
      return { 
        state: 'running', 
        dot: <Loader2 className="w-3 h-3 text-indigo-500 animate-spin" />,
      };
  }
};

// Extract summary from tool input
const getToolSummary = (name: string, input: Record<string, unknown>): string => {
  if (name === 'read_file' || name === 'write_file' || name === 'edit_file') {
    return (input.file_path as string) || '';
  }
  if (name === 'run_shell') {
    return (input.command as string) || '';
  }
  if (name === 'grep_search') {
    const pattern = input.pattern || '';
    const path = input.path || '.';
    return `"${pattern}" in ${path}`;
  }
  if (name === 'webfetch') {
    return (input.url as string) || '';
  }
  if (name === 'list_files') {
    const pattern = (input.pattern as string) || '';
    const path = (input.path as string) || '.';
    return pattern ? `${pattern}${path !== '.' ? ` in ${path}` : ''}` : path;
  }
  if (name === 'agent') {
    return (input.description as string) || (input.type as string) || '';
  }
  const str = JSON.stringify(input);
  return str.length > 80 ? str.slice(0, 80) + '...' : str;
};

const formatDuration = (ms?: number): string => {
  if (ms === undefined) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
};

// Diff 视图组件，用于显示 edit_file 的结果
const DiffView: React.FC<{ content: string }> = ({ content }) => {
  const lines = content.split('\n');
  
  return (
    <div className="text-xs bg-white border border-gray-200 rounded p-2 overflow-x-auto max-h-48 overflow-y-auto font-mono">
      {lines.map((line, i) => {
        let className = 'text-gray-700';
        let bgColor = '';
        
        if (line.startsWith('@@')) {
          className = 'text-indigo-600 font-medium';
        } else if (line.startsWith('- ')) {
          className = 'text-red-700';
          bgColor = 'bg-red-50';
        } else if (line.startsWith('+ ')) {
          className = 'text-green-700';
          bgColor = 'bg-green-50';
        } else if (line.startsWith('Successfully')) {
          className = 'text-gray-500 italic';
        }
        
        return (
          <div key={i} className={`${className} ${bgColor} px-1`}>
            {line || '\u00A0'}
          </div>
        );
      })}
    </div>
  );
};

export const ToolRow: React.FC<ToolRowProps> = ({ call }) => {
  const [expanded, setExpanded] = useState(false);
  
  const variant = getToolVariant(call.name);
  const icon = VARIANT_ICONS[variant];
  const label = variant === 'other' ? call.name : VARIANT_LABELS[variant];
  const { state, dot } = getStateConfig(call.status);
  const summary = getToolSummary(call.name, call.input);
  const isRunning = state === 'running';
  const isError = state === 'error';
  
  // Error state shows failure message as summary
  const displaySummary = isError && call.result 
    ? call.result.split('\n')[0].slice(0, 100)
    : summary;
  
  const hasExpandableContent = call.result || call.snapshot;

  return (
    <div 
      className="group rounded-md border border-gray-200 overflow-hidden transition-colors hover:border-gray-300"
      data-state={state}
      data-variant={variant}
    >
      {/* Single-line summary row */}
      <div
        className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer transition-colors ${
          isRunning ? 'bg-indigo-50/50' : 
          isError ? 'bg-red-50/50' : 
          'hover:bg-gray-50'
        }`}
        onClick={() => hasExpandableContent && setExpanded(!expanded)}
      >
        {/* Leading slot: state dot for error/running, tool icon otherwise */}
        <div className="flex items-center justify-center w-4 h-4 flex-shrink-0">
          {isError ? (
            <XCircle className="w-3.5 h-3.5 text-red-500" />
          ) : isRunning ? (
            dot
          ) : (
            <span className="text-gray-500">{icon}</span>
          )}
        </div>
        
        {/* Title */}
        <span className="text-sm font-medium text-gray-900 flex-shrink-0">
          {label}
        </span>
        
        {/* Separator dot */}
        {displaySummary && (
          <span className="w-1 h-1 rounded-full bg-gray-300 flex-shrink-0" aria-hidden />
        )}
        
        {/* Summary - truncated */}
        <span 
          className={`text-sm truncate min-w-0 ${
            isError ? 'text-red-600' : 'text-gray-500'
          }`}
          title={displaySummary}
        >
          {displaySummary}
        </span>
        
        {/* Right side: duration and expand indicator */}
        <div className="flex items-center gap-2 ml-auto flex-shrink-0">
          {call.duration_ms !== undefined && (
            <span className="text-xs text-gray-400">
              {formatDuration(call.duration_ms)}
            </span>
          )}
          {hasExpandableContent && (
            <ChevronRight 
              className={`w-3.5 h-3.5 text-gray-400 transition-transform ${
                expanded ? 'rotate-90' : ''
              }`}
            />
          )}
        </div>
      </div>

      {/* Expanded body */}
      {expanded && hasExpandableContent && (
        <div className="border-t border-gray-200 bg-gray-50">
          {/* Input/Output card style */}
          <div className="p-2.5 space-y-2">
            {/* Output section */}
            {call.result && (
              <div className="space-y-1">
                <span className="text-[10px] font-medium text-gray-400 uppercase tracking-wider">
                  Output
                </span>
                {/* 对于 edit_file，显示 diff 格式 */}
                {call.name === 'edit_file' ? (
                  <DiffView content={call.result.slice(0, 3000)} />
                ) : (
                  <pre className="text-xs bg-white border border-gray-200 rounded p-2 overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap text-gray-700 font-mono">
                    {call.result.slice(0, 3000)}
                    {call.result.length > 3000 && '\n\n... (truncated)'}
                  </pre>
                )}
              </div>
            )}
            
            {/* File change indicator */}
            {call.snapshot && (
              <div className="flex items-center gap-1.5 text-xs text-indigo-600">
                <Edit3 className="w-3 h-3" />
                <span className="font-medium">Modified:</span>
                <span className="font-mono">{call.snapshot.file_path}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default ToolRow;

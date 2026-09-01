import { useState } from 'react';
import { ChevronDown, ChevronRight, Eye, FolderOpen, Search, Loader2 } from 'lucide-react';
import type { ToolCallEvent } from '../../hooks';

interface ContextToolGroupProps {
  tools: ToolCallEvent[];
  isStreaming?: boolean;
}

// Context tools that should be grouped together
const CONTEXT_TOOLS = ['read_file', 'list_files', 'grep_search', 'file_search'];

const getContextIcon = (tool: string) => {
  switch (tool) {
    case 'read_file': return Eye;
    case 'list_files': return FolderOpen;
    case 'grep_search':
    case 'file_search': return Search;
    default: return Search;
  }
};

const getContextSummary = (tools: ToolCallEvent[]) => {
  const reads = tools.filter(t => t.name === 'read_file').length;
  const searches = tools.filter(t => t.name === 'grep_search' || t.name === 'file_search').length;
  const lists = tools.filter(t => t.name === 'list_files').length;
  
  const parts: string[] = [];
  if (reads > 0) parts.push(`${reads} file${reads > 1 ? 's' : ''}`);
  if (searches > 0) parts.push(`${searches} search${searches > 1 ? 'es' : ''}`);
  if (lists > 0) parts.push(`${lists} list${lists > 1 ? 's' : ''}`);
  
  return parts.join(', ');
};

export const ContextToolGroup: React.FC<ContextToolGroupProps> = ({ tools, isStreaming: _isStreaming }) => {
  const [expanded, setExpanded] = useState(false);
  
  if (tools.length === 0) return null;
  
  const hasPending = tools.some(t => t.status === 'pending');
  const summary = getContextSummary(tools);

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      {/* Collapsible header */}
      <div
        className="flex items-center gap-3 px-3 py-2.5 cursor-pointer bg-gray-50 dark:bg-gray-800/50 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        {/* Status icon */}
        <div className="flex items-center justify-center w-7 h-7 rounded-md bg-gray-100 dark:bg-gray-700">
          {hasPending ? (
            <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />
          ) : (
            <Search className="w-4 h-4 text-gray-500 dark:text-gray-400" />
          )}
        </div>
        
        {/* Title and summary */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
              {hasPending ? 'Gathering context' : 'Context gathered'}
            </span>
            {hasPending && (
              <span className="flex items-center gap-1">
                <span className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-pulse" />
              </span>
            )}
          </div>
          <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            {summary}
          </div>
        </div>

        {/* Expand indicator */}
        {expanded ? (
          <ChevronDown className="w-4 h-4 text-gray-400" />
        ) : (
          <ChevronRight className="w-4 h-4 text-gray-400" />
        )}
      </div>

      {/* Expanded tool list */}
      {expanded && (
        <div className="border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
          <div className="divide-y divide-gray-100 dark:divide-gray-800">
            {tools.map((tool) => {
              const Icon = getContextIcon(tool.name);
              const isPending = tool.status === 'pending';
              
              return (
                <div 
                  key={tool.call_id}
                  className="flex items-center gap-3 px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
                >
                  <Icon className={`w-3.5 h-3.5 ${isPending ? 'text-blue-500' : 'text-gray-400'}`} />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs font-mono text-gray-600 dark:text-gray-400 truncate">
                      {tool.name === 'read_file' && String(tool.input.file_path || '')}
                      {tool.name === 'grep_search' && `"${String(tool.input.pattern || '')}"`}
                      {tool.name === 'list_files' && String(tool.input.path || '')}
                      {tool.name === 'file_search' && String(tool.input.pattern || '')}
                    </div>
                  </div>
                  {isPending ? (
                    <Loader2 className="w-3 h-3 text-blue-500 animate-spin" />
                  ) : tool.status === 'success' ? (
                    <span className="text-xs text-green-500">✓</span>
                  ) : tool.status === 'error' ? (
                    <span className="text-xs text-red-500">✗</span>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
};

// Helper to check if a tool is a context tool
export const isContextTool = (name: string): boolean => {
  return CONTEXT_TOOLS.includes(name);
};

export default ContextToolGroup;

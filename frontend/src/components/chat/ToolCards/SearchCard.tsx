import React, { useState } from 'react';
import { Search, ChevronRight, ChevronDown, CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import type { ToolCallEvent } from '../../../hooks';

interface SearchCardProps {
  call: ToolCallEvent;
}

const searchPattern = (call: ToolCallEvent): string => {
  return (call.input.pattern as string) || (call.input.query as string) || '';
};

const searchPath = (call: ToolCallEvent): string => {
  return (call.input.path as string) || '.';
};

const resultCount = (call: ToolCallEvent): number | null => {
  if (!call.result) return null;
  const lines = call.result.split('\n').filter(l => l.trim());
  return lines.length;
};

export const SearchCard: React.FC<SearchCardProps> = ({ call }) => {
  const [expanded, setExpanded] = useState(false);
  const pattern = searchPattern(call);
  const path = searchPath(call);
  const count = resultCount(call);
  const isRunning = call.status === 'pending';
  const isError = call.status === 'error';
  const isSuccess = call.status === 'success';

  return (
    <div className={`tool-card tool-card-search ${isRunning ? 'running' : ''} ${isError ? 'error' : ''}`}>
      <div
        className="tool-card-header"
        onClick={() => call.result && setExpanded(!expanded)}
      >
        <div className="tool-card-icon search">
          <Search className="w-3.5 h-3.5" />
        </div>
        
        <div className="tool-card-content">
          <code className="tool-card-search-pattern">"{pattern}"</code>
          {path !== '.' && <span className="tool-card-search-path">in {path}</span>}
        </div>

        <div className="tool-card-status">
          {isRunning && <Loader2 className="w-3.5 h-3.5 text-blue-500 animate-spin" />}
          {isSuccess && count !== null && (
            <span className="tool-card-result-count">{count} results</span>
          )}
          {isSuccess && count === null && <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />}
          {isError && <XCircle className="w-3.5 h-3.5 text-red-500" />}
          {call.result && (
            expanded ? <ChevronDown className="w-3.5 h-3.5 text-gray-400" /> : <ChevronRight className="w-3.5 h-3.5 text-gray-400" />
          )}
        </div>
      </div>

      {expanded && call.result && (
        <div className="tool-card-body">
          <pre className="tool-card-output">{call.result.slice(0, 3000)}</pre>
        </div>
      )}
    </div>
  );
};

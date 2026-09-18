import React, { useState } from 'react';
import { Wrench, ChevronRight, ChevronDown, CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import type { ToolCallEvent } from '../../../hooks';

interface DefaultCardProps {
  call: ToolCallEvent;
}

const formatInput = (call: ToolCallEvent): string => {
  const input = call.input;
  const str = JSON.stringify(input);
  return str.length > 80 ? str.slice(0, 80) + '...' : str;
};

export const DefaultCard: React.FC<DefaultCardProps> = ({ call }) => {
  const [expanded, setExpanded] = useState(false);
  const summary = formatInput(call);
  const isRunning = call.status === 'pending';
  const isError = call.status === 'error';
  const isSuccess = call.status === 'success';

  return (
    <div className={`tool-card tool-card-default ${isRunning ? 'running' : ''} ${isError ? 'error' : ''}`}>
      <div
        className="tool-card-header"
        onClick={() => call.result && setExpanded(!expanded)}
      >
        <div className="tool-card-icon default">
          <Wrench className="w-3.5 h-3.5" />
        </div>
        
        <div className="tool-card-content">
          <span className="tool-card-tool-name">{call.name}</span>
          {summary && <span className="tool-card-tool-summary">{summary}</span>}
        </div>

        <div className="tool-card-status">
          {isRunning && <Loader2 className="w-3.5 h-3.5 text-blue-500 animate-spin" />}
          {isSuccess && <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />}
          {isError && <XCircle className="w-3.5 h-3.5 text-red-500" />}
          {call.duration_ms !== undefined && (
            <span className="tool-card-duration">
              {call.duration_ms < 1000 ? `${Math.round(call.duration_ms)}ms` : `${(call.duration_ms / 1000).toFixed(1)}s`}
            </span>
          )}
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

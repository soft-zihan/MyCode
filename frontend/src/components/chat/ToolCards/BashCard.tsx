import React, { useState } from 'react';
import { Terminal, ChevronRight, ChevronDown, CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import type { ToolCallEvent } from '../../../hooks';

interface BashCardProps {
  call: ToolCallEvent;
}

const command = (call: ToolCallEvent): string => {
  return (call.input.command as string) || '';
};

const exitCode = (call: ToolCallEvent): number | null => {
  const timeout = call.input.timeout as number | undefined;
  if (call.status === 'error' && timeout) return 124;
  if (call.status === 'success') return 0;
  if (call.status === 'error') return 1;
  return null;
};

export const BashCard: React.FC<BashCardProps> = ({ call }) => {
  const [expanded, setExpanded] = useState(false);
  const cmd = command(call);
  const code = exitCode(call);
  const isRunning = call.status === 'pending';
  const isError = call.status === 'error';
  const isSuccess = call.status === 'success';

  return (
    <div className={`tool-card tool-card-bash ${isRunning ? 'running' : ''} ${isError ? 'error' : ''}`}>
      <div
        className="tool-card-header"
        onClick={() => call.result && setExpanded(!expanded)}
      >
        <div className="tool-card-icon bash">
          <Terminal className="w-3.5 h-3.5" />
        </div>
        
        <div className="tool-card-content">
          <code className="tool-card-bash-command">{cmd || 'shell command'}</code>
        </div>

        <div className="tool-card-status">
          {isRunning && <Loader2 className="w-3.5 h-3.5 text-blue-500 animate-spin" />}
          {isSuccess && <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />}
          {isError && <XCircle className="w-3.5 h-3.5 text-red-500" />}
          {code !== null && !isRunning && (
            <span className={`tool-card-exit-code ${code === 0 ? 'success' : 'error'}`}>
              {code}
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

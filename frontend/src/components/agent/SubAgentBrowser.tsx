import { useState } from 'react';
import { ChevronDown, ChevronRight, Bot, Clock, Cpu, CheckCircle, XCircle, Loader2 } from 'lucide-react';
import type { SubAgentEvent } from '../../hooks';

interface SubAgentBrowserProps {
  subAgents: Map<string, SubAgentEvent>;
}

const getStatusStyle = (status: string) => {
  switch (status) {
    case 'completed':
      return { 
        color: 'text-green-500', 
        bg: 'bg-green-50 dark:bg-green-900/20', 
        border: 'border-green-200 dark:border-green-800',
        icon: CheckCircle
      };
    case 'error':
      return { 
        color: 'text-red-500', 
        bg: 'bg-red-50 dark:bg-red-900/20', 
        border: 'border-red-200 dark:border-red-800',
        icon: XCircle
      };
    default:
      return { 
        color: 'text-blue-500', 
        bg: 'bg-blue-50 dark:bg-blue-900/20', 
        border: 'border-blue-200 dark:border-blue-800',
        icon: Loader2
      };
  }
};

const formatDuration = (ms?: number): string => {
  if (ms === undefined) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
};

const formatTokens = (tokens?: number): string => {
  if (tokens === undefined) return '';
  if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
  return `${tokens}`;
};

interface SubAgentItemProps {
  agent: SubAgentEvent;
}

const SubAgentItem: React.FC<SubAgentItemProps> = ({ agent }) => {
  const [expanded, setExpanded] = useState(false);
  const statusStyle = getStatusStyle(agent.status);
  const StatusIcon = statusStyle.icon;
  const isRunning = agent.status === 'running';

  return (
    <div className={`rounded-lg border ${statusStyle.border} overflow-hidden transition-all duration-200`}>
      <div
        className={`flex items-center gap-3 px-3 py-2 cursor-pointer ${statusStyle.bg} hover:opacity-80 transition-opacity`}
        onClick={() => setExpanded(!expanded)}
      >
        <div className={`flex items-center justify-center w-6 h-6 rounded ${statusStyle.bg}`}>
          <StatusIcon className={`w-3.5 h-3.5 ${statusStyle.color} ${isRunning ? 'animate-spin' : ''}`} />
        </div>
        
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <Bot className="w-3.5 h-3.5 text-purple-500" />
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
              {agent.agent_type}
            </span>
            {isRunning && (
              <span className="flex items-center gap-1">
                <span className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-pulse" />
                <span className="text-xs text-blue-500">Running...</span>
              </span>
            )}
            {agent.status === 'completed' && (
              <span className="text-xs text-green-500">✓ Done</span>
            )}
            {agent.status === 'error' && (
              <span className="text-xs text-red-500">✗ Failed</span>
            )}
          </div>
          <div className="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">
            {agent.description}
          </div>
        </div>

        <div className="flex items-center gap-3">
          {agent.duration_ms !== undefined && (
            <div className="flex items-center gap-1 text-xs text-gray-400">
              <Clock className="w-3 h-3" />
              <span>{formatDuration(agent.duration_ms)}</span>
            </div>
          )}
          {agent.tokens !== undefined && (
            <div className="flex items-center gap-1 text-xs text-gray-400">
              <Cpu className="w-3 h-3" />
              <span>{formatTokens(agent.tokens)}</span>
            </div>
          )}
          {(agent.summary || agent.tokens !== undefined) && (
            expanded ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />
          )}
        </div>
      </div>

      {expanded && (agent.summary || agent.tokens !== undefined) && (
        <div className="px-3 py-2 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
          {agent.summary && (
            <div className="text-xs text-gray-600 dark:text-gray-400 mb-2">
              <span className="font-medium text-gray-700 dark:text-gray-300">Summary:</span>
              <p className="mt-1 whitespace-pre-wrap">{agent.summary}</p>
            </div>
          )}
          <div className="flex items-center gap-4 text-xs text-gray-500">
            {agent.tokens !== undefined && (
              <div className="flex items-center gap-1">
                <Cpu className="w-3 h-3" />
                <span>Tokens: {formatTokens(agent.tokens)}</span>
              </div>
            )}
            {agent.duration_ms !== undefined && (
              <div className="flex items-center gap-1">
                <Clock className="w-3 h-3" />
                <span>Duration: {formatDuration(agent.duration_ms)}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export const SubAgentBrowser: React.FC<SubAgentBrowserProps> = ({ subAgents }) => {
  const agentsArray = Array.from(subAgents.values());

  if (agentsArray.length === 0) {
    return null;
  }

  const runningCount = agentsArray.filter(a => a.status === 'running').length;
  const completedCount = agentsArray.filter(a => a.status === 'completed').length;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
          <Bot className="w-4 h-4 text-purple-500" />
          Sub-Agents ({agentsArray.length})
        </h3>
        <div className="flex items-center gap-2 text-xs">
          {runningCount > 0 && (
            <span className="text-blue-500">{runningCount} running</span>
          )}
          {completedCount > 0 && (
            <span className="text-green-500">{completedCount} done</span>
          )}
        </div>
      </div>
      <div className="space-y-1.5">
        {agentsArray.map((agent) => (
          <SubAgentItem key={agent.agent_id} agent={agent} />
        ))}
      </div>
    </div>
  );
};

export default SubAgentBrowser;

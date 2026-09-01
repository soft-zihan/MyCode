import { useState } from 'react';
import { 
  ChevronRight, 
  ChevronDown,
  Bot, 
  Loader2,
  Clock,
  Cpu
} from 'lucide-react';
import type { SubAgentEvent } from '../../hooks';

interface SubAgentTreeProps {
  subAgents: Map<string, SubAgentEvent>;
}

const getStateConfig = (status: string) => {
  switch (status) {
    case 'completed':
      return { 
        dot: <span className="w-2 h-2 rounded-full bg-green-500" />,
      };
    case 'error':
      return { 
        dot: <span className="w-2 h-2 rounded-full bg-red-500" />,
      };
    default:
      return { 
        dot: <Loader2 className="w-3 h-3 text-blue-500 animate-spin" />,
      };
  }
};

const formatDuration = (ms?: number): string => {
  if (ms === undefined) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
};

const formatTokens = (tokens?: number): string => {
  if (tokens === undefined) return '';
  if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
  return `${tokens}`;
};

interface SubAgentRowProps {
  agent: SubAgentEvent;
  level?: number;
}

const SubAgentRow: React.FC<SubAgentRowProps> = ({ agent, level = 0 }) => {
  const [expanded, setExpanded] = useState(false);
  
  const { dot } = getStateConfig(agent.status);
  const isRunning = agent.status === 'running';
  const hasExpandableContent = agent.summary || agent.tokens !== undefined;
  
  // Build secondary info: mode · activity
  const activity = isRunning ? 'running' : 'inactive';
  const secondary = [agent.agent_type, activity].join(' · ');

  // Metrics: tokens + duration
  const tokenMetric = agent.tokens !== undefined ? `${formatTokens(agent.tokens)} tok` : undefined;
  const durationMetric = agent.duration_ms !== undefined ? formatDuration(agent.duration_ms) : undefined;
  const metrics = [tokenMetric, durationMetric].filter(Boolean).join(' · ');

  return (
    <div className="group">
      <div
        className={`flex items-center gap-2 px-2.5 py-1.5 rounded-md cursor-pointer transition-colors ${
          isRunning 
            ? 'bg-blue-50/50 dark:bg-blue-900/10' 
            : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
        }`}
        style={{ paddingLeft: `${level * 12 + 10}px` }}
        onClick={() => hasExpandableContent && setExpanded(!expanded)}
      >
        {/* Disclosure chevron for expandable rows */}
        <div className="w-3.5 h-3.5 flex items-center justify-center flex-shrink-0">
          {hasExpandableContent ? (
            expanded ? (
              <ChevronDown className="w-3 h-3 text-gray-400" />
            ) : (
              <ChevronRight className="w-3 h-3 text-gray-400" />
            )
          ) : (
            <span className="w-3" />
          )}
        </div>
        
        {/* State dot */}
        <div className="flex items-center justify-center w-4 h-4 flex-shrink-0">
          {dot}
        </div>
        
        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            {/* Label */}
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
              {agent.description || agent.agent_type}
            </span>
          </div>
          {/* Secondary line */}
          <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
            {secondary}
          </div>
        </div>
        
        {/* Metrics */}
        {metrics && (
          <div className="flex items-center gap-2 text-xs text-gray-400 dark:text-gray-500 flex-shrink-0">
            {tokenMetric && (
              <span className="flex items-center gap-0.5">
                <Cpu className="w-3 h-3" />
                {tokenMetric}
              </span>
            )}
            {durationMetric && (
              <span className="flex items-center gap-0.5">
                <Clock className="w-3 h-3" />
                {durationMetric}
              </span>
            )}
          </div>
        )}
      </div>
      
      {/* Expanded content */}
      {expanded && hasExpandableContent && (
        <div 
          className="px-3 py-2 ml-6 border-l-2 border-gray-200 dark:border-gray-700"
          style={{ marginLeft: `${level * 12 + 24}px` }}
        >
          {agent.summary && (
            <div className="text-xs text-gray-600 dark:text-gray-400 whitespace-pre-wrap">
              {agent.summary}
            </div>
          )}
          {(agent.tokens !== undefined || agent.duration_ms !== undefined) && (
            <div className="flex items-center gap-3 mt-2 pt-2 border-t border-gray-100 dark:border-gray-800 text-xs text-gray-500">
              {agent.tokens !== undefined && (
                <span>Tokens: <span className="font-medium text-gray-700 dark:text-gray-300">{formatTokens(agent.tokens)}</span></span>
              )}
              {agent.duration_ms !== undefined && (
                <span>Duration: <span className="font-medium text-gray-700 dark:text-gray-300">{formatDuration(agent.duration_ms)}</span></span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export const SubAgentTree: React.FC<SubAgentTreeProps> = ({ subAgents }) => {
  const agentsArray = Array.from(subAgents.values());

  if (agentsArray.length === 0) return null;

  const runningCount = agentsArray.filter(a => a.status === 'running').length;
  const completedCount = agentsArray.filter(a => a.status === 'completed').length;
  const errorCount = agentsArray.filter(a => a.status === 'error').length;

  return (
    <div className="space-y-0.5">
      {/* Header with stats */}
      <div className="flex items-center justify-between px-2.5 py-1">
        <div className="flex items-center gap-2">
          <Bot className="w-4 h-4 text-indigo-500" />
          <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
            Sub-Agents
          </span>
          <span className="text-xs text-gray-400">
            ({agentsArray.length})
          </span>
        </div>
        <div className="flex items-center gap-2 text-xs">
          {runningCount > 0 && (
            <span className="flex items-center gap-1 text-blue-500">
              <Loader2 className="w-3 h-3 animate-spin" />
              {runningCount}
            </span>
          )}
          {completedCount > 0 && (
            <span className="text-green-500">{completedCount} done</span>
          )}
          {errorCount > 0 && (
            <span className="text-red-500">{errorCount} failed</span>
          )}
        </div>
      </div>

      {/* Agent rows */}
      <div className="space-y-0.5">
        {agentsArray.map((agent) => (
          <SubAgentRow key={agent.agent_id} agent={agent} />
        ))}
      </div>
    </div>
  );
};

export default SubAgentTree;

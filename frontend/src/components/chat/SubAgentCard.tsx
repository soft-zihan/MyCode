import { useState } from 'react';
import { 
  ChevronDown, 
  ChevronRight, 
  Bot, 
  CheckCircle, 
  XCircle, 
  Loader2,
  Clock,
  Cpu,
  MessageSquare,
  Sparkles
} from 'lucide-react';
import type { SubAgentEvent } from '../../hooks';

interface SubAgentCardProps {
  agent: SubAgentEvent;
}

// Agent type configuration with colors and icons
const AGENT_CONFIG: Record<string, { color: string; icon: React.FC<{className?: string}> }> = {
  explore: { color: 'text-cyan-500', icon: Sparkles },
  build: { color: 'text-green-500', icon: Bot },
  plan: { color: 'text-purple-500', icon: MessageSquare },
  review: { color: 'text-orange-500', icon: Bot },
  writer: { color: 'text-pink-500', icon: Bot },
};

const DEFAULT_AGENT = { color: 'text-indigo-500', icon: Bot };

const getStatusConfig = (status: string) => {
  switch (status) {
    case 'completed':
      return { 
        icon: CheckCircle, 
        color: 'text-green-500', 
        bg: 'bg-green-50 dark:bg-green-900/20',
        border: 'border-green-200 dark:border-green-800',
        label: 'Completed'
      };
    case 'error':
      return { 
        icon: XCircle, 
        color: 'text-red-500', 
        bg: 'bg-red-50 dark:bg-red-900/20',
        border: 'border-red-200 dark:border-red-800',
        label: 'Failed'
      };
    default:
      return { 
        icon: Loader2, 
        color: 'text-blue-500', 
        bg: 'bg-blue-50 dark:bg-blue-900/20',
        border: 'border-blue-200 dark:border-blue-800',
        label: 'Running'
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

export const SubAgentCard: React.FC<SubAgentCardProps> = ({ agent }) => {
  const [expanded, setExpanded] = useState(false);
  
  const agentType = agent.agent_type.toLowerCase();
  const agentConfig = AGENT_CONFIG[agentType] || DEFAULT_AGENT;
  const statusConfig = getStatusConfig(agent.status);
  const isRunning = agent.status === 'running';
  const AgentIcon = agentConfig.icon;
  const StatusIcon = statusConfig.icon;

  return (
    <div className={`rounded-lg border ${statusConfig.border} overflow-hidden transition-all duration-200`}>
      {/* Header */}
      <div
        className={`flex items-center gap-3 px-3 py-2.5 cursor-pointer ${statusConfig.bg} hover:opacity-90 transition-opacity`}
        onClick={() => (agent.summary || agent.tokens !== undefined) && setExpanded(!expanded)}
      >
        {/* Agent icon */}
        <div className={`flex items-center justify-center w-7 h-7 rounded-md ${statusConfig.bg}`}>
          <AgentIcon className={`w-4 h-4 ${agentConfig.color}`} />
        </div>
        
        {/* Main content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
              {agent.agent_type}
            </span>
            {isRunning ? (
              <span className="flex items-center gap-1.5">
                <Loader2 className="w-3 h-3 text-blue-500 animate-spin" />
                <span className="text-xs text-blue-500 font-medium">{statusConfig.label}</span>
              </span>
            ) : (
              <span className={`flex items-center gap-1 text-xs ${statusConfig.color}`}>
                <StatusIcon className="w-3 h-3" />
                <span className="font-medium">{statusConfig.label}</span>
              </span>
            )}
          </div>
          <div className="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">
            {agent.description}
          </div>
        </div>

        {/* Right side: stats and expand */}
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
            expanded ? (
              <ChevronDown className="w-4 h-4 text-gray-400" />
            ) : (
              <ChevronRight className="w-4 h-4 text-gray-400" />
            )
          )}
        </div>
      </div>

      {/* Expandable content */}
      {expanded && (agent.summary || agent.tokens !== undefined) && (
        <div className="px-3 py-2.5 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
          {agent.summary && (
            <div className="mb-3">
              <div className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">Summary:</div>
              <div className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed">
                {agent.summary}
              </div>
            </div>
          )}
          <div className="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400 pt-2 border-t border-gray-100 dark:border-gray-800">
            {agent.tokens !== undefined && (
              <div className="flex items-center gap-1.5">
                <Cpu className="w-3.5 h-3.5" />
                <span>Tokens: <span className="font-medium text-gray-700 dark:text-gray-300">{formatTokens(agent.tokens)}</span></span>
              </div>
            )}
            {agent.duration_ms !== undefined && (
              <div className="flex items-center gap-1.5">
                <Clock className="w-3.5 h-3.5" />
                <span>Duration: <span className="font-medium text-gray-700 dark:text-gray-300">{formatDuration(agent.duration_ms)}</span></span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default SubAgentCard;

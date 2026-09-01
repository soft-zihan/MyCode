import React, { useState } from 'react';
import { ChevronRight, ChevronDown, Bot, Clock, CheckCircle, XCircle, Loader2 } from 'lucide-react';

interface SubAgent {
  id: string;
  type: string;
  status: 'running' | 'completed' | 'error';
  durationMs?: number;
  tokens?: { input: number; output: number };
  summary?: string;
  children?: SubAgent[];
}

interface SubAgentLineageProps {
  agents: SubAgent[];
}

export const SubAgentLineage: React.FC<SubAgentLineageProps> = ({ agents }) => {
  if (agents.length === 0) return null;

  return (
    <div className="space-y-1">
      {agents.map(agent => (
        <SubAgentNode key={agent.id} agent={agent} depth={0} />
      ))}
    </div>
  );
};

interface SubAgentNodeProps {
  agent: SubAgent;
  depth: number;
}

const SubAgentNode: React.FC<SubAgentNodeProps> = ({ agent, depth }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const hasChildren = agent.children && agent.children.length > 0;

  const getStatusIcon = () => {
    switch (agent.status) {
      case 'running':
        return <Loader2 className="w-3.5 h-3.5 text-blue-500 animate-spin" />;
      case 'completed':
        return <CheckCircle className="w-3.5 h-3.5 text-green-500" />;
      case 'error':
        return <XCircle className="w-3.5 h-3.5 text-red-500" />;
    }
  };

  const getStatusColor = () => {
    switch (agent.status) {
      case 'running':
        return 'bg-blue-50 border-blue-200';
      case 'completed':
        return 'bg-green-50 border-green-200';
      case 'error':
        return 'bg-red-50 border-red-200';
    }
  };

  const getDuration = () => {
    if (!agent.durationMs) return null;
    if (agent.durationMs < 1000) return `${agent.durationMs}ms`;
    return `${(agent.durationMs / 1000).toFixed(1)}s`;
  };

  const formatTokens = (tokens?: { input: number; output: number }) => {
    if (!tokens) return null;
    const total = tokens.input + tokens.output;
    if (total >= 1000) return `${(total / 1000).toFixed(1)}K`;
    return total.toString();
  };

  return (
    <div className="select-none">
      <div
        className={`flex items-center gap-2 px-3 py-2 rounded border cursor-pointer transition-colors ${getStatusColor()} ${
          depth > 0 ? 'ml-4' : ''
        }`}
        onClick={() => setIsExpanded(!isExpanded)}
      >
        {/* Expand/collapse indicator */}
        {(hasChildren || agent.summary) ? (
          isExpanded ? (
            <ChevronDown className="w-3 h-3 text-gray-400" />
          ) : (
            <ChevronRight className="w-3 h-3 text-gray-400" />
          )
        ) : (
          <div className="w-3" />
        )}

        {/* Status icon */}
        {getStatusIcon()}

        {/* Agent type */}
        <Bot className="w-3.5 h-3.5 text-purple-500" />
        <span className="text-xs font-medium text-gray-700">
          {agent.type}
        </span>

        {/* Metrics */}
        <div className="flex items-center gap-2 ml-auto text-xs text-gray-500">
          {agent.tokens && (
            <span className="flex items-center gap-1">
              <span className="font-mono">{formatTokens(agent.tokens)} tokens</span>
            </span>
          )}
          {agent.durationMs && (
            <span className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              <span className="font-mono">{getDuration()}</span>
            </span>
          )}
        </div>
      </div>

      {/* Expanded content */}
      {isExpanded && (
        <div className="ml-6 pl-2 border-l border-gray-200 mt-1">
          {/* Summary */}
          {agent.summary && (
            <div className="mb-2 p-2 bg-white rounded border border-gray-200">
              <div className="text-xs font-medium text-gray-500 mb-1">Summary</div>
              <p className="text-xs text-gray-700 whitespace-pre-wrap">
                {agent.summary}
              </p>
            </div>
          )}

          {/* Children */}
          {hasChildren && agent.children && (
            <div>
              <div className="text-xs font-medium text-gray-500 mb-1">
                Child Agents ({agent.children.length})
              </div>
              {agent.children.map(child => (
                <SubAgentNode
                  key={child.id}
                  agent={child}
                  depth={depth + 1}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default SubAgentLineage;

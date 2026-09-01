import React, { useState } from 'react';
import { ChevronDown, ChevronRight, Bot, Brain, MessageSquare, Wrench } from 'lucide-react';
import { MessageRenderer } from './MessageRenderer';
import { ToolRow } from './ToolRow';
import type { SubAgentEvent } from '../../hooks';

interface SubAgentViewProps {
  agent: SubAgentEvent;
}

export const SubAgentView: React.FC<SubAgentViewProps> = ({ agent }) => {
  const [expanded, setExpanded] = useState(true);
  const [showThinking, setShowThinking] = useState(false);
  const [showText, setShowText] = useState(true);
  const [showTools, setShowTools] = useState(true);

  const isRunning = agent.status === 'running';
  const hasThinking = agent.thinking && agent.thinking.length > 0;
  const hasText = agent.text && agent.text.length > 0;
  const hasTools = agent.tool_calls && agent.tool_calls.length > 0;

  return (
    <div className="rounded-lg border border-indigo-200 dark:border-indigo-800 bg-indigo-50/30 dark:bg-indigo-900/10 overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center gap-2 px-3 py-2 bg-indigo-100/50 dark:bg-indigo-900/30 cursor-pointer hover:bg-indigo-100 dark:hover:bg-indigo-900/50 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center justify-center w-6 h-6 rounded-full bg-indigo-500 text-white">
          <Bot className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-indigo-900 dark:text-indigo-100">
              {agent.agent_type}
            </span>
            {isRunning && (
              <span className="flex items-center gap-1">
                <span className="w-1.5 h-1.5 bg-indigo-500 rounded-full animate-pulse" />
                <span className="text-xs text-indigo-600 dark:text-indigo-400">Running...</span>
              </span>
            )}
            {agent.status === 'completed' && (
              <span className="text-xs text-green-600 dark:text-green-400">✓ Completed</span>
            )}
            {agent.status === 'error' && (
              <span className="text-xs text-red-600 dark:text-red-400">✗ Error</span>
            )}
          </div>
          <div className="text-xs text-indigo-600 dark:text-indigo-400 truncate">
            {agent.description}
          </div>
        </div>
        {expanded ? (
          <ChevronDown className="w-4 h-4 text-indigo-500" />
        ) : (
          <ChevronRight className="w-4 h-4 text-indigo-500" />
        )}
      </div>

      {/* Content */}
      {expanded && (
        <div className="p-3 space-y-3">
          {/* Thinking section */}
          {hasThinking && (
            <div className="rounded-md border border-purple-200 dark:border-purple-800 overflow-hidden">
              <div
                className="flex items-center gap-2 px-2.5 py-1.5 bg-purple-50 dark:bg-purple-900/20 cursor-pointer hover:opacity-80 transition-opacity"
                onClick={() => setShowThinking(!showThinking)}
              >
                <Brain className="w-3.5 h-3.5 text-purple-500" />
                <span className="text-xs font-medium text-purple-700 dark:text-purple-300">
                  Thinking
                </span>
                {showThinking ? (
                  <ChevronDown className="w-3 h-3 text-purple-400 ml-auto" />
                ) : (
                  <ChevronRight className="w-3 h-3 text-purple-400 ml-auto" />
                )}
              </div>
              {showThinking && (
                <div className="px-2.5 py-2 border-t border-purple-200 dark:border-purple-800 bg-white dark:bg-gray-900">
                  <div className="text-xs text-gray-600 dark:text-gray-400 whitespace-pre-wrap font-mono">
                    {agent.thinking}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Text section */}
          {hasText && (
            <div className="rounded-md border border-gray-200 dark:border-gray-700 overflow-hidden">
              <div
                className="flex items-center gap-2 px-2.5 py-1.5 bg-gray-50 dark:bg-gray-800/50 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
                onClick={() => setShowText(!showText)}
              >
                <MessageSquare className="w-3.5 h-3.5 text-gray-500" />
                <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
                  Response
                </span>
                {showText ? (
                  <ChevronDown className="w-3 h-3 text-gray-400 ml-auto" />
                ) : (
                  <ChevronRight className="w-3 h-3 text-gray-400 ml-auto" />
                )}
              </div>
              {showText && (
                <div className="px-2.5 py-2 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
                  <MessageRenderer content={agent.text || ''} isStreaming={isRunning} />
                </div>
              )}
            </div>
          )}

          {/* Tools section */}
          {hasTools && (
            <div className="rounded-md border border-gray-200 dark:border-gray-700 overflow-hidden">
              <div
                className="flex items-center gap-2 px-2.5 py-1.5 bg-gray-50 dark:bg-gray-800/50 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
                onClick={() => setShowTools(!showTools)}
              >
                <Wrench className="w-3.5 h-3.5 text-gray-500" />
                <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
                  Tools ({agent.tool_calls.length})
                </span>
                {showTools ? (
                  <ChevronDown className="w-3 h-3 text-gray-400 ml-auto" />
                ) : (
                  <ChevronRight className="w-3 h-3 text-gray-400 ml-auto" />
                )}
              </div>
              {showTools && (
                <div className="p-2 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 space-y-1">
                  {agent.tool_calls.map((call) => (
                    <ToolRow key={call.call_id} call={call} />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Summary */}
          {agent.summary && (
            <div className="rounded-md border border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20 p-2.5">
              <div className="text-xs font-medium text-green-700 dark:text-green-300 mb-1">
                Summary
              </div>
              <div className="text-xs text-green-600 dark:text-green-400 whitespace-pre-wrap">
                {agent.summary}
              </div>
            </div>
          )}

          {/* Stats */}
          {(agent.tokens !== undefined || agent.duration_ms !== undefined) && (
            <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
              {agent.tokens !== undefined && (
                <span>{agent.tokens.toLocaleString()} tokens</span>
              )}
              {agent.duration_ms !== undefined && (
                <span>{(agent.duration_ms / 1000).toFixed(1)}s</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default SubAgentView;

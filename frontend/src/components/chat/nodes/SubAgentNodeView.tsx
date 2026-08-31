import { memo, useState } from 'react';
import { Bot, ChevronDown, ChevronRight, Brain } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ToolRow } from '../ToolRow';
import type { SubAgentNode, ToolCallNode } from './types';
import type { ToolCallEvent } from '../../../hooks';

interface SubAgentNodeViewProps {
  node: SubAgentNode;
}

const formatDuration = (ms?: number): string => {
  if (ms === undefined) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
};

const formatTokens = (tokens?: number): string => {
  if (tokens === undefined) return '';
  if (tokens < 1000) return String(tokens);
  return `${(tokens / 1000).toFixed(1)}K`;
};

export const SubAgentNodeView = memo(function SubAgentNodeView({ node }: SubAgentNodeViewProps) {
  const [expanded, setExpanded] = useState(node.status === 'running');
  const [showThinking, setShowThinking] = useState(false);

  const isRunning = node.status === 'running';
  const isError = node.status === 'error';

  return (
    <div className="flex justify-start">
      <div className="max-w-2xl w-full">
        <div className={`rounded-lg border overflow-hidden transition-colors ${
          isRunning ? 'border-purple-300 bg-purple-50/30' :
          isError ? 'border-red-300 bg-red-50/30' :
          'border-gray-200 bg-gray-50/30'
        }`}>
          {/* Header */}
          <div
            className={`flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors ${
              isRunning ? 'hover:bg-purple-100/50' : 'hover:bg-gray-100/50'
            }`}
            onClick={() => setExpanded(!expanded)}
          >
            <div className="flex items-center justify-center w-5 h-5">
              {isRunning ? (
                <div className="w-2.5 h-2.5 bg-purple-500 rounded-full animate-pulse" />
              ) : isError ? (
                <div className="w-2.5 h-2.5 bg-red-500 rounded-full" />
              ) : (
                <Bot className="w-4 h-4 text-gray-500" />
              )}
            </div>
            <span className="text-sm font-medium text-gray-900">
              Sub-Agent: {node.agentType}
            </span>
            <span className="text-sm text-gray-500 truncate">
              {node.description}
            </span>
            <div className="flex items-center gap-2 ml-auto">
              {node.tokens !== undefined && (
                <span className="text-xs text-gray-400">{formatTokens(node.tokens)} tok</span>
              )}
              {node.durationMs !== undefined && (
                <span className="text-xs text-gray-400">{formatDuration(node.durationMs)}</span>
              )}
              {expanded ? (
                <ChevronDown className="w-4 h-4 text-gray-400" />
              ) : (
                <ChevronRight className="w-4 h-4 text-gray-400" />
              )}
            </div>
          </div>

          {/* Expanded content */}
          {expanded && (
            <div className="border-t border-gray-200 px-3 py-2 space-y-2">
              {/* Thinking section */}
              {node.thinking && (
                <div>
                  <button
                    onClick={(e) => { e.stopPropagation(); setShowThinking(!showThinking); }}
                    className="flex items-center gap-1.5 text-xs font-medium text-purple-600 hover:text-purple-700"
                  >
                    <Brain className="w-3 h-3" />
                    <span>Thinking</span>
                    {showThinking ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                  </button>
                  {showThinking && (
                    <pre className="mt-1 text-xs text-gray-600 bg-purple-50/50 rounded p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap font-mono">
                      {node.thinking}
                    </pre>
                  )}
                </div>
              )}

              {/* Text output */}
              {node.text && (
                <div className="prose prose-xs max-w-none">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {node.text}
                  </ReactMarkdown>
                </div>
              )}

              {/* Tool calls */}
              {node.toolCalls.length > 0 && (
                <div className="space-y-1">
                  {node.toolCalls.map(tc => (
                    <SubAgentToolRow key={tc.callId} node={tc} />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
});

const SubAgentToolRow = memo(function SubAgentToolRow({ node }: { node: ToolCallNode }) {
  const toolCall: ToolCallEvent = {
    call_id: node.callId,
    name: node.name,
    input: node.input,
    status: node.status === 'success' ? 'success' : node.status === 'error' ? 'error' : node.status === 'denied' ? 'denied' : 'pending',
    result: node.result,
    duration_ms: node.durationMs,
  };

  return <ToolRow call={toolCall} />;
});

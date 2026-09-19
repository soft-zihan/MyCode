import { useState, useEffect, useCallback } from 'react';
import { fetchSessionStats, fetchTokenBreakdown, SessionStats, TokenBreakdown as TokenBreakdownData } from '../../api/client';
import { McpPanel } from './McpPanel';

interface ContextPanelProps {
  sessionId: string | null;
}

const formatTokens = (n: number): string => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toString();
};

export function ContextPanel({ sessionId }: ContextPanelProps) {
  const [stats, setStats] = useState<SessionStats | null>(null);
  const [breakdown, setBreakdown] = useState<TokenBreakdownData | null>(null);
  const [loading, setLoading] = useState(false);

  const loadData = useCallback(async () => {
    if (!sessionId) return;
    setLoading(true);
    try {
      const [statsResult, breakdownResult] = await Promise.all([
        fetchSessionStats(sessionId),
        fetchTokenBreakdown(sessionId),
      ]);
      setStats(statsResult);
      setBreakdown(breakdownResult);
    } catch (err) {
      console.error('Failed to load token data:', err);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 5000);
    return () => clearInterval(interval);
  }, [loadData]);

  if (!sessionId) {
    return (
      <div className="p-4 text-center text-gray-400 text-sm">
        No session active
      </div>
    );
  }

  if (loading || !stats) {
    return (
      <div className="p-4 flex items-center justify-center">
        <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-500" />
      </div>
    );
  }

  const inputTokens = stats.input_tokens || 0;
  const outputTokens = stats.output_tokens || 0;
  const cachedTokens = stats.cached_tokens || 0;
  const contextWindow = stats.context_window || 128000;
  const contextUsed = stats.last_input_token_count || 0;
  const cachePercent = inputTokens > 0 ? Math.round((cachedTokens / inputTokens) * 100) : 0;
  const contextPercent = contextWindow > 0 ? Math.round((contextUsed / contextWindow) * 100) : 0;

  // 计算各部分占比
  const totalTokens = breakdown ? breakdown.total_tokens : 0;
  const systemPercent = breakdown && totalTokens > 0 ? Math.round((breakdown.system_tokens / totalTokens) * 100) : 0;
  const toolsPercent = breakdown && totalTokens > 0 ? Math.round((breakdown.tools_tokens / totalTokens) * 100) : 0;
  const messagesPercent = breakdown && totalTokens > 0 ? Math.round((breakdown.messages_tokens / totalTokens) * 100) : 0;

  return (
    <div className="flex-1 overflow-auto flex flex-col">
      {/* Token Summary */}
      <div className="px-3 py-2 border-b border-gray-200">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-gray-700">Token Usage</span>
          {cachedTokens > 0 && (
            <span className="text-[10px] px-1.5 py-0.5 bg-green-100 text-green-700 rounded">
              {cachePercent}% cache
            </span>
          )}
        </div>
        <div className="grid grid-cols-3 gap-2">
          <div className="text-center p-2 bg-blue-50 rounded">
            <div className="text-[10px] text-blue-600">Input</div>
            <div className="text-xs font-bold text-blue-900">{formatTokens(inputTokens)}</div>
          </div>
          <div className="text-center p-2 bg-green-50 rounded">
            <div className="text-[10px] text-green-600">Output</div>
            <div className="text-xs font-bold text-green-900">{formatTokens(outputTokens)}</div>
          </div>
          <div className="text-center p-2 bg-purple-50 rounded">
            <div className="text-[10px] text-purple-600">Cached</div>
            <div className="text-xs font-bold text-purple-900">{formatTokens(cachedTokens)}</div>
          </div>
        </div>
      </div>

      {/* Context Window */}
      <div className="px-3 py-2 border-b border-gray-200">
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs font-medium text-gray-600">Context Window</span>
          <span className="text-[10px] text-gray-500">{contextPercent}%</span>
        </div>
        <div className="w-full h-1.5 bg-gray-200 rounded-full overflow-hidden">
          <div
            className={`h-full transition-all ${
              contextPercent >= 90 ? 'bg-red-500' :
              contextPercent >= 70 ? 'bg-yellow-500' :
              'bg-green-500'
            }`}
            style={{ width: `${Math.min(contextPercent, 100)}%` }}
          />
        </div>
        <div className="flex items-center justify-between mt-0.5">
          <span className="text-[10px] text-gray-500">{formatTokens(contextUsed)}</span>
          <span className="text-[10px] text-gray-500">{formatTokens(contextWindow)}</span>
        </div>
      </div>

      {/* Context Breakdown - 从上到下 */}
      {breakdown && (
        <div className="px-3 py-2 border-b border-gray-200">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-gray-700">Context Composition</span>
            <span className="text-[10px] text-gray-500">{formatTokens(totalTokens)} tokens</span>
          </div>

          {/* Stacked bar */}
          <div className="w-full h-3 bg-gray-100 rounded-full overflow-hidden flex mb-3">
            {breakdown.system_tokens > 0 && (
              <div
                className="h-full bg-purple-500"
                style={{ width: `${systemPercent}%` }}
                title={`System: ${formatTokens(breakdown.system_tokens)} (${systemPercent}%)`}
              />
            )}
            {breakdown.tools_tokens > 0 && (
              <div
                className="h-full bg-amber-500"
                style={{ width: `${toolsPercent}%` }}
                title={`Tools: ${formatTokens(breakdown.tools_tokens)} (${toolsPercent}%)`}
              />
            )}
            {breakdown.messages_tokens > 0 && (
              <div
                className="h-full bg-blue-500"
                style={{ width: `${messagesPercent}%` }}
                title={`Messages: ${formatTokens(breakdown.messages_tokens)} (${messagesPercent}%)`}
              />
            )}
          </div>

          {/* Details */}
          <div className="space-y-2">
            {/* System Prompt */}
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded-sm bg-purple-500" />
              <span className="text-xs text-gray-600 flex-1">System Prompt</span>
              <span className="text-xs font-mono text-gray-900">{formatTokens(breakdown.system_tokens)}</span>
              <span className="text-[10px] text-gray-400 w-8 text-right">{systemPercent}%</span>
            </div>

            {/* Tools */}
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded-sm bg-amber-500" />
              <span className="text-xs text-gray-600 flex-1">
                Tools
                <span className="text-[10px] text-gray-400 ml-1">
                  ({breakdown.builtin_tool_count} built-in + {breakdown.mcp_tool_count} MCP)
                </span>
              </span>
              <span className="text-xs font-mono text-gray-900">{formatTokens(breakdown.tools_tokens)}</span>
              <span className="text-[10px] text-gray-400 w-8 text-right">{toolsPercent}%</span>
            </div>

            {/* Messages */}
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded-sm bg-blue-500" />
              <span className="text-xs text-gray-600 flex-1">
                Messages
                <span className="text-[10px] text-gray-400 ml-1">({breakdown.message_count} msgs)</span>
              </span>
              <span className="text-xs font-mono text-gray-900">{formatTokens(breakdown.messages_tokens)}</span>
              <span className="text-[10px] text-gray-400 w-8 text-right">{messagesPercent}%</span>
            </div>
          </div>
        </div>
      )}

      {/* MCP Services */}
      <div>
        <div className="px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-50 border-b border-gray-200">
          MCP Services
        </div>
        <McpPanel />
      </div>
    </div>
  );
}

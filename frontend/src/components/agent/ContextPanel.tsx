import { useState, useEffect, useCallback } from 'react';
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts';
import { fetchSessionStats, fetchTokenBreakdown, SessionStats, TokenBreakdown as TokenBreakdownData } from '../../api/client';
import { McpPanel } from './McpPanel';

interface ContextPanelProps {
  sessionId: string | null;
}

const COLORS = {
  system: '#8b5cf6',
  tools: '#f59e0b',
  messages: '#3b82f6',
  context_files: '#10b981',
};

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

  const chartData = breakdown ? [
    { name: 'System', value: breakdown.system_tokens, color: COLORS.system },
    { name: 'Tools', value: breakdown.tools_tokens, color: COLORS.tools },
    { name: 'Messages', value: breakdown.messages_tokens, color: COLORS.messages },
    { name: 'Context Files', value: breakdown.context_files_tokens, color: COLORS.context_files },
  ].filter(d => d.value > 0) : [];

  const totalBreakdown = chartData.reduce((sum, d) => sum + d.value, 0);

  return (
    <div className="flex-1 overflow-auto flex flex-col">
      {/* Token Summary */}
      <div className="px-3 py-2 border-b border-gray-200">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-gray-700">Token Usage</span>
          {cachedTokens > 0 && (
            <span className="text-[10px] px-1.5 py-0.5 bg-green-100 text-green-700 rounded">
              {cachePercent}% cache hit
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

      {/* Context Breakdown */}
      {chartData.length > 0 && (
        <div className="px-3 py-2 border-b border-gray-200">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-gray-700">Context Breakdown</span>
            <span className="text-[10px] text-gray-500">{formatTokens(totalBreakdown)} tokens</span>
          </div>

          <div className="flex items-center gap-3">
            <div className="w-20 h-20">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={chartData}
                    cx="50%"
                    cy="50%"
                    innerRadius={18}
                    outerRadius={35}
                    dataKey="value"
                  >
                    {chartData.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip
                    formatter={(value) => formatTokens(Number(value))}
                    contentStyle={{ fontSize: '10px', padding: '2px 6px' }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>

            <div className="flex-1 space-y-1">
              {chartData.map((item) => (
                <div key={item.name} className="flex items-center justify-between text-[11px]">
                  <div className="flex items-center gap-1.5">
                    <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: item.color }} />
                    <span className="text-gray-600">{item.name}</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className="font-mono text-gray-900">{formatTokens(item.value)}</span>
                    <span className="text-gray-400 w-8 text-right">
                      {totalBreakdown > 0 ? Math.round((item.value / totalBreakdown) * 100) : 0}%
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Context Files List */}
      {breakdown && breakdown.context_files && breakdown.context_files.length > 0 && (
        <div className="px-3 py-2 border-b border-gray-200">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs font-medium text-gray-700">Context Files</span>
            <span className="text-[10px] text-gray-500">{breakdown.context_files.length} files</span>
          </div>
          <div className="space-y-0.5 max-h-24 overflow-auto">
            {breakdown.context_files.map((file, i) => (
              <div key={i} className="flex items-center justify-between text-[10px] py-0.5">
                <span className="text-gray-600 truncate flex-1 mr-2">{file.path}</span>
                <span className="text-gray-400 font-mono">{formatTokens(file.tokens)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Messages Count */}
      {breakdown && (
        <div className="px-3 py-2 border-b border-gray-200">
          <div className="flex items-center justify-between text-xs">
            <span className="text-gray-600">Messages</span>
            <span className="font-medium text-gray-900">{breakdown.message_count}</span>
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

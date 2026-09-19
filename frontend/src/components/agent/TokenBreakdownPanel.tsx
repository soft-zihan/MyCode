import { useState, useEffect, useCallback } from 'react';
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts';
import { fetchSessionStats, fetchTokenBreakdown, SessionStats, TokenBreakdown as TokenBreakdownData } from '../../api/client';

interface TokenBreakdownProps {
  sessionId: string | null;
  compact?: boolean;
}

const COLORS = {
  system: '#8b5cf6',
  tools: '#f59e0b',
  messages: '#3b82f6',
};

const formatTokens = (n: number): string => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toString();
};

export function TokenBreakdownPanel({ sessionId, compact = false }: TokenBreakdownProps) {
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
  }, [loadData]);

  // Compact mode: just show token counts inline
  if (compact) {
    if (!sessionId || !stats) {
      return (
        <div className="px-3 py-2 text-xs text-gray-400">
          No session active
        </div>
      );
    }
    return (
      <div className="px-3 py-2 flex items-center gap-3 text-xs">
        <span className="text-gray-500">
          <span className="font-medium text-gray-700">{formatTokens(stats.input_tokens)}</span> in
        </span>
        <span className="text-gray-300">|</span>
        <span className="text-gray-500">
          <span className="font-medium text-gray-700">{formatTokens(stats.output_tokens)}</span> out
        </span>
        {stats.cached_tokens && stats.cached_tokens > 0 && (
          <>
            <span className="text-gray-300">|</span>
            <span className="text-green-600">
              {Math.round((stats.cached_tokens / stats.input_tokens) * 100)}% cache
            </span>
          </>
        )}
      </div>
    );
  }

  if (!sessionId) {
    return (
      <div className="p-4 text-center text-gray-400 text-sm">
        Start a session to view token usage
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

  // 计算 system tokens 总和
  const systemTokens = breakdown ? (
    breakdown.base_prompt_tokens + breakdown.claude_md_tokens + breakdown.skills_tokens +
    breakdown.memory_tokens + breakdown.wiki_tokens + breakdown.agents_tokens
  ) : 0;

  const chartData = breakdown ? [
    { name: 'System', value: systemTokens, color: COLORS.system },
    { name: 'Tools', value: breakdown.tools_tokens, color: COLORS.tools },
    { name: 'Messages', value: breakdown.messages_tokens, color: COLORS.messages },
  ].filter(d => d.value > 0) : [];

  const totalBreakdown = chartData.reduce((sum, d) => sum + d.value, 0);

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-700">Token Usage</h3>
      </div>

      <div className="space-y-3">
        <div className="flex items-center justify-between py-2 px-3 bg-blue-50 rounded-lg">
          <span className="text-xs font-medium text-blue-700">Input Tokens</span>
          <span className="text-sm font-bold text-blue-900">{formatTokens(inputTokens)}</span>
        </div>

        <div className="flex items-center justify-between py-2 px-3 bg-green-50 rounded-lg">
          <span className="text-xs font-medium text-green-700">Output Tokens</span>
          <span className="text-sm font-bold text-green-900">{formatTokens(outputTokens)}</span>
        </div>

        {cachedTokens > 0 && (
          <div className="flex items-center justify-between py-2 px-3 bg-purple-50 rounded-lg">
            <span className="text-xs font-medium text-purple-700">Cached Tokens</span>
            <span className="text-sm font-bold text-purple-900">{formatTokens(cachedTokens)} ({cachePercent}%)</span>
          </div>
        )}
      </div>

      <div className="pt-3 border-t border-gray-200">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-gray-600">Context Window</span>
          <span className="text-xs text-gray-500">{contextPercent}%</span>
        </div>
        <div className="w-full h-2 bg-gray-200 rounded-full overflow-hidden">
          <div
            className={`h-full transition-all ${
              contextPercent >= 90 ? 'bg-red-500' :
              contextPercent >= 70 ? 'bg-yellow-500' :
              'bg-green-500'
            }`}
            style={{ width: `${Math.min(contextPercent, 100)}%` }}
          />
        </div>
        <div className="flex items-center justify-between mt-1">
          <span className="text-[10px] text-gray-500">{formatTokens(contextUsed)}</span>
          <span className="text-[10px] text-gray-500">{formatTokens(contextWindow)}</span>
        </div>
      </div>

      <div className="pt-3 border-t border-gray-200">
        <div className="flex items-center justify-between text-xs text-gray-600">
          <span>Total</span>
          <span className="font-semibold">{formatTokens(inputTokens + outputTokens)}</span>
        </div>
      </div>

      {chartData.length > 0 && (
        <div className="pt-4 border-t border-gray-200">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-medium text-gray-600">Token Breakdown</span>
            <span className="text-xs text-gray-500">{formatTokens(totalBreakdown)} tokens</span>
          </div>

          <div className="flex items-center gap-4">
            <div className="w-24 h-24">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={chartData}
                    cx="50%"
                    cy="50%"
                    innerRadius={20}
                    outerRadius={40}
                    dataKey="value"
                  >
                    {chartData.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip
                    formatter={(value) => formatTokens(Number(value))}
                    contentStyle={{ fontSize: '11px', padding: '4px 8px' }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>

            <div className="flex-1 space-y-1.5">
              {chartData.map((item) => (
                <div key={item.name} className="flex items-center justify-between text-xs">
                  <div className="flex items-center gap-2">
                    <div className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: item.color }} />
                    <span className="text-gray-600">{item.name}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-gray-900">{formatTokens(item.value)}</span>
                    <span className="text-gray-400 w-10 text-right">
                      {totalBreakdown > 0 ? Math.round((item.value / totalBreakdown) * 100) : 0}%
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

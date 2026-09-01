import React, { useState, useEffect } from 'react';
import { BarChart3, RefreshCw, Zap, Clock, Cpu } from 'lucide-react';
import { getSessionStats } from '../../api/client';

interface SessionStats {
  session_id: string;
  model: string;
  total_input_tokens: number;
  total_output_tokens: number;
  current_turns: number;
  tool_execution_stats: Record<string, { count: number; total_ms: number }>;
}

interface StatsPanelProps {
  sessionId: string | null;
}

export const StatsPanel: React.FC<StatsPanelProps> = ({ sessionId }) => {
  const [stats, setStats] = useState<SessionStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStats = async () => {
    if (!sessionId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getSessionStats(sessionId);
      setStats(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load stats');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadStats();
  }, [sessionId]);

  if (!sessionId) return null;

  if (loading && !stats) {
    return (
      <div className="p-4 text-center text-gray-500 text-sm">
        Loading stats...
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 text-center text-red-500 text-sm">
        {error}
      </div>
    );
  }

  if (!stats) return null;

  const totalTokens = stats.total_input_tokens + stats.total_output_tokens;
  const inputPercent = totalTokens > 0 ? (stats.total_input_tokens / totalTokens) * 100 : 0;
  const outputPercent = totalTokens > 0 ? (stats.total_output_tokens / totalTokens) * 100 : 0;

  const toolStats = Object.entries(stats.tool_execution_stats)
    .sort((a, b) => b[1].total_ms - a[1].total_ms)
    .slice(0, 5);

  const maxToolTime = toolStats.length > 0 ? toolStats[0][1].total_ms : 1;

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900 flex items-center gap-2">
          <BarChart3 className="w-4 h-4 text-blue-500" />
          Session Stats
        </h3>
        <button
          onClick={loadStats}
          className="p-1 text-gray-500 hover:text-gray-700"
          title="Refresh"
        >
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Token Usage */}
      <div className="space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-600">Token Usage</span>
          <span className="font-mono text-gray-900">{totalTokens.toLocaleString()}</span>
        </div>
        <div className="h-2 bg-gray-100 rounded-full overflow-hidden flex">
          <div
            className="bg-blue-500 transition-all"
            style={{ width: `${inputPercent}%` }}
            title={`Input: ${stats.total_input_tokens.toLocaleString()}`}
          />
          <div
            className="bg-green-500 transition-all"
            style={{ width: `${outputPercent}%` }}
            title={`Output: ${stats.total_output_tokens.toLocaleString()}`}
          />
        </div>
        <div className="flex items-center justify-between text-xs text-gray-500">
          <div className="flex items-center gap-1">
            <div className="w-2 h-2 bg-blue-500 rounded-full" />
            <span>Input: {stats.total_input_tokens.toLocaleString()}</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="w-2 h-2 bg-green-500 rounded-full" />
            <span>Output: {stats.total_output_tokens.toLocaleString()}</span>
          </div>
        </div>
      </div>

      {/* Quick Stats */}
      <div className="grid grid-cols-2 gap-2">
        <div className="bg-blue-50 rounded-lg p-2">
          <div className="flex items-center gap-1 text-xs text-blue-600 mb-1">
            <Clock className="w-3 h-3" />
            <span>Turns</span>
          </div>
          <div className="text-lg font-bold text-blue-900">{stats.current_turns}</div>
        </div>
        <div className="bg-purple-50 rounded-lg p-2">
          <div className="flex items-center gap-1 text-xs text-purple-600 mb-1">
            <Cpu className="w-3 h-3" />
            <span>Model</span>
          </div>
          <div className="text-xs font-mono text-purple-900 truncate" title={stats.model}>
            {stats.model.split('/').pop() || stats.model}
          </div>
        </div>
      </div>

      {/* Tool Execution Stats */}
      {toolStats.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-xs">
            <span className="text-gray-600 flex items-center gap-1">
              <Zap className="w-3 h-3" />
              Top Tools
            </span>
          </div>
          <div className="space-y-1.5">
            {toolStats.map(([toolName, toolData]) => (
              <div key={toolName}>
                <div className="flex items-center justify-between text-xs mb-0.5">
                  <span className="text-gray-700 font-mono truncate max-w-[120px]" title={toolName}>
                    {toolName}
                  </span>
                  <span className="text-gray-500">
                    {toolData.count}x · {(toolData.total_ms / 1000).toFixed(1)}s
                  </span>
                </div>
                <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                  <div
                    className="bg-orange-400 transition-all"
                    style={{ width: `${(toolData.total_ms / maxToolTime) * 100}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default StatsPanel;

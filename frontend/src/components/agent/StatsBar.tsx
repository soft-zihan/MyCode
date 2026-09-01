import React from 'react';

export interface AgentStats {
  input_tokens: number;
  output_tokens: number;
  ttft_ms: number;
  tokens_per_sec: number;
}

interface StatsBarProps {
  stats: AgentStats | null;
}

const formatDuration = (ms: number): string => {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
};

const formatTokens = (tokens: number): string => {
  if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
  return `${tokens}`;
};

export const StatsBar: React.FC<StatsBarProps> = ({ stats }) => {
  if (!stats) return null;

  const inputTokens = stats.input_tokens ?? 0;
  const outputTokens = stats.output_tokens ?? 0;
  const ttftMs = stats.ttft_ms ?? 0;
  const tps = stats.tokens_per_sec ?? 0;

  return (
    <div className="flex items-center gap-4 px-4 py-1.5 bg-gray-50 dark:bg-gray-800 border-t border-gray-200 dark:border-gray-700 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="text-gray-400 dark:text-gray-500">Input</span>
        <span className="font-mono text-gray-600 dark:text-gray-300">{formatTokens(inputTokens)}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <span className="text-gray-400 dark:text-gray-500">Output</span>
        <span className="font-mono text-gray-600 dark:text-gray-300">{formatTokens(outputTokens)}</span>
      </div>
      {ttftMs > 0 && (
        <div className="flex items-center gap-1.5">
          <span className="text-gray-400 dark:text-gray-500">TTFT</span>
          <span className="font-mono text-gray-600 dark:text-gray-300">{formatDuration(ttftMs)}</span>
        </div>
      )}
      {tps > 0 && (
        <div className="flex items-center gap-1.5">
          <span className="text-gray-400 dark:text-gray-500">Speed</span>
          <span className="font-mono text-gray-600 dark:text-gray-300">{tps.toFixed(0)} tok/s</span>
        </div>
      )}
    </div>
  );
};

export default StatsBar;

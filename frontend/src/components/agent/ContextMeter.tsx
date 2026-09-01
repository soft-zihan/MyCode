import React from 'react';

export interface ContextInfo {
  used_tokens: number;
  total_tokens: number;
  occupancy_percent: number;
}

interface ContextMeterProps {
  context: ContextInfo | null;
}

export const ContextMeter: React.FC<ContextMeterProps> = ({ context }) => {
  if (!context) {
    return null;
  }

  const usedTokens = context.used_tokens ?? 0;
  const totalTokens = context.total_tokens ?? 0;
  const occupancyPercent = context.occupancy_percent ?? 0;

  const formatTokens = (tokens: number): string => {
    if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
    if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
    return `${tokens}`;
  };

  const getBarColor = (percent: number): string => {
    if (percent >= 90) return 'bg-red-500';
    if (percent >= 70) return 'bg-yellow-500';
    return 'bg-green-500';
  };

  return (
    <div className="flex items-center gap-2 px-4 py-2 bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 text-xs">
      <span className="text-gray-500 dark:text-gray-400">Context:</span>
      <div className="flex-1 max-w-xs h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div
          className={`h-full ${getBarColor(occupancyPercent)} transition-all duration-300`}
          style={{ width: `${Math.min(occupancyPercent, 100)}%` }}
        />
      </div>
      <span className="font-mono text-gray-600 dark:text-gray-300">
        {formatTokens(usedTokens)} / {formatTokens(totalTokens)}
      </span>
      <span className="font-mono text-gray-500 dark:text-gray-400">
        ({occupancyPercent.toFixed(0)}%)
      </span>
    </div>
  );
};

export default ContextMeter;

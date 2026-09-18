import React from 'react';
import { Clock, Cpu, Database } from 'lucide-react';

interface StatsBarProps {
  turns?: number;
  steps?: number;
  tokensPerSec?: number;
  inputTokens?: number;
  outputTokens?: number;
  cachedTokens?: number;
  contextUsed?: number;
  contextTotal?: number;
}

const formatTokens = (tokens: number): string => {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
  return `${tokens}`;
};

export const StatsBar: React.FC<StatsBarProps> = ({
  turns,
  steps,
  tokensPerSec,
  inputTokens,
  outputTokens,
  cachedTokens,
  contextUsed,
  contextTotal,
}) => {
  const hasAnyData = turns !== undefined || inputTokens !== undefined || contextUsed !== undefined;
  if (!hasAnyData) return null;

  const cacheHitRate = inputTokens && cachedTokens
    ? Math.round((cachedTokens / inputTokens) * 100)
    : 0;

  const contextPercent = contextUsed && contextTotal
    ? Math.round((contextUsed / contextTotal) * 100)
    : 0;

  return (
    <div className="stats-bar">
      <div className="stats-bar-group">
        {turns !== undefined && steps !== undefined && (
          <>
            <Clock className="w-3 h-3 text-gray-400" />
            <span className="stats-bar-item">{turns} turns</span>
            <span className="stats-bar-sep" />
            <span className="stats-bar-item">{steps} steps</span>
            <span className="stats-bar-sep" />
          </>
        )}
        {tokensPerSec !== undefined && (
          <>
            <span className="stats-bar-item">{tokensPerSec.toFixed(0)} tok/s</span>
            <span className="stats-bar-sep" />
          </>
        )}
      </div>

      <div className="stats-bar-group">
        {inputTokens !== undefined && (
          <>
            <Cpu className="w-3 h-3 text-gray-400" />
            <span className="stats-bar-item">{formatTokens(inputTokens)} in</span>
            {outputTokens !== undefined && (
              <>
                <span className="stats-bar-sep" />
                <span className="stats-bar-item">{formatTokens(outputTokens)} out</span>
              </>
            )}
            {cachedTokens !== undefined && cachedTokens > 0 && (
              <>
                <span className="stats-bar-sep" />
                <span className="stats-bar-item text-green-600">
                  cache {cacheHitRate}%
                </span>
              </>
            )}
          </>
        )}
      </div>

      {contextUsed !== undefined && contextTotal !== undefined && (
        <div className="stats-bar-group">
          <Database className="w-3 h-3 text-gray-400" />
          <div className="stats-bar-context-bar">
            <div
              className={`stats-bar-context-fill ${
                contextPercent >= 90 ? 'bg-red-500' :
                contextPercent >= 70 ? 'bg-yellow-500' : 'bg-green-500'
              }`}
              style={{ width: `${Math.min(contextPercent, 100)}%` }}
            />
          </div>
          <span className="stats-bar-item">{contextPercent}%</span>
        </div>
      )}
    </div>
  );
};

export default StatsBar;

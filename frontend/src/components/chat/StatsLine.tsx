import React from 'react';
import { Clock } from 'lucide-react';

interface StatsLineProps {
  turns?: number;
  steps?: number;
  llmMs?: number;
  toolMs?: number;
  ttftMs?: number;
  tokensPerSec?: number;
  inputTokens?: number;
  outputTokens?: number;
}

export const StatsLine: React.FC<StatsLineProps> = ({
  turns,
  steps,
  llmMs,
  toolMs,
  ttftMs,
  tokensPerSec,
  inputTokens,
  outputTokens,
}) => {
  const groups: string[] = [];

  if (turns !== undefined && steps !== undefined) {
    groups.push(`${turns} turns · ${steps} steps`);
  }

  if (llmMs !== undefined) {
    groups.push(`LLM ${(llmMs / 1000).toFixed(1)}s`);
  }

  if (toolMs !== undefined) {
    groups.push(`Tools ${(toolMs / 1000).toFixed(1)}s`);
  }

  if (ttftMs !== undefined) {
    groups.push(`TTFT ${ttftMs}ms`);
  }

  if (tokensPerSec !== undefined) {
    groups.push(`${tokensPerSec.toFixed(1)} tok/s`);
  }

  if (inputTokens !== undefined && outputTokens !== undefined) {
    const totalTokens = inputTokens + outputTokens;
    groups.push(formatTokens(totalTokens));
  }

  if (groups.length === 0) return null;

  return (
    <div className="flex items-center gap-2 px-4 py-1.5 text-xs text-gray-500 border-t border-gray-100 bg-gray-50/50">
      <div className="flex items-center gap-1.5">
        <Clock className="w-3 h-3" />
        <span className="font-mono">{groups[0]}</span>
      </div>
      {groups.slice(1).map((group, i) => (
        <React.Fragment key={i}>
          <span className="text-gray-300">|</span>
          <span className="font-mono">{group}</span>
        </React.Fragment>
      ))}
    </div>
  );
};

function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) {
    return `${(tokens / 1_000_000).toFixed(1)}M tokens`;
  }
  if (tokens >= 1_000) {
    return `${(tokens / 1_000).toFixed(1)}K tokens`;
  }
  return `${tokens} tokens`;
}

export default StatsLine;

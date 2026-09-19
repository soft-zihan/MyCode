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

interface BreakdownItemProps {
  label: string;
  tokens: number;
  totalTokens: number;
  color: string;
  indent?: number;
  detail?: string;
}

function BreakdownItem({ label, tokens, totalTokens, color, indent = 0, detail }: BreakdownItemProps) {
  const percent = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0;
  return (
    <div className="flex items-center gap-2 py-0.5" style={{ paddingLeft: `${indent * 12}px` }}>
      <div className="w-2.5 h-2.5 rounded-sm flex-shrink-0" style={{ backgroundColor: color }} />
      <span className="text-xs text-gray-600 flex-1 truncate">{label}</span>
      {detail && <span className="text-[10px] text-gray-400">{detail}</span>}
      <span className="text-xs font-mono text-gray-900 w-12 text-right">{formatTokens(tokens)}</span>
      <span className="text-[10px] text-gray-400 w-8 text-right">{percent}%</span>
    </div>
  );
}

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
    // 只在 session 切换时刷新，不自动轮询
  }, [sessionId]);

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

  const totalTokens = breakdown ? breakdown.total_tokens : 0;

  // 计算各部分占比
  const systemTotal = breakdown ? (
    breakdown.base_prompt_tokens + breakdown.claude_md_tokens + breakdown.skills_tokens +
    breakdown.memory_tokens + breakdown.wiki_tokens + breakdown.agents_tokens
  ) : 0;

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

      {/* Context Composition - 层次结构 */}
      {breakdown && (
        <div className="px-3 py-2 border-b border-gray-200">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-gray-700">Context Composition</span>
            <span className="text-[10px] text-gray-500">{formatTokens(totalTokens)} tokens</span>
          </div>

          {/* Stacked bar */}
          <div className="w-full h-3 bg-gray-100 rounded-full overflow-hidden flex mb-3">
            {systemTotal > 0 && (
              <div
                className="h-full bg-purple-500"
                style={{ width: `${(systemTotal / totalTokens) * 100}%` }}
                title={`System: ${formatTokens(systemTotal)}`}
              />
            )}
            {breakdown.tools_tokens > 0 && (
              <div
                className="h-full bg-amber-500"
                style={{ width: `${(breakdown.tools_tokens / totalTokens) * 100}%` }}
                title={`Tools: ${formatTokens(breakdown.tools_tokens)}`}
              />
            )}
            {breakdown.messages_tokens > 0 && (
              <div
                className="h-full bg-blue-500"
                style={{ width: `${(breakdown.messages_tokens / totalTokens) * 100}%` }}
                title={`Messages: ${formatTokens(breakdown.messages_tokens)}`}
              />
            )}
          </div>

          {/* 层次结构详情 */}
          <div className="space-y-1">
            {/* Plan Mode 提示 */}
            {breakdown.is_plan_mode && breakdown.plan_mode_tokens > 0 && (
              <div className="flex items-center gap-2 py-1 px-2 mb-2 bg-orange-50 border border-orange-200 rounded">
                <div className="w-3 h-3 rounded-sm bg-orange-500" />
                <span className="text-xs text-orange-700 flex-1">Plan Mode</span>
                <span className="text-xs font-mono text-orange-900">{formatTokens(breakdown.plan_mode_tokens)}</span>
                <span className="text-[10px] text-orange-600 w-8 text-right">{totalTokens > 0 ? Math.round((breakdown.plan_mode_tokens / totalTokens) * 100) : 0}%</span>
              </div>
            )}

            {/* Layer 1: System Prompt */}
            <div className="flex items-center gap-2 py-0.5 font-medium">
              <div className="w-3 h-3 rounded-sm bg-purple-500" />
              <span className="text-xs text-gray-700 flex-1">System Prompt</span>
              <span className="text-xs font-mono text-gray-900">{formatTokens(systemTotal)}</span>
              <span className="text-[10px] text-gray-400 w-8 text-right">{totalTokens > 0 ? Math.round((systemTotal / totalTokens) * 100) : 0}%</span>
            </div>
            <BreakdownItem label="Base Prompt" tokens={breakdown.base_prompt_tokens} totalTokens={totalTokens} color="#a855f7" indent={1} />
            <BreakdownItem label="CLAUDE.md" tokens={breakdown.claude_md_tokens} totalTokens={totalTokens} color="#c084fc" indent={1} detail="项目规则" />
            <BreakdownItem label="Skills" tokens={breakdown.skills_tokens} totalTokens={totalTokens} color="#d8b4fe" indent={1} />
            <BreakdownItem label="Memory" tokens={breakdown.memory_tokens} totalTokens={totalTokens} color="#e9d5ff" indent={1} detail="跨会话" />
            <BreakdownItem label="Wiki" tokens={breakdown.wiki_tokens} totalTokens={totalTokens} color="#f3e8ff" indent={1} />
            <BreakdownItem label="Agents" tokens={breakdown.agents_tokens} totalTokens={totalTokens} color="#faf5ff" indent={1} detail="子代理" />

            {/* Layer 2: Tools */}
            <div className="flex items-center gap-2 py-0.5 font-medium mt-2">
              <div className="w-3 h-3 rounded-sm bg-amber-500" />
              <span className="text-xs text-gray-700 flex-1">Tools</span>
              <span className="text-xs font-mono text-gray-900">{formatTokens(breakdown.tools_tokens)}</span>
              <span className="text-[10px] text-gray-400 w-8 text-right">{totalTokens > 0 ? Math.round((breakdown.tools_tokens / totalTokens) * 100) : 0}%</span>
            </div>
            {(breakdown.builtin_tool_count > 0 || breakdown.mcp_tool_count > 0) ? (
              <>
                <BreakdownItem 
                  label="Built-in" 
                  tokens={Math.round(breakdown.tools_tokens * (breakdown.builtin_tool_count / (breakdown.builtin_tool_count + breakdown.mcp_tool_count)))}
                  totalTokens={totalTokens} 
                  color="#f59e0b" 
                  indent={1} 
                  detail={`${breakdown.builtin_tool_count} 个`}
                />
                <BreakdownItem 
                  label="MCP" 
                  tokens={Math.round(breakdown.tools_tokens * (breakdown.mcp_tool_count / (breakdown.builtin_tool_count + breakdown.mcp_tool_count)))}
                  totalTokens={totalTokens} 
                  color="#fbbf24" 
                  indent={1} 
                  detail={`${breakdown.mcp_tool_count} 个`}
                />
              </>
            ) : (
              <BreakdownItem 
                label="Tool Definitions" 
                tokens={breakdown.tools_tokens}
                totalTokens={totalTokens} 
                color="#f59e0b" 
                indent={1} 
                detail="估算"
              />
            )}

            {/* Layer 3: Messages */}
            <div className="flex items-center gap-2 py-0.5 font-medium mt-2">
              <div className="w-3 h-3 rounded-sm bg-blue-500" />
              <span className="text-xs text-gray-700 flex-1">Messages</span>
              <span className="text-xs font-mono text-gray-900">{formatTokens(breakdown.messages_tokens)}</span>
              <span className="text-[10px] text-gray-400 w-8 text-right">{totalTokens > 0 ? Math.round((breakdown.messages_tokens / totalTokens) * 100) : 0}%</span>
            </div>
            <BreakdownItem label="User" tokens={breakdown.user_tokens} totalTokens={totalTokens} color="#3b82f6" indent={1} detail={`${breakdown.message_count} 条`} />
            <BreakdownItem label="Assistant" tokens={breakdown.assistant_tokens} totalTokens={totalTokens} color="#60a5fa" indent={1} />
            <BreakdownItem label="Tool Results" tokens={breakdown.tool_tokens} totalTokens={totalTokens} color="#93c5fd" indent={1} />
            {/* 按工具名拆分的结果 */}
            {breakdown.tool_result_by_name && Object.keys(breakdown.tool_result_by_name).length > 0 && (
              <div className="ml-4 mt-1 space-y-0.5">
                {Object.entries(breakdown.tool_result_by_name)
                  .sort(([, a], [, b]) => b - a)
                  .map(([toolName, tokens]) => (
                    <BreakdownItem 
                      key={toolName}
                      label={toolName} 
                      tokens={tokens} 
                      totalTokens={totalTokens} 
                      color="#bfdbfe" 
                      indent={2} 
                    />
                  ))}
              </div>
            )}
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

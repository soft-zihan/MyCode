import { useSessionStore } from '../../store';
import { sessionStore } from '../../store';
import { DEFAULT_CONTEXT_WINDOW } from '../../api/client';
import { McpPanel } from './McpPanel';
import { PromptsPanel } from './PromptsPanel';

interface ContextPanelProps {
  sessionId: string | null;
  onFileSelect?: (path: string) => void;
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

export function ContextPanel({ sessionId, onFileSelect }: ContextPanelProps) {
  const stats = useSessionStore(() => sessionId ? sessionStore.getDetailedStats(sessionId) : null);
  const breakdown = useSessionStore(() => sessionId ? sessionStore.getBreakdown(sessionId) : null);
  const contextUsed = useSessionStore(() => sessionId ? sessionStore.getContextUsed(sessionId) : 0);
  const contextTotal = useSessionStore(() => sessionId ? sessionStore.getContextTotal(sessionId) : DEFAULT_CONTEXT_WINDOW);

  if (!sessionId) {
    return (
      <div className="p-4 text-center text-gray-400 text-sm">
        No session active
      </div>
    );
  }

  const inputTokens = stats?.inputTokens || 0;
  const outputTokens = stats?.outputTokens || 0;
  const cachedTokens = stats?.cachedTokens || 0;
  const contextWindow = contextTotal;
  const cachePercent = inputTokens > 0 ? Math.round((cachedTokens / inputTokens) * 100) : 0;
  const contextPercent = contextWindow > 0 ? Math.round((contextUsed / contextWindow) * 100) : 0;

  const totalTokens = breakdown ? breakdown.total_tokens : 0;

  const systemTotal = breakdown ? (
    breakdown.base_prompt_tokens + breakdown.claude_md_tokens + breakdown.skills_tokens +
    breakdown.memory_tokens + breakdown.wiki_tokens + breakdown.agents_tokens
  ) : 0;

  const toolResultByName: Record<string, number> = breakdown?.tool_result_by_name || {};

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
          <div className="text-center p-2 bg-indigo-50 rounded">
            <div className="text-[10px] text-indigo-600">Input</div>
            <div className="text-xs font-bold text-indigo-900">{formatTokens(inputTokens)}</div>
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

      {/* Context Composition */}
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
            {breakdown.tool_tokens > 0 && (
              <div
                className="h-full bg-amber-500"
                style={{ width: `${(breakdown.tool_tokens / totalTokens) * 100}%` }}
                title={`Tool Results: ${formatTokens(breakdown.tool_tokens)}`}
              />
            )}
            {breakdown.messages_tokens > 0 && (
              <div
                className="h-full bg-indigo-500"
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

            {/* Layer 2: Tool Results */}
            <div className="flex items-center gap-2 py-0.5 font-medium mt-2">
              <div className="w-3 h-3 rounded-sm bg-amber-500" />
              <span className="text-xs text-gray-700 flex-1">Tool Results</span>
              <span className="text-xs font-mono text-gray-900">{formatTokens(breakdown.tool_tokens)}</span>
              <span className="text-[10px] text-gray-400 w-8 text-right">{totalTokens > 0 ? Math.round((breakdown.tool_tokens / totalTokens) * 100) : 0}%</span>
            </div>
            {Object.keys(toolResultByName).length > 0 && (
              <div className="ml-4 mt-1 space-y-0.5">
                {Object.entries(toolResultByName)
                  .sort(([, a], [, b]) => b - a)
                  .map(([toolName, tokens]) => (
                    <BreakdownItem 
                      key={toolName}
                      label={toolName} 
                      tokens={tokens}
                      totalTokens={totalTokens} 
                      color="#fbbf24" 
                      indent={1} 
                    />
                  ))}
              </div>
            )}

            {/* Layer 3: Messages */}
            <div className="flex items-center gap-2 py-0.5 font-medium mt-2">
              <div className="w-3 h-3 rounded-sm bg-indigo-500" />
              <span className="text-xs text-gray-700 flex-1">Messages</span>
              <span className="text-xs font-mono text-gray-900">{formatTokens(breakdown.messages_tokens)}</span>
              <span className="text-[10px] text-gray-400 w-8 text-right">{totalTokens > 0 ? Math.round((breakdown.messages_tokens / totalTokens) * 100) : 0}%</span>
            </div>
            <BreakdownItem label="User" tokens={breakdown.user_tokens} totalTokens={totalTokens} color="#3b82f6" indent={1} detail={`${breakdown.message_count} 条`} />
            <BreakdownItem label="Assistant" tokens={breakdown.assistant_tokens} totalTokens={totalTokens} color="#60a5fa" indent={1} />
          </div>
        </div>
      )}

      {/* Prompts */}
      <div>
        <div className="px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-50 border-b border-gray-200">
          System Prompts
        </div>
        <PromptsPanel onFileSelect={onFileSelect} />
      </div>

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

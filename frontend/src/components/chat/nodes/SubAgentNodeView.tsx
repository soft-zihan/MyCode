import { memo, useEffect, useState } from 'react';
import { Bot, CheckCircle2, ChevronDown, ChevronRight, Brain, Minimize2, XCircle } from 'lucide-react';
import { sessionStore } from '../../../store';
import { backgroundSubagent } from '../../../api/client';
import { ToolRow } from '../ToolRow';
import { Markdown } from '../markdown';
import type { SubAgentNode, ToolCallNode, SubAgentEventItem } from './types';
import type { ToolCallEvent } from './types';

interface SubAgentNodeViewProps {
  node: SubAgentNode;
  sessionId?: string;
}

const formatDuration = (ms?: number): string => {
  if (ms === undefined) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
};

const formatTokens = (tokens?: number): string => {
  if (tokens === undefined) return '';
  if (tokens < 1000) return String(tokens);
  return `${(tokens / 1000).toFixed(1)}K`;
};

/** 去掉 <subagent session_id=... state=...> 包装，只展示内容 */
const stripSubagentTag = (text: string): string =>
  text.replace(/^<subagent [^>]*>/, '').replace(/<\/subagent>$/, '');

/** U3a：阻塞超 3s 才显示"转后台"按钮（方案验收标准） */
const BACKGROUND_BUTTON_DELAY_MS = 3000;

export const SubAgentNodeView = memo(function SubAgentNodeView({ node, sessionId }: SubAgentNodeViewProps) {
  const [expanded, setExpanded] = useState(node.status === 'running');
  const [showThinking, setShowThinking] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [bgBusy, setBgBusy] = useState(false);

  const isRunning = node.status === 'running';
  const isError = node.status === 'error';

  useEffect(() => {
    if (!isRunning) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [isRunning]);

  const canBackground =
    isRunning && !node.backgrounded && !bgBusy &&
    !!sessionId && !!node.startedAt && now - node.startedAt > BACKGROUND_BUTTON_DELAY_MS;

  const handleBackground = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!sessionId) return;
    setBgBusy(true);
    try {
      const res = await backgroundSubagent(sessionId, node.agentId);
      if (res.success) {
        sessionStore.updateSnapshot(sessionId, prev => {
          const existing = prev.nodes.get(node.key);
          if (!existing || existing.kind !== 'sub-agent') return prev;
          const newNodes = new Map(prev.nodes);
          newNodes.set(node.key, { ...existing, backgrounded: true });
          return { order: prev.order, nodes: newNodes };
        });
      } else {
        console.warn('[SUBAGENT] background rejected:', res.message);
      }
    } catch (err) {
      console.error('[SUBAGENT] background failed:', err);
    } finally {
      setBgBusy(false);
    }
  };

  const renderEventItem = (item: SubAgentEventItem, idx: number) => {
    switch (item.type) {
      case 'thinking':
        return (
          <div key={`thinking-${idx}`}>
            <button
              onClick={(e) => { e.stopPropagation(); setShowThinking(!showThinking); }}
              className="flex items-center gap-1.5 text-xs font-medium text-purple-600 hover:text-purple-700"
            >
              <Brain className="w-3 h-3" />
              <span>Thinking</span>
              {showThinking ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
            {showThinking && (
              <pre className="mt-1 text-xs text-gray-600 bg-purple-50/50 rounded p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap font-mono">
                {item.content}
              </pre>
            )}
          </div>
        );
      case 'text':
        return (
          <div key={`text-${idx}`}>
            <Markdown content={item.content} linkifyFiles={false} compact />
          </div>
        );
      case 'tool_call':
        return <SubAgentToolRow key={`tool-${idx}`} node={item.toolCall} />;
    }
  };

  return (
    <div className="flex justify-start">
      <div className="max-w-full w-full">
        <div className={`rounded-lg border overflow-hidden transition-colors ${
          isRunning ? (node.backgrounded ? 'border-blue-300 bg-blue-50/30' : 'border-purple-300 bg-purple-50/30') :
          isError ? 'border-red-300 bg-red-50/30' :
          'border-gray-200 bg-gray-50/30'
        }`}>
          {/* Header */}
          <div
            className={`flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors ${
              isRunning ? 'hover:bg-purple-100/50' : 'hover:bg-gray-100/50'
            }`}
            onClick={() => setExpanded(!expanded)}
          >
            <div className="flex items-center justify-center w-5 h-5">
              {isRunning ? (
                <div className={`w-2.5 h-2.5 rounded-full animate-pulse ${node.backgrounded ? 'bg-blue-500' : 'bg-purple-500'}`} />
              ) : isError ? (
                <div className="w-2.5 h-2.5 bg-red-500 rounded-full" />
              ) : (
                <Bot className="w-4 h-4 text-gray-500" />
              )}
            </div>
            <span className="text-sm font-medium text-gray-900">
              Sub-Agent: {node.agentType}
            </span>
            <span className="text-sm text-gray-500 truncate">
              {node.description}
            </span>
            <div className="flex items-center gap-2 ml-auto">
              {node.backgrounded && isRunning && (
                <span className="text-xs px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded whitespace-nowrap">
                  后台运行中
                </span>
              )}
              {canBackground && (
                <button
                  onClick={handleBackground}
                  className="flex items-center gap-1 text-xs px-1.5 py-0.5 bg-purple-100 text-purple-700 rounded hover:bg-purple-200 transition-colors whitespace-nowrap"
                  title="转为后台运行：主会话立即可继续对话，完成后自动通知"
                >
                  <Minimize2 className="w-3 h-3" />
                  <span>转后台</span>
                </button>
              )}
              {node.tokens !== undefined && (
                <span className="text-xs text-gray-400">{formatTokens(node.tokens)} tok</span>
              )}
              {node.durationMs !== undefined && (
                <span className="text-xs text-gray-400">{formatDuration(node.durationMs)}</span>
              )}
              {expanded ? (
                <ChevronDown className="w-4 h-4 text-gray-400" />
              ) : (
                <ChevronRight className="w-4 h-4 text-gray-400" />
              )}
            </div>
          </div>

          {/* U3a：后台完成通知卡片（subagent/completed synthetic 事件） */}
          {node.completionText && (
            <div className={`border-t px-3 py-2 ${isError ? 'border-red-200 bg-red-50/50' : 'border-green-200 bg-green-50/50'}`}>
              <div className={`flex items-center gap-1.5 text-xs font-medium mb-1 ${isError ? 'text-red-700' : 'text-green-700'}`}>
                {isError ? <XCircle className="w-3 h-3" /> : <CheckCircle2 className="w-3 h-3" />}
                <span>{isError ? '后台任务结束（异常）' : '后台任务完成'} — 结果已注入会话，模型下一轮可见</span>
              </div>
              <Markdown content={stripSubagentTag(node.completionText)} linkifyFiles={false} compact />
            </div>
          )}

          {/* Expanded content - render in order */}
          {expanded && (
            <div className="border-t border-gray-200 px-3 py-2 space-y-2">
              {node.internalOrder.length > 0 ? (
                node.internalOrder.map((item, idx) => renderEventItem(item, idx))
              ) : (
                <>
                  {/* Fallback for old data without internalOrder */}
                  {node.thinking && (
                    <div>
                      <button
                        onClick={(e) => { e.stopPropagation(); setShowThinking(!showThinking); }}
                        className="flex items-center gap-1.5 text-xs font-medium text-purple-600 hover:text-purple-700"
                      >
                        <Brain className="w-3 h-3" />
                        <span>Thinking</span>
                        {showThinking ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                      </button>
                      {showThinking && (
                        <pre className="mt-1 text-xs text-gray-600 bg-purple-50/50 rounded p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap font-mono">
                          {node.thinking}
                        </pre>
                      )}
                    </div>
                  )}
                  {node.text && (
                    <Markdown content={node.text} linkifyFiles={false} compact />
                  )}
                  {node.toolCalls.length > 0 && (
                    <div className="space-y-1">
                      {node.toolCalls.map(tc => (
                        <SubAgentToolRow key={tc.callId} node={tc} />
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
});

const SubAgentToolRow = memo(function SubAgentToolRow({ node }: { node: ToolCallNode }) {
  const toolCall: ToolCallEvent = {
    call_id: node.callId,
    name: node.name,
    input: node.input,
    status: node.status === 'success' ? 'success' : node.status === 'error' ? 'error' : node.status === 'denied' ? 'denied' : 'pending',
    result: node.result,
    duration_ms: node.durationMs,
  };

  return <ToolRow call={toolCall} />;
});

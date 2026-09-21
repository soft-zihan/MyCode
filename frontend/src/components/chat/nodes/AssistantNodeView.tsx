import { memo, useState, useEffect, useRef } from 'react';
import { Brain, ChevronDown, ChevronRight, Copy, Check } from 'lucide-react';
import { Markdown } from '../markdown';
import type { AssistantNode } from './types';
import { ThumbsDownButton, type ThumbsDownFeedback } from '../ThumbsDownDialog';

interface AssistantNodeViewProps {
  node: AssistantNode;
  sessionId?: string;
  onFileClick?: (path: string) => void;
}

export const AssistantNodeView = memo(function AssistantNodeView({ node, sessionId, onFileClick }: AssistantNodeViewProps) {
  const [showThinking, setShowThinking] = useState(node.streaming && !!node.thinking && !node.content);
  const [copied, setCopied] = useState(false);
  const prevStreamingRef = useRef(node.streaming);
  const prevHasContentRef = useRef(!!node.content);
  const thinkingRef = useRef<HTMLPreElement>(null);
  
  // 当 streaming 开始时，如果有 thinking 且没有 content，自动展开
  // 当开始输出 content 时，自动折叠 thinking
  useEffect(() => {
    const hadContent = prevHasContentRef.current;
    
    // streaming 开始且有 thinking，展开
    if (node.streaming && !!node.thinking && !node.content) {
      setShowThinking(true);
    }
    // 开始输出 content，折叠 thinking
    else if (!hadContent && !!node.content) {
      setShowThinking(false);
    }
    
    prevStreamingRef.current = node.streaming;
    prevHasContentRef.current = !!node.content;
  }, [node.streaming, node.thinking, node.content]);
  
  // thinking 内容更新时自动滚动到底部
  useEffect(() => {
    if (showThinking && thinkingRef.current && node.streaming && !node.content) {
      thinkingRef.current.scrollTop = thinkingRef.current.scrollHeight;
    }
  }, [node.thinking, showThinking, node.streaming, node.content]);
  
  if (!node.content && !node.thinking && !node.streaming) return null;

  const handleCopy = async () => {
    if (node.content) {
      await navigator.clipboard.writeText(node.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const handleThumbsDown = async (feedback: ThumbsDownFeedback) => {
    if (!sessionId || !node.turn) return;
    
    try {
      await fetch('/api/bad-cases/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          turn_number: node.turn,
          step_number: node.step,
          reason: feedback.reason,
          expected_tool: feedback.expectedTool,
          comment: feedback.comment,
        }),
      });
    } catch (error) {
      console.error('Failed to submit feedback:', error);
    }
  };

  return (
    <div className="flex justify-start group">
      <div className="max-w-full w-full">
        <div className="px-1 py-1 text-gray-900">
          {/* Thinking section */}
          {node.thinking && (
            <div className="mb-2">
              <button
                onClick={() => setShowThinking(!showThinking)}
                className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-600 transition-colors"
              >
                <Brain className="w-3 h-3" />
                <span>Thought{node.streaming && !node.content ? 'ing...' : ''}</span>
                {showThinking ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
              </button>
              {showThinking && (
                <pre ref={thinkingRef} className="mt-1 text-xs text-gray-400 bg-gray-50 border-l-2 border-gray-200 rounded-r p-2 overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap font-mono leading-relaxed">
                  {node.thinking}
                </pre>
              )}
            </div>
          )}
          
          {/* Main content */}
          {node.content && (
            <Markdown
              content={node.content}
              onFileClick={onFileClick}
              isStreaming={node.streaming}
            />
          )}
          
          {/* Streaming indicator when only thinking */}
          {node.streaming && !node.content && !node.thinking && (
            <div className="flex items-center gap-2 text-gray-400 text-sm">
              <div className="flex gap-1">
                <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" />
                <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" style={{ animationDelay: '0.2s' }} />
                <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" style={{ animationDelay: '0.4s' }} />
              </div>
              <span>Thinking...</span>
            </div>
          )}
          
          <div className="hidden group-hover:flex text-[11px] mt-1 items-center justify-between text-gray-400">
            <span>{new Date(node.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span>
            {node.content && !node.streaming && (
              <div className="flex items-center gap-1">
                <button
                  onClick={handleCopy}
                  className="flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-gray-200 hover:text-gray-600 transition-colors"
                  title="Copy message"
                >
                  {copied ? (
                    <>
                      <Check className="w-3 h-3" />
                      <span>Copied</span>
                    </>
                  ) : (
                    <>
                      <Copy className="w-3 h-3" />
                      <span>Copy</span>
                    </>
                  )}
                </button>
                {sessionId && node.turn && (
                  <ThumbsDownButton
                    hasToolCalls={node.hasToolCalls || false}
                    onSubmit={handleThumbsDown}
                  />
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
});

import { memo, useState, useEffect, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Brain, ChevronDown, ChevronRight, Copy, Check } from 'lucide-react';
import type { AssistantNode } from './types';

interface AssistantNodeViewProps {
  node: AssistantNode;
  onFileClick?: (path: string) => void;
}

const fileLinkRenderer = (onFileClick?: (path: string) => void) => (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
  const { href, children } = props;
  if (href && !href.startsWith('http') && !href.startsWith('#')) {
    return (
      <span
        className="text-blue-600 hover:text-blue-800 underline cursor-pointer font-mono text-xs"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onFileClick?.(href);
        }}
      >
        {children}
      </span>
    );
  }
  return <a {...props} />;
};

export const AssistantNodeView = memo(function AssistantNodeView({ node, onFileClick }: AssistantNodeViewProps) {
  const [showThinking, setShowThinking] = useState(node.streaming && !!node.thinking && !node.content);
  const [copied, setCopied] = useState(false);
  const prevStreamingRef = useRef(node.streaming);
  const prevHasContentRef = useRef(!!node.content);
  
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
  
  if (!node.content && !node.thinking && !node.streaming) return null;

  const handleCopy = async () => {
    if (node.content) {
      await navigator.clipboard.writeText(node.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="flex justify-start">
      <div className="max-w-2xl w-full">
        <div className="rounded-lg px-4 py-3 bg-gray-100 text-gray-900">
          {/* Thinking section */}
          {node.thinking && (
            <div className="mb-2">
              <button
                onClick={() => setShowThinking(!showThinking)}
                className="flex items-center gap-1.5 text-xs font-medium text-purple-600 hover:text-purple-700"
              >
                <Brain className="w-3 h-3" />
                <span>Thought{node.streaming && !node.content ? 'ing...' : ''}</span>
                {showThinking ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
              </button>
              {showThinking && (
                <pre className="mt-1 text-xs text-gray-600 bg-purple-50/50 rounded p-2 overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap font-mono">
                  {node.thinking}
                </pre>
              )}
            </div>
          )}
          
          {/* Main content */}
          {node.content && (
            <div className="prose prose-sm max-w-none">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{ a: fileLinkRenderer(onFileClick) }}
              >
                {node.content}
              </ReactMarkdown>
            </div>
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
          
          <div className="text-xs mt-1 flex items-center justify-between text-gray-400">
            <span>{new Date(node.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span>
            {node.content && !node.streaming && (
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
            )}
          </div>
        </div>
      </div>
    </div>
  );
});

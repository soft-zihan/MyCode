import { memo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Brain, ChevronDown, ChevronRight } from 'lucide-react';
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
  const [showThinking, setShowThinking] = useState(false);
  
  if (!node.content && !node.thinking && !node.streaming) return null;

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
          
          <div className="text-xs mt-1 flex items-center gap-2 text-gray-400">
            <span>{new Date(node.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span>
          </div>
        </div>
      </div>
    </div>
  );
});

import { memo, useState, useEffect, useRef } from 'react';
import { Brain, ChevronDown, ChevronRight } from 'lucide-react';
import type { ThinkingNode } from './types';

interface ThinkingNodeViewProps {
  node: ThinkingNode;
}

export const ThinkingNodeView = memo(function ThinkingNodeView({ node }: ThinkingNodeViewProps) {
  const [isExpanded, setIsExpanded] = useState(node.streaming && !node.complete);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevStreamingRef = useRef(node.streaming);

  useEffect(() => {
    const wasStreaming = prevStreamingRef.current;
    if (node.streaming && !node.complete) {
      setIsExpanded(true);
    } else if (wasStreaming && !node.streaming) {
      setIsExpanded(false);
    }
    prevStreamingRef.current = node.streaming;
  }, [node.streaming, node.complete]);

  useEffect(() => {
    if (node.streaming && !node.complete && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [node.content, node.streaming, node.complete]);

  if (node.streaming && !node.complete) {
    return (
      <div className="mb-3 border border-purple-200 rounded-lg bg-purple-50/50">
        <div className="flex items-center gap-2 px-3 py-1.5 border-b border-purple-200 bg-purple-100/50 rounded-t-lg">
          <Brain className="w-3.5 h-3.5 text-purple-600" />
          <span className="text-xs font-medium text-purple-700">Thinking...</span>
          <div className="flex-1" />
          <div className="flex gap-1">
            <div className="w-1.5 h-1.5 bg-purple-500 rounded-full animate-pulse" />
            <div className="w-1.5 h-1.5 bg-purple-500 rounded-full animate-pulse" style={{ animationDelay: '0.2s' }} />
            <div className="w-1.5 h-1.5 bg-purple-500 rounded-full animate-pulse" style={{ animationDelay: '0.4s' }} />
          </div>
        </div>
        <div
          ref={scrollRef}
          className="px-3 py-2 text-xs text-gray-600 overflow-y-auto font-mono whitespace-pre-wrap"
          style={{ maxHeight: '150px' }}
        >
          {node.content}
        </div>
      </div>
    );
  }

  return (
    <div className="mb-3">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-purple-600 bg-purple-50 hover:bg-purple-100 border border-purple-200 rounded-lg transition-colors"
      >
        <Brain className="w-3.5 h-3.5" />
        <span>Thinking</span>
        {isExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
      </button>
      {isExpanded && (
        <div className="mt-2 border border-purple-200 rounded-lg bg-purple-50/50">
          <div className="px-3 py-2 text-xs text-gray-600 overflow-y-auto font-mono whitespace-pre-wrap max-h-60">
            {node.content}
          </div>
        </div>
      )}
    </div>
  );
});

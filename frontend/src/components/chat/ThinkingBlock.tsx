import { useState, useEffect, useRef } from 'react';
import { Brain, ChevronDown, ChevronRight } from 'lucide-react';

interface ThinkingBlockProps {
  thinking: string;
  isStreaming: boolean;
  isComplete: boolean;
}

export function ThinkingBlock({ thinking, isStreaming, isComplete }: ThinkingBlockProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevIsStreamingRef = useRef(isStreaming);
  
  useEffect(() => {
    const wasStreaming = prevIsStreamingRef.current;
    
    if (isStreaming && !isComplete) {
      setIsExpanded(true);
    } else if (wasStreaming && !isStreaming) {
      setIsExpanded(false);
    }
    
    prevIsStreamingRef.current = isStreaming;
  }, [isStreaming, isComplete]);
  
  useEffect(() => {
    if (isStreaming && !isComplete && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [thinking, isStreaming, isComplete]);
  
  if (isStreaming && !isComplete) {
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
          {thinking}
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
            {thinking}
          </div>
        </div>
      )}
    </div>
  );
}

export default ThinkingBlock;

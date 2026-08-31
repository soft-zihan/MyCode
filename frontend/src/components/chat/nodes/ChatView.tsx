import { memo, useRef, useEffect, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import type { ChatSnapshot } from './types';
import { ChatNodeSeat } from './ChatNodeSeat';

interface ChatViewProps {
  snapshot: ChatSnapshot;
  isStreaming: boolean;
  onEditMessage?: (index: number) => void;
  onFileClick?: (path: string) => void;
}

export const ChatView = memo(function ChatView({ snapshot, isStreaming, onEditMessage, onFileClick }: ChatViewProps) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef(true);
  const [showScrollBtn, setShowScrollBtn] = useState(false);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    isNearBottomRef.current = true;
    setShowScrollBtn(false);
  };

  const handleScroll = () => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
    isNearBottomRef.current = atBottom;
    setShowScrollBtn(!atBottom);
  };

  useEffect(() => {
    if (isNearBottomRef.current) {
      scrollToBottom();
    }
  }, [snapshot.order.length]);

  const { order, nodes } = snapshot;

  if (order.length === 0 && !isStreaming) {
    return (
      <div className="flex-1 overflow-y-auto px-4 py-4 flex items-center justify-center text-gray-400">
        <div className="text-center">
          <svg className="w-12 h-12 mx-auto mb-4 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
          </svg>
          <p className="text-sm">Start a conversation</p>
          <p className="text-xs mt-1">Type a message below or use @ to reference files</p>
        </div>
      </div>
    );
  }

  return (
    <div ref={scrollContainerRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-4 py-4">
      <div className="space-y-4">
        {order.map((key, idx) => {
          const node = nodes.get(key);
          if (!node) return null;
          return (
            <ChatNodeSeat
              key={key}
              node={node}
              index={idx}
              onEditMessage={onEditMessage}
              onFileClick={onFileClick}
            />
          );
        })}
        <div ref={messagesEndRef} />
      </div>

      {showScrollBtn && (
        <button
          onClick={scrollToBottom}
          className="sticky bottom-2 float-right mr-2 px-3 py-1.5 bg-gray-800 text-white text-xs rounded-full shadow-lg hover:bg-gray-700 transition-colors z-10 flex items-center gap-1"
        >
          <ChevronDown className="w-3 h-3" />
          Bottom
        </button>
      )}
    </div>
  );
});

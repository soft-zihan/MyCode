import { memo, useRef, useEffect, useState } from 'react';
import { ChevronDown, Loader2, GitBranch } from 'lucide-react';
import type { ChatSnapshot, UserNode } from './types';
import { ChatNodeSeat } from './ChatNodeSeat';

interface ChatViewProps {
  snapshot: ChatSnapshot;
  isStreaming: boolean;
  isWaitingResponse?: boolean;
  onEditMessage?: (node: UserNode, restoreFiles: boolean) => void;
  onFileClick?: (path: string) => void;
  onFork?: (beforeIndex: number) => void;
}

export const ChatView = memo(function ChatView({ snapshot, isStreaming, isWaitingResponse, onEditMessage, onFileClick, onFork }: ChatViewProps) {
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
  }, [snapshot.order.length, isWaitingResponse]);

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

  let userMsgIdx = 0;

  return (
    <div ref={scrollContainerRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-4 py-4">
      <div className="space-y-4">
        {order.map((key, idx) => {
          const node = nodes.get(key);
          if (!node) return null;
          
          const isUserNode = node.kind === 'user';
          const thisUserMsgIdx = isUserNode ? userMsgIdx++ : -1;
          
          const showForkButton = isUserNode && idx > 0 && !isStreaming;
          
          return (
            <div key={key}>
              {showForkButton && (
                <div className="flex items-center gap-2 py-2 my-2 group/fork">
                  <div className="flex-1 h-px bg-gray-200"></div>
                  <button
                    onClick={() => {
                      console.log('[FORK] button clicked, thisUserMsgIdx:', thisUserMsgIdx, 'nodeKey:', key);
                      onFork?.(thisUserMsgIdx);
                    }}
                    className="flex items-center gap-1 px-2 py-1 text-xs text-gray-400 hover:text-green-600 hover:bg-green-50 rounded transition-colors opacity-0 group-hover/fork:opacity-100"
                    title="Fork from this point"
                  >
                    <GitBranch className="w-3 h-3" />
                    Fork here
                  </button>
                  <div className="flex-1 h-px bg-gray-200"></div>
                </div>
              )}
              <ChatNodeSeat
                node={node}
                index={idx}
                onEditMessage={onEditMessage}
                onFileClick={onFileClick}
              />
            </div>
          );
        })}
        {isWaitingResponse && (
          <div className="flex items-center gap-2 px-3 py-2 text-gray-400">
            <Loader2 className="w-4 h-4 animate-spin" />
            <span className="text-sm">Thinking...</span>
          </div>
        )}
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

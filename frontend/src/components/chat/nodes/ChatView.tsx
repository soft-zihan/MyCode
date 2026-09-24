import { memo, useRef, useEffect, useState } from 'react';
import { ChevronDown, Loader2, GitBranch } from 'lucide-react';
import type { ChatSnapshot, UserNode } from './types';
import { ChatNodeSeat } from './ChatNodeSeat';

interface ChatViewProps {
  snapshot: ChatSnapshot;
  sessionId?: string;
  isStreaming: boolean;
  isLoadingSession?: boolean;
  isWaitingResponse?: boolean;
  hasMoreHistory?: boolean;
  onLoadMoreHistory?: () => Promise<boolean>;
  onEditMessage?: (node: UserNode) => void;
  onRewind?: (userMessageIndex: number) => void;
  onFileClick?: (path: string) => void;
  onFork?: (beforeIndex: number) => void;
}

export const ChatView = memo(function ChatView({ snapshot, sessionId, isStreaming, isLoadingSession, isWaitingResponse, hasMoreHistory, onLoadMoreHistory, onEditMessage, onRewind, onFileClick, onFork }: ChatViewProps) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef(true);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  // BC-19：滚动顶懒加载更早历史（prepend 后恢复阅读位置）
  const [loadingMore, setLoadingMore] = useState(false);
  const loadingMoreRef = useRef(false);
  const anchorRef = useRef<{ scrollHeight: number; scrollTop: number } | null>(null);

  const scrollToBottom = (behavior: ScrollBehavior = 'smooth') => {
    messagesEndRef.current?.scrollIntoView({ behavior });
    isNearBottomRef.current = true;
    setShowScrollBtn(false);
  };

  // 流式输出时内容高度持续增长但 order.length 不变：观察内容尺寸，贴底则跟随滚动
  useEffect(() => {
    const el = contentRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      if (isNearBottomRef.current) {
        scrollToBottom('auto');
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const handleScroll = () => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
    isNearBottomRef.current = atBottom;
    setShowScrollBtn(!atBottom);

    // BC-19：滚到顶部触发懒加载更早事件
    if (el.scrollTop < 80 && hasMoreHistory && onLoadMoreHistory && !loadingMoreRef.current) {
      loadingMoreRef.current = true;
      setLoadingMore(true);
      anchorRef.current = { scrollHeight: el.scrollHeight, scrollTop: el.scrollTop };
      onLoadMoreHistory().finally(() => {
        loadingMoreRef.current = false;
        setLoadingMore(false);
      });
    }
  };

  // prepend 落地后恢复阅读位置（新增高度补偿到 scrollTop）
  useEffect(() => {
    const el = scrollContainerRef.current;
    const anchor = anchorRef.current;
    if (!el || !anchor) return;
    el.scrollTop = anchor.scrollTop + (el.scrollHeight - anchor.scrollHeight);
    anchorRef.current = null;
  }, [snapshot]);

  useEffect(() => {
    if (isNearBottomRef.current) {
      scrollToBottom();
    }
  }, [snapshot.order.length, isWaitingResponse]);

  const { order, nodes } = snapshot;

  // 右侧 TOC 轨：每个用户消息 = 一个 turn 起点
  const userTurns: { key: string; preview: string }[] = [];
  for (const key of order) {
    const n = nodes.get(key);
    if (n && n.kind === 'user') {
      userTurns.push({ key, preview: (n.content || '').replace(/\s+/g, ' ').slice(0, 50) });
    }
  }

  const scrollToNode = (key: string) => {
    document.getElementById(`chat-node-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  if (order.length === 0 && !isStreaming) {
    if (isLoadingSession) {
      return (
        <div className="flex-1 overflow-y-auto px-4 py-4 flex items-center justify-center text-gray-400">
          <div className="flex items-center gap-2">
            <Loader2 className="w-5 h-5 animate-spin" />
            <span className="text-sm">Loading session...</span>
          </div>
        </div>
      );
    }
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
    <div className="flex-1 flex min-h-0 relative">
    <div ref={scrollContainerRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-4 py-4 min-w-0">
      <div ref={contentRef} className="space-y-4">
        {loadingMore && (
          <div className="flex items-center justify-center gap-2 py-2 text-gray-400">
            <Loader2 className="w-4 h-4 animate-spin" />
            <span className="text-xs">加载更早历史…</span>
          </div>
        )}
        {order.map((key, idx) => {
          const node = nodes.get(key);
          if (!node) return null;
          
          const isUserNode = node.kind === 'user';
          const thisUserMsgIdx = isUserNode ? userMsgIdx++ : -1;
          
          const showForkButton = isUserNode && idx > 0 && !isStreaming;
          
          return (
            <div key={key} id={`chat-node-${key}`}>
              {showForkButton && (
                <div className="flex items-center gap-2 h-3 -my-1 group/fork opacity-0 hover:opacity-100 transition-opacity">
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
                sessionId={sessionId}
                userMessageIndex={isUserNode ? thisUserMsgIdx : undefined}
                onEditMessage={onEditMessage}
                onRewind={onRewind}
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
          onClick={() => scrollToBottom()}
          className="sticky bottom-2 float-right mr-2 px-3 py-1.5 bg-gray-800 text-white text-xs rounded-full shadow-lg hover:bg-gray-700 transition-colors z-10 flex items-center gap-1"
        >
          <ChevronDown className="w-3 h-3" />
          Bottom
        </button>
      )}
    </div>

    {userTurns.length > 1 && (
      <div
        className="w-5 flex-shrink-0 flex flex-col items-center justify-center gap-1.5 py-6 overflow-y-auto border-l border-gray-100"
        title="Turn 定位（点击跳转）"
      >
        {userTurns.map(t => (
          <button
            key={t.key}
            onClick={() => scrollToNode(t.key)}
            title={t.preview || 'turn'}
            className="w-3 h-0.5 rounded bg-gray-300 hover:bg-indigo-500 hover:w-4 transition-all flex-shrink-0"
          />
        ))}
      </div>
    )}
    </div>
  );
});

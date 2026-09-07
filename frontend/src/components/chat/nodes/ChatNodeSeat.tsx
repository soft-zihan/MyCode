import { memo } from 'react';
import type { ChatNode, UserNode } from './types';
import { UserNodeView } from './UserNodeView';
import { AssistantNodeView } from './AssistantNodeView';
import { ThinkingNodeView } from './ThinkingNodeView';
import { ToolCallNodeView } from './ToolCallNodeView';
import { SubAgentNodeView } from './SubAgentNodeView';
import { ErrorNodeView } from './ErrorNodeView';
import { SystemNodeView } from './SystemNodeView';

interface ChatNodeSeatProps {
  node: ChatNode;
  index: number;
  sessionId?: string;
  onEditMessage?: (node: UserNode, restoreFiles: boolean) => void;
  onFileClick?: (path: string) => void;
}

export const ChatNodeSeat = memo(function ChatNodeSeat({ node, sessionId, onEditMessage, onFileClick }: ChatNodeSeatProps) {
  switch (node.kind) {
    case 'user':
      return <UserNodeView node={node} sessionId={sessionId} onEdit={onEditMessage} />;
    case 'assistant':
      return <AssistantNodeView node={node} onFileClick={onFileClick} />;
    case 'thinking':
      return <ThinkingNodeView node={node} />;
    case 'tool-call':
      return <ToolCallNodeView node={node} />;
    case 'sub-agent':
      return <SubAgentNodeView node={node} />;
    case 'error':
      return <ErrorNodeView node={node} />;
    case 'system':
      return <SystemNodeView node={node} />;
    case 'turn-status':
      return null;
    default:
      return null;
  }
});

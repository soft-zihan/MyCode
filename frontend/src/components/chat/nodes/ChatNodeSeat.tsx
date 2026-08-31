import { memo } from 'react';
import type { ChatNode } from './types';
import { UserNodeView } from './UserNodeView';
import { AssistantNodeView } from './AssistantNodeView';
import { ThinkingNodeView } from './ThinkingNodeView';
import { ToolCallNodeView } from './ToolCallNodeView';
import { SubAgentNodeView } from './SubAgentNodeView';
import { ErrorNodeView } from './ErrorNodeView';

interface ChatNodeSeatProps {
  node: ChatNode;
  index: number;
  onEditMessage?: (index: number) => void;
  onFileClick?: (path: string) => void;
}

export const ChatNodeSeat = memo(function ChatNodeSeat({ node, index, onEditMessage, onFileClick }: ChatNodeSeatProps) {
  switch (node.kind) {
    case 'user':
      return <UserNodeView node={node} onEdit={onEditMessage} index={index} />;
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
    case 'turn-status':
      return null;
    default:
      return null;
  }
});

import { memo } from 'react';
import { ToolRow } from '../ToolRow';
import type { ToolCallNode } from './types';
import type { ToolCallEvent } from './types';

interface ToolCallNodeViewProps {
  node: ToolCallNode;
}

export const ToolCallNodeView = memo(function ToolCallNodeView({ node }: ToolCallNodeViewProps) {
  const toolCall: ToolCallEvent = {
    call_id: node.callId,
    name: node.name,
    input: node.input,
    status: node.status === 'success' ? 'success' : node.status === 'error' ? 'error' : node.status === 'denied' ? 'denied' : 'pending',
    result: node.result,
    duration_ms: node.durationMs,
    snapshot: node.snapshot ? {
      file_path: node.snapshot.file_path,
      old_content: node.snapshot.old_content,
      new_content: node.snapshot.new_content,
    } : undefined,
  };

  return (
    <div className="flex justify-start">
      <div className="w-full">
        <ToolRow call={toolCall} />
      </div>
    </div>
  );
});

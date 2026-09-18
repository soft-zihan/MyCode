import type { ToolCallEvent } from '../../hooks';
import { ToolCardRouter } from './ToolCards';
import { ContextToolGroup, isContextTool } from './ContextToolGroup';

interface ToolCallTreeProps {
  calls: Map<string, ToolCallEvent> | ToolCallEvent[];
  maxDisplay?: number;
  isStreaming?: boolean;
}

export const ToolCallTree: React.FC<ToolCallTreeProps> = ({ 
  calls, 
  maxDisplay = 10,
  isStreaming = false 
}) => {
  const callsArray = Array.isArray(calls) 
    ? calls
    : Array.from(calls.values());

  if (callsArray.length === 0) return null;

  const groups: Array<{ type: 'context' | 'tool'; items: ToolCallEvent[] }> = [];
  let currentContextGroup: ToolCallEvent[] = [];

  for (const call of callsArray) {
    if (isContextTool(call.name)) {
      currentContextGroup.push(call);
    } else {
      if (currentContextGroup.length > 0) {
        groups.push({ type: 'context', items: currentContextGroup });
        currentContextGroup = [];
      }
      groups.push({ type: 'tool', items: [call] });
    }
  }

  if (currentContextGroup.length > 0) {
    groups.push({ type: 'context', items: currentContextGroup });
  }

  const totalItems = groups.length;
  const displayGroups = maxDisplay && totalItems > maxDisplay 
    ? groups.slice(-maxDisplay) 
    : groups;
  const hiddenCount = totalItems - displayGroups.length;

  return (
    <div className="space-y-1">
      {hiddenCount > 0 && (
        <div className="text-xs text-gray-400 dark:text-gray-500 px-1 py-0.5">
          +{hiddenCount} more tool call{hiddenCount > 1 ? 's' : ''}
        </div>
      )}
      {displayGroups.map((group, index) => {
        if (group.type === 'context') {
          return (
            <ContextToolGroup 
              key={`ctx-${index}`} 
              tools={group.items} 
              isStreaming={isStreaming} 
            />
          );
        } else {
          return group.items.map(call => (
            <ToolCardRouter key={call.call_id} call={call} />
          ));
        }
      })}
    </div>
  );
};

export default ToolCallTree;

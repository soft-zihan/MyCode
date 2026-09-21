import { memo } from 'react';
import { Info } from 'lucide-react';
import type { SystemNode } from './types';

interface SystemNodeViewProps {
  node: SystemNode;
}

export const SystemNodeView = memo(function SystemNodeView({ node }: SystemNodeViewProps) {
  return (
    <div className="flex justify-center py-0.5">
      <div className="inline-flex items-center gap-1.5 text-xs text-gray-400 bg-gray-50 border border-gray-100 rounded-full px-3 py-1 max-w-[90%]">
        <Info className="w-3 h-3 flex-shrink-0" />
        <span className="truncate">{node.message}</span>
      </div>
    </div>
  );
});

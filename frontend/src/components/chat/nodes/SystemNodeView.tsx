import { memo } from 'react';
import { Info } from 'lucide-react';
import type { SystemNode } from './types';

interface SystemNodeViewProps {
  node: SystemNode;
}

export const SystemNodeView = memo(function SystemNodeView({ node }: SystemNodeViewProps) {
  return (
    <div className="flex justify-center">
      <div className="max-w-full">
        <div className="rounded-lg px-4 py-3 bg-blue-50 border border-blue-200">
          <div className="flex items-center gap-2 text-blue-700">
            <Info className="w-4 h-4" />
            <span className="text-sm font-medium">System</span>
          </div>
          <p className="text-sm text-blue-600 mt-1">{node.message}</p>
        </div>
      </div>
    </div>
  );
});

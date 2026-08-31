import { memo } from 'react';
import { AlertCircle } from 'lucide-react';
import type { ErrorNode } from './types';

interface ErrorNodeViewProps {
  node: ErrorNode;
}

export const ErrorNodeView = memo(function ErrorNodeView({ node }: ErrorNodeViewProps) {
  return (
    <div className="flex justify-start">
      <div className="max-w-2xl">
        <div className="rounded-lg px-4 py-3 bg-red-50 border border-red-200">
          <div className="flex items-center gap-2 text-red-700">
            <AlertCircle className="w-4 h-4" />
            <span className="text-sm font-medium">Error</span>
          </div>
          <p className="text-sm text-red-600 mt-1">{node.message}</p>
        </div>
      </div>
    </div>
  );
});

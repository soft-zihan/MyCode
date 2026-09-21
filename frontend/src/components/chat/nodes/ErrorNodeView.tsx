import { memo } from 'react';
import { AlertCircle } from 'lucide-react';
import type { ErrorNode } from './types';

interface ErrorNodeViewProps {
  node: ErrorNode;
}

export const ErrorNodeView = memo(function ErrorNodeView({ node }: ErrorNodeViewProps) {
  return (
    <div className="flex justify-start">
      <div className="max-w-full">
        <div className="rounded-lg px-4 py-2.5 bg-red-50/60 border border-red-100 border-l-4 border-l-red-400">
          <div className="flex items-start gap-2 text-red-700">
            <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <p className="text-sm leading-relaxed">{node.message}</p>
          </div>
        </div>
      </div>
    </div>
  );
});

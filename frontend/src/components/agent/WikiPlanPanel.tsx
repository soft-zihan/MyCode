import { useState } from 'react';
import { BookOpen, Target } from 'lucide-react';
import { WikiPanel } from './WikiPanel';
import { PlanControlPanel } from './PlanControlPanel';

interface WikiPlanPanelProps {
  cwd: string | null;
  sessionId: string;
  planSlug: string;
  permissionMode?: string;
  onFileSelect: (path: string) => void;
  selectedFile: string | null;
}

type SubTab = 'wiki' | 'plan';

function PlanCombinedPanel({ sessionId, planSlug, permissionMode }: {
  sessionId: string;
  planSlug: string;
  permissionMode?: string;
  cwd: string | null;
  onFileSelect: (path: string) => void;
}) {
  return (
    <div className="flex flex-col h-full">
      <PlanControlPanel sessionId={sessionId} planSlug={planSlug} permissionMode={permissionMode} />
    </div>
  );
}

export function WikiPlanPanel({ cwd, sessionId, planSlug, permissionMode, onFileSelect, selectedFile }: WikiPlanPanelProps) {
  const [subTab, setSubTab] = useState<SubTab>('wiki');

  return (
    <div className="flex flex-col h-full">
      <div className="flex border-b border-gray-200 bg-gray-50">
        <button
          onClick={() => setSubTab('wiki')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'wiki' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <BookOpen className="w-3 h-3 inline mr-1" />
          Wiki
        </button>
        <button
          onClick={() => setSubTab('plan')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'plan' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <Target className="w-3 h-3 inline mr-1" />
          Plan
        </button>
      </div>

      <div className="flex-1 overflow-auto">
        {subTab === 'wiki' && (
          <WikiPanel cwd={cwd} onFileSelect={onFileSelect} selectedFile={selectedFile} />
        )}
        {subTab === 'plan' && (
          <PlanCombinedPanel sessionId={sessionId} planSlug={planSlug || ''} permissionMode={permissionMode} cwd={cwd} onFileSelect={onFileSelect} />
        )}
      </div>
    </div>
  );
}

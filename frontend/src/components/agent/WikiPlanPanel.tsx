import { useState } from 'react';
import { BookOpen, Target, Settings, FileText } from 'lucide-react';
import { WikiPanel } from './WikiPanel';
import { PlanControlPanel } from './PlanControlPanel';

interface WikiPlanPanelProps {
  cwd: string | null;
  sessionId: string;
  planSlug: string;
  onFileSelect: (path: string) => void;
  selectedFile: string | null;
}

type SubTab = 'docs' | 'plan-config' | 'plan-artifacts';

export function WikiPlanPanel({ cwd, sessionId, planSlug, onFileSelect, selectedFile }: WikiPlanPanelProps) {
  const [subTab, setSubTab] = useState<SubTab>('docs');

  return (
    <div className="flex flex-col h-full">
      {/* Sub-tabs */}
      <div className="flex border-b border-gray-200 bg-gray-50">
        <button
          onClick={() => setSubTab('docs')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'docs' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <BookOpen className="w-3 h-3 inline mr-1" />
          文档
        </button>
        <button
          onClick={() => setSubTab('plan-config')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'plan-config' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <Settings className="w-3 h-3 inline mr-1" />
          Plan 配置
        </button>
        <button
          onClick={() => setSubTab('plan-artifacts')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'plan-artifacts' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <FileText className="w-3 h-3 inline mr-1" />
          Plan 产物
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto">
        {subTab === 'docs' && (
          <WikiPanel cwd={cwd} onFileSelect={onFileSelect} selectedFile={selectedFile} />
        )}
        {subTab === 'plan-config' && (
          <PlanControlPanel sessionId={sessionId} planSlug="" />
        )}
        {subTab === 'plan-artifacts' && (
          <PlanControlPanel sessionId={sessionId} planSlug={planSlug} />
        )}
      </div>
    </div>
  );
}

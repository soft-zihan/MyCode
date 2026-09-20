import { useState, useEffect, useMemo, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { MessageSquare, X, Edit3, Zap, StepForward, Eraser } from 'lucide-react';
import type { PermissionRequest } from '../../store/SessionStore';
import { getPlanDraftArtifacts, updatePlanDraftArtifact } from '../../api/client';

interface PlanApprovalDialogProps {
  request: PermissionRequest;
  sessionId: string;
  onApprove: (choice: string) => void;
  onDeny: (feedback?: string) => void;
}

type ViewMode = 'preview' | 'edit' | 'feedback';

const TAB_LABELS: Record<string, string> = {
  'spec.md': 'Spec',
  'design.md': 'Design',
  'tasks.md': 'Tasks',
  'plan.md': 'Plan',
  'proposal.md': 'Proposal',
};

export function PlanApprovalDialog({ request, sessionId, onApprove, onDeny }: PlanApprovalDialogProps) {
  const [viewMode, setViewMode] = useState<ViewMode>('preview');
  const [feedback, setFeedback] = useState('');
  const [artifacts, setArtifacts] = useState<Record<string, string> | null>(null);
  const [activeTab, setActiveTab] = useState<string>('');
  const [editedContent, setEditedContent] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getPlanDraftArtifacts(sessionId)
      .then((result) => {
        if (cancelled) return;
        if (result.success && result.data && Object.keys(result.data.artifacts).length > 0) {
          setArtifacts(result.data.artifacts);
          const names = Object.keys(result.data.artifacts);
          setActiveTab(names.includes('spec.md') ? 'spec.md' : names[0]);
        }
      })
      .catch(() => { /* fallback to command text */ });
    return () => { cancelled = true; };
  }, [sessionId]);

  const fallbackContent = useMemo(() => {
    const cmd = request.command || '';
    return cmd.replace(/^Plan completed\. Exit plan mode\?\s*/, '').trim();
  }, [request.command]);

  const tabNames = artifacts ? Object.keys(artifacts) : [];
  const currentContent = artifacts && activeTab ? artifacts[activeTab] : fallbackContent;

  const handleStartEdit = useCallback(() => {
    if (!artifacts || !activeTab) return;
    setEditedContent(artifacts[activeTab]);
    setViewMode('edit');
    setSaveError(null);
  }, [artifacts, activeTab]);

  const handleSaveEdit = async () => {
    if (!activeTab) return;
    setIsSaving(true);
    setSaveError(null);
    try {
      const result = await updatePlanDraftArtifact(sessionId, activeTab, editedContent);
      if (result.success) {
        setArtifacts((prev) => (prev ? { ...prev, [activeTab]: editedContent } : prev));
        setViewMode('preview');
      } else {
        setSaveError(result.message || 'Failed to save');
      }
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setIsSaving(false);
    }
  };

  const handleStartFeedback = () => {
    setViewMode('feedback');
    setFeedback('');
  };

  const handleSubmitFeedback = () => {
    onDeny(feedback.trim() || undefined);
  };

  return (
    <div className="border-t px-4 py-3 border-blue-300 bg-blue-50">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-sm font-medium text-blue-800">📋 计划审批</span>
            <span className="text-xs px-1.5 py-0.5 rounded bg-blue-200 text-blue-800">exit_plan_mode</span>
            {tabNames.length > 1 && viewMode !== 'feedback' && (
              <div className="flex items-center gap-1 ml-2">
                {tabNames.map((name) => (
                  <button
                    key={name}
                    onClick={() => { setActiveTab(name); setViewMode('preview'); }}
                    className={`px-2 py-0.5 text-xs rounded transition-colors ${
                      activeTab === name
                        ? 'bg-blue-600 text-white'
                        : 'bg-white text-blue-700 border border-blue-300 hover:bg-blue-50'
                    }`}
                  >
                    {TAB_LABELS[name] || name}
                  </button>
                ))}
              </div>
            )}
            <div className="flex-1" />
            {viewMode === 'preview' && (
              <div className="flex items-center gap-1">
                {artifacts && activeTab && (
                  <button
                    onClick={handleStartEdit}
                    className="flex items-center gap-1 px-2 py-1 text-xs text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
                    title="编辑当前文档"
                  >
                    <Edit3 className="w-3 h-3" />
                    编辑
                  </button>
                )}
                <button
                  onClick={handleStartFeedback}
                  className="flex items-center gap-1 px-2 py-1 text-xs text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
                  title="提出修改意见"
                >
                  <MessageSquare className="w-3 h-3" />
                  修改意见
                </button>
              </div>
            )}
          </div>

          {viewMode === 'preview' && (
            <div className="bg-white border border-blue-200 rounded p-3 max-h-96 overflow-y-auto">
              <div className="prose prose-sm max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {currentContent || '（空计划）'}
                </ReactMarkdown>
              </div>
            </div>
          )}

          {viewMode === 'edit' && (
            <div className="space-y-2">
              <textarea
                value={editedContent}
                onChange={(e) => setEditedContent(e.target.value)}
                className="w-full h-64 p-3 text-sm font-mono bg-white border border-blue-300 rounded resize-y focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder="编辑计划内容..."
              />
              {saveError && <p className="text-xs text-red-600">{saveError}</p>}
              <div className="flex items-center gap-2">
                <button
                  onClick={handleSaveEdit}
                  disabled={isSaving}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-white bg-blue-600 border border-blue-700 rounded hover:bg-blue-700 disabled:opacity-50 transition-colors"
                >
                  {isSaving ? '保存中...' : '保存修改'}
                </button>
                <button
                  onClick={() => { setViewMode('preview'); setSaveError(null); }}
                  className="px-3 py-1.5 text-sm font-medium text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
                >
                  取消
                </button>
              </div>
            </div>
          )}

          {viewMode === 'feedback' && (
            <div className="space-y-2">
              <textarea
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                className="w-full h-32 p-3 text-sm bg-white border border-blue-300 rounded resize-y focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder="请输入修改意见，AI 将根据您的意见修改计划..."
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={handleSubmitFeedback}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-white bg-orange-600 border border-orange-700 rounded hover:bg-orange-700 transition-colors"
                >
                  <MessageSquare className="w-3 h-3" />
                  提交意见
                </button>
                <button
                  onClick={() => { setViewMode('preview'); setFeedback(''); }}
                  className="px-3 py-1.5 text-sm font-medium text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </div>

        {viewMode === 'preview' && (
          <div className="flex flex-col gap-2 flex-shrink-0">
            <button
              onClick={() => onApprove('execute')}
              className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-white bg-blue-600 border border-blue-700 rounded hover:bg-blue-700 transition-colors"
              title="批准后进入 acceptEdits 模式自动执行"
            >
              <Zap className="w-3 h-3" />
              批准并自动执行
            </button>
            <button
              onClick={() => onApprove('manual-execute')}
              className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
              title="批准后回到原权限模式，逐步确认执行"
            >
              <StepForward className="w-3 h-3" />
              批准并逐步确认
            </button>
            <button
              onClick={() => onApprove('clear-and-execute')}
              className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
              title="清空对话上下文后自动执行（长规划省 token）"
            >
              <Eraser className="w-3 h-3" />
              清空上下文执行
            </button>
            <button
              onClick={() => onDeny()}
              className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
            >
              <X className="w-3 h-3" />
              继续修改
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

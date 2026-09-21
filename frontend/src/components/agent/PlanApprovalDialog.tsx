import { useState, useEffect, useMemo, useCallback } from 'react';
import { MessageSquare, Edit3, Zap, StepForward, Eraser, X, Check, Loader2, FileText } from 'lucide-react';
import type { PermissionRequest } from '../../store/SessionStore';
import { getPlanDraftArtifacts, updatePlanDraftArtifact } from '../../api/client';
import { Markdown } from '../chat/markdown';

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

/**
 * 计划审批抽屉：右侧全高 drawer，长文档舒适阅读。
 *
 * - tab 切换草稿产物（spec/design/tasks/plan/proposal）
 * - 支持直接编辑保存当前文档
 * - 修改意见（feedback）注入回规划循环
 * - 三种批准路由：execute / manual-execute / clear-and-execute
 * - ⌘+Enter（Ctrl+Enter）快捷批准并自动执行
 */
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

  const handleSubmitFeedback = () => {
    onDeny(feedback.trim() || undefined);
  };

  // ⌘+Enter / Ctrl+Enter 快捷批准（仅 preview 态）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && viewMode === 'preview') {
        e.preventDefault();
        onApprove('execute');
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [viewMode, onApprove]);

  return (
    <>
      {/* Overlay */}
      <div className="fixed inset-0 bg-black/20 z-40" onClick={() => viewMode === 'preview' && onDeny()} />

      {/* Drawer */}
      <div className="fixed right-0 top-0 h-full w-[600px] max-w-[92vw] bg-white shadow-2xl z-50 flex flex-col animate-slide-in-right border-l border-gray-200">
        {/* Header */}
        <div className="flex items-center gap-2 px-5 py-3.5 border-b border-gray-200 flex-shrink-0">
          <FileText className="w-4 h-4 text-indigo-600" />
          <h2 className="text-sm font-semibold text-gray-900">计划审批</h2>
          <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 font-mono">exit_plan_mode</span>
          <div className="flex-1" />
          {tabNames.length > 1 && viewMode === 'preview' && (
            <div className="flex items-center gap-0.5 bg-gray-100 rounded-lg p-0.5">
              {tabNames.map((name) => (
                <button
                  key={name}
                  onClick={() => setActiveTab(name)}
                  className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${
                    activeTab === name
                      ? 'bg-white text-gray-900 shadow-sm'
                      : 'text-gray-500 hover:text-gray-700'
                  }`}
                >
                  {TAB_LABELS[name] || name}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto min-h-0">
          {viewMode === 'preview' && (
            <div className="px-6 py-5">
              {currentContent ? (
                <Markdown content={currentContent} linkifyFiles={false} />
              ) : (
                <p className="text-sm text-gray-400">（空计划）</p>
              )}
            </div>
          )}

          {viewMode === 'edit' && (
            <div className="h-full flex flex-col p-5">
              <textarea
                value={editedContent}
                onChange={(e) => setEditedContent(e.target.value)}
                className="input-base flex-1 font-mono text-[13px] resize-none min-h-0"
                placeholder="编辑计划内容..."
                autoFocus
              />
              {saveError && <p className="mt-2 text-xs text-danger">{saveError}</p>}
              <div className="flex items-center gap-2 mt-3">
                <button onClick={handleSaveEdit} disabled={isSaving} className="btn-primary btn-sm">
                  {isSaving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
                  保存修改
                </button>
                <button
                  onClick={() => { setViewMode('preview'); setSaveError(null); }}
                  className="btn-ghost btn-sm"
                >
                  取消
                </button>
              </div>
            </div>
          )}

          {viewMode === 'feedback' && (
            <div className="p-5 space-y-3">
              <p className="text-xs text-gray-500">修改意见将发回给 Agent，计划会继续修改后重新提交审批。</p>
              <textarea
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                className="input-base h-40 resize-y"
                placeholder="例如：任务 3 的接口设计不合理，应该……"
                autoFocus
              />
              <div className="flex items-center gap-2">
                <button onClick={handleSubmitFeedback} className="btn-primary btn-sm">
                  <MessageSquare className="w-3 h-3" />
                  提交意见
                </button>
                <button
                  onClick={() => { setViewMode('preview'); setFeedback(''); }}
                  className="btn-ghost btn-sm"
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Footer 操作栏 */}
        {viewMode === 'preview' && (
          <div className="flex-shrink-0 border-t border-gray-200 px-5 py-3.5 bg-gray-50/50">
            <div className="flex items-center gap-2">
              <button
                onClick={() => onApprove('execute')}
                className="btn-primary"
                title="批准后进入自动执行模式（⌘+Enter）"
              >
                <Zap className="w-3.5 h-3.5" />
                批准并自动执行
              </button>
              <button
                onClick={() => onApprove('manual-execute')}
                className="btn-secondary"
                title="批准后回到原权限模式，每步编辑仍需确认"
              >
                <StepForward className="w-3.5 h-3.5" />
                逐步确认
              </button>
              <button
                onClick={() => onApprove('clear-and-execute')}
                className="btn-secondary"
                title="清空对话上下文后自动执行（长规划省 token）"
              >
                <Eraser className="w-3.5 h-3.5" />
                清空执行
              </button>
              <div className="flex-1" />
              {artifacts && activeTab && (
                <button onClick={handleStartEdit} className="btn-ghost" title="编辑当前文档">
                  <Edit3 className="w-3.5 h-3.5" />
                  编辑
                </button>
              )}
              <button onClick={() => setViewMode('feedback')} className="btn-ghost" title="提出修改意见">
                <MessageSquare className="w-3.5 h-3.5" />
                修改意见
              </button>
              <button onClick={() => onDeny()} className="btn-ghost text-gray-400" title="拒绝，继续规划">
                <X className="w-4 h-4" />
              </button>
            </div>
            <p className="mt-2 text-[11px] text-gray-400">
              ⌘+Enter 快速批准 · 点击遮罩拒绝并继续规划
            </p>
          </div>
        )}
      </div>
    </>
  );
}

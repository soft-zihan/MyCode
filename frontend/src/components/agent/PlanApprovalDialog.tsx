import { useState, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Edit3, MessageSquare, Check, X } from 'lucide-react';
import type { PermissionRequest } from '../../store/SessionStore';
import { updatePlanFile } from '../../api/client';

interface PlanApprovalDialogProps {
  request: PermissionRequest;
  sessionId: string;
  onApprove: () => void;
  onDeny: (feedback?: string) => void;
}

type ViewMode = 'preview' | 'edit' | 'feedback';

export function PlanApprovalDialog({ request, sessionId, onApprove, onDeny }: PlanApprovalDialogProps) {
  const [viewMode, setViewMode] = useState<ViewMode>('preview');
  const [editedContent, setEditedContent] = useState('');
  const [feedback, setFeedback] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const planContent = useMemo(() => {
    const cmd = request.command || '';
    const planMatch = cmd.match(/## Plan:\n([\s\S]*)$/);
    return planMatch ? planMatch[1].trim() : cmd;
  }, [request.command]);

  const handleStartEdit = () => {
    setEditedContent(planContent);
    setViewMode('edit');
    setSaveError(null);
  };

  const handleSaveEdit = async () => {
    if (!request.plan_file_path) {
      setSaveError('No plan file path available');
      return;
    }
    setIsSaving(true);
    setSaveError(null);
    try {
      const result = await updatePlanFile(sessionId, request.plan_file_path, editedContent);
      if (result.success) {
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

  const handleCancelEdit = () => {
    setViewMode('preview');
    setSaveError(null);
  };

  const handleStartFeedback = () => {
    setViewMode('feedback');
    setFeedback('');
  };

  const handleSubmitFeedback = () => {
    onDeny(feedback.trim() || undefined);
  };

  const handleCancelFeedback = () => {
    setViewMode('preview');
    setFeedback('');
  };

  return (
    <div className="border-t px-4 py-3 border-blue-300 bg-blue-50">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-sm font-medium text-blue-800">
              📋 计划审批
            </span>
            <span className="text-xs px-1.5 py-0.5 rounded bg-blue-200 text-blue-800">
              exit_plan_mode
            </span>
            <div className="flex-1" />
            {viewMode === 'preview' && (
              <div className="flex items-center gap-1">
                <button
                  onClick={handleStartEdit}
                  className="flex items-center gap-1 px-2 py-1 text-xs text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
                  title="编辑计划"
                >
                  <Edit3 className="w-3 h-3" />
                  编辑
                </button>
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
                  {planContent}
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
              {saveError && (
                <p className="text-xs text-red-600">{saveError}</p>
              )}
              <div className="flex items-center gap-2">
                <button
                  onClick={handleSaveEdit}
                  disabled={isSaving}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-white bg-blue-600 border border-blue-700 rounded hover:bg-blue-700 disabled:opacity-50 transition-colors"
                >
                  {isSaving ? '保存中...' : '保存修改'}
                </button>
                <button
                  onClick={handleCancelEdit}
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
                  onClick={handleCancelFeedback}
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
              onClick={() => onDeny()}
              className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-blue-700 bg-white border border-blue-300 rounded hover:bg-blue-50 transition-colors"
            >
              <X className="w-3 h-3" />
              继续修改
            </button>
            <button
              onClick={onApprove}
              className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-white bg-blue-600 border border-blue-700 rounded hover:bg-blue-700 transition-colors"
            >
              <Check className="w-3 h-3" />
              批准计划
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

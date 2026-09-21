import { useState, useEffect } from 'react';
import { X, FilePlus, FileEdit, FileMinus, Loader2, Check, RotateCcw, MessageSquare, AlertTriangle } from 'lucide-react';
import { stageRewind, commitRewind, clearRewind } from '../../api/client';
import type { RewindPlan, RewindFileChange } from '../../api/client';

interface RewindDialogProps {
  sessionId: string;
  /** 回退目标：turns 与 keepUserMessages 二选一（与后端 stage 契约一致） */
  turns?: number;
  keepUserMessages?: number;
  /** 已经 stage 过的计划（编辑消息流程复用，避免二次 stage） */
  preStagedPlan?: RewindPlan | null;
  onClose: () => void;
  onCommitted: () => void;
}

function FileStatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'added':
      return <FilePlus className="w-4 h-4 text-green-500" />;
    case 'modified':
      return <FileEdit className="w-4 h-4 text-yellow-500" />;
    case 'deleted':
      return <FileMinus className="w-4 h-4 text-red-500" />;
    default:
      return null;
  }
}

function FileChangeItem({ change }: { change: RewindFileChange }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="border border-gray-200 rounded">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-gray-50 text-left"
      >
        <FileStatusIcon status={change.status} />
        <span className="flex-1 text-sm font-mono truncate">{change.path}</span>
        <span className="text-xs text-gray-500 capitalize">{change.status}</span>
      </button>
      {expanded && change.patch && (
        <pre className="px-3 py-2 bg-gray-50 border-t text-xs overflow-x-auto max-h-48">
          <code>{change.patch}</code>
        </pre>
      )}
    </div>
  );
}

export function RewindDialog({
  sessionId,
  turns,
  keepUserMessages,
  preStagedPlan,
  onClose,
  onCommitted,
}: RewindDialogProps) {
  const [plan, setPlan] = useState<RewindPlan | null>(preStagedPlan ?? null);
  const [loading, setLoading] = useState(!preStagedPlan);
  const [error, setError] = useState<string | null>(null);
  const [committing, setCommitting] = useState(false);

  useEffect(() => {
    if (preStagedPlan) return;
    const loadPlan = async () => {
      try {
        setLoading(true);
        const staged = await stageRewind(sessionId, { turns, keepUserMessages });
        setPlan(staged);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load rewind plan');
      } finally {
        setLoading(false);
      }
    };
    loadPlan();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const handleCommit = async () => {
    if (!plan) return;
    try {
      setCommitting(true);
      await commitRewind(sessionId, plan.plan_id);
      onCommitted();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to commit rewind');
    } finally {
      setCommitting(false);
    }
  };

  const handleCancel = async () => {
    if (plan) {
      try {
        await clearRewind(sessionId, plan.plan_id);
      } catch {
        // 计划可能已过期，取消场景下无副作用，忽略
      }
    }
    onClose();
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white rounded-lg p-6 flex items-center gap-3">
          <Loader2 className="w-5 h-5 animate-spin text-indigo-500" />
          <span>正在计算回退计划...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white rounded-lg p-6 max-w-md">
          <h3 className="text-lg font-semibold text-red-600 mb-2">错误</h3>
          <p className="text-sm text-gray-600 mb-4">{error}</p>
          <button
            onClick={onClose}
            className="px-4 py-2 bg-gray-100 hover:bg-gray-200 rounded text-sm"
          >
            关闭
          </button>
        </div>
      </div>
    );
  }

  if (!plan) return null;

  const addedCount = plan.file_changes.filter(c => c.status === 'added').length;
  const modifiedCount = plan.file_changes.filter(c => c.status === 'modified').length;
  const deletedCount = plan.file_changes.filter(c => c.status === 'deleted').length;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-lg w-full max-w-2xl max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <RotateCcw className="w-5 h-5 text-indigo-500" />
            回退到这个时间点？
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4">
          <div className="mb-4 p-3 bg-indigo-50 rounded flex items-start gap-2">
            <MessageSquare className="w-4 h-4 text-indigo-500 mt-0.5 shrink-0" />
            <div className="text-sm text-gray-700">
              <p>
                对话将删除 <span className="font-semibold">{plan.removed_user_messages}</span> 条用户消息
                （共 {plan.removed_events} 个事件），回退锚点：
              </p>
              <p className="mt-1 text-xs text-gray-500 font-mono truncate">
                {plan.target_message.content.slice(0, 80) || '(空消息)'}
              </p>
            </div>
          </div>

          {!plan.has_snapshot && (
            <div className="mb-4 p-3 bg-yellow-50 rounded flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-yellow-600 mt-0.5 shrink-0" />
              <p className="text-sm text-yellow-800">
                该消息没有文件快照，本次仅回退对话，文件保持当前状态。
              </p>
            </div>
          )}

          {plan.has_snapshot && (
            <div className="mb-4">
              <p className="text-sm text-gray-600 mb-2">
                文件将恢复到该消息发送时的状态：
              </p>
              <div className="flex gap-4 text-sm">
                {addedCount > 0 && <span className="text-green-600">- {addedCount} 新增（将删除）</span>}
                {modifiedCount > 0 && <span className="text-yellow-600">~ {modifiedCount} 修改（将还原）</span>}
                {deletedCount > 0 && <span className="text-red-600">+ {deletedCount} 删除（将恢复）</span>}
                {plan.file_changes.length === 0 && <span className="text-gray-500">无文件变更</span>}
              </div>
            </div>
          )}

          {plan.file_changes.length > 0 && (
            <div className="space-y-2">
              {plan.file_changes.map((change, idx) => (
                <FileChangeItem key={idx} change={change} />
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t bg-gray-50">
          <button
            onClick={handleCancel}
            disabled={committing}
            className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-200 rounded flex items-center gap-2 disabled:opacity-50"
          >
            <X className="w-4 h-4" />
            取消
          </button>
          <button
            onClick={handleCommit}
            disabled={committing}
            className="px-4 py-2 text-sm text-white bg-indigo-500 hover:bg-indigo-600 rounded flex items-center gap-2 disabled:opacity-50"
          >
            {committing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            确认回退
          </button>
        </div>
      </div>
    </div>
  );
}

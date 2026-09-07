import { useState, useEffect } from 'react';
import { X, FilePlus, FileEdit, FileMinus, Loader2, Check, RotateCcw } from 'lucide-react';
import { stageRevert, commitRevert, clearRevert } from '../../api/client';
import type { RevertPlan, FileDiff } from '../../api/client';

interface RewindDialogProps {
  sessionId: string;
  snapshotId: string;
  messageId?: string;
  onClose: () => void;
  onConfirm: () => void;
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

function FileChangeItem({ change }: { change: FileDiff }) {
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

export function RewindDialog({ sessionId, snapshotId, messageId, onClose, onConfirm }: RewindDialogProps) {
  const [plan, setPlan] = useState<RevertPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [committing, setCommitting] = useState(false);
  const [clearing, setClearing] = useState(false);

  useEffect(() => {
    const loadPlan = async () => {
      try {
        setLoading(true);
        const revertPlan = await stageRevert(sessionId, snapshotId, messageId);
        setPlan(revertPlan);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load revert plan');
      } finally {
        setLoading(false);
      }
    };
    loadPlan();
  }, [sessionId, snapshotId, messageId]);

  const handleCommit = async () => {
    if (!plan) return;
    try {
      setCommitting(true);
      await commitRevert(plan.id);
      onConfirm();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to commit revert');
    } finally {
      setCommitting(false);
    }
  };

  const handleClear = async () => {
    if (!plan) return;
    try {
      setClearing(true);
      await clearRevert(plan.id);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear revert');
    } finally {
      setClearing(false);
    }
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white rounded-lg p-6 flex items-center gap-3">
          <Loader2 className="w-5 h-5 animate-spin text-blue-500" />
          <span>正在计算变更...</span>
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

  const addedCount = plan.changes.filter(c => c.status === 'added').length;
  const modifiedCount = plan.changes.filter(c => c.status === 'modified').length;
  const deletedCount = plan.changes.filter(c => c.status === 'deleted').length;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-lg w-full max-w-2xl max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <RotateCcw className="w-5 h-5 text-blue-500" />
            恢复到这个时间点？
          </h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-gray-100 rounded"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4">
          <div className="mb-4">
            <p className="text-sm text-gray-600 mb-2">
              此操作将恢复以下文件到快照时的状态：
            </p>
            <div className="flex gap-4 text-sm">
              {addedCount > 0 && (
                <span className="text-green-600">+ {addedCount} 新增</span>
              )}
              {modifiedCount > 0 && (
                <span className="text-yellow-600">~ {modifiedCount} 修改</span>
              )}
              {deletedCount > 0 && (
                <span className="text-red-600">- {deletedCount} 删除</span>
              )}
              {plan.changes.length === 0 && (
                <span className="text-gray-500">无文件变更</span>
              )}
            </div>
          </div>

          {plan.changes.length > 0 && (
            <div className="space-y-2">
              {plan.changes.map((change, idx) => (
                <FileChangeItem key={idx} change={change} />
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t bg-gray-50">
          <button
            onClick={handleClear}
            disabled={clearing}
            className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-200 rounded flex items-center gap-2 disabled:opacity-50"
          >
            {clearing ? <Loader2 className="w-4 h-4 animate-spin" /> : <X className="w-4 h-4" />}
            取消
          </button>
          <button
            onClick={handleCommit}
            disabled={committing || plan.changes.length === 0}
            className="px-4 py-2 text-sm text-white bg-blue-500 hover:bg-blue-600 rounded flex items-center gap-2 disabled:opacity-50"
          >
            {committing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            确认恢复
          </button>
        </div>
      </div>
    </div>
  );
}

import { useState, useEffect } from 'react';
import { CheckCircle, XCircle, Clock, Play, Pause, SkipForward, X } from 'lucide-react';
import { getPlanStatus, planPause, planResume, planSkipTask, planAbandon } from '../../api/client';

interface PlanProgress {
  slug: string;
  status: string;
  tasks: {
    total: number;
    done: number;
    failed: number;
    pending: number;
  };
  task_list?: { id: number; title: string; file?: string; status: string }[];
}

interface PlanProgressPanelProps {
  sessionId: string;
  planSlug: string;
}

export function PlanProgressPanel({ sessionId, planSlug }: PlanProgressPanelProps) {
  const [progress, setProgress] = useState<PlanProgress | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchProgress = async () => {
    try {
      setLoading(true);
      const result = await getPlanStatus(sessionId, planSlug);
      if (result.success && result.data) {
        setProgress(result.data);
      } else {
        setError(result.message || 'Failed to fetch progress');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch progress');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchProgress();
    const interval = setInterval(fetchProgress, 3000);
    return () => clearInterval(interval);
  }, [sessionId, planSlug]);

  const handlePause = async () => {
    await planPause(sessionId, planSlug);
    fetchProgress();
  };

  const handleResume = async () => {
    await planResume(sessionId, planSlug);
    fetchProgress();
  };

  const handleSkip = async (taskId: number) => {
    await planSkipTask(sessionId, planSlug, taskId);
    fetchProgress();
  };

  const handleAbandon = async () => {
    if (confirm('确定要终止此计划吗？')) {
      await planAbandon(sessionId, planSlug);
      fetchProgress();
    }
  };

  if (loading && !progress) {
    return <div className="p-4 text-sm text-gray-500">加载中...</div>;
  }

  if (error) {
    if (error.includes('not found')) {
      return null;
    }
    return <div className="p-4 text-sm text-red-500">错误: {error}</div>;
  }

  if (!progress) {
    return null;
  }

  const { tasks } = progress;
  const progressPercent = tasks.total > 0 ? Math.round((tasks.done / tasks.total) * 100) : 0;
  const isPaused = progress.status === 'paused';
  const isCompleted = progress.status === 'completed' || progress.status === 'ready_to_archive';

  return (
    <div className="border-t px-4 py-3 border-green-300 bg-green-50">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-sm font-medium text-green-800">
          📋 计划执行进度
        </span>
        <span className="text-xs px-1.5 py-0.5 rounded bg-green-200 text-green-800">
          {progress.slug}
        </span>
        <div className="flex-1" />
        {!isCompleted && (
          <div className="flex items-center gap-1">
            {isPaused ? (
              <button
                onClick={handleResume}
                className="flex items-center gap-1 px-2 py-1 text-xs text-green-700 bg-white border border-green-300 rounded hover:bg-green-50 transition-colors"
                title="继续执行"
              >
                <Play className="w-3 h-3" />
                继续
              </button>
            ) : (
              <button
                onClick={handlePause}
                className="flex items-center gap-1 px-2 py-1 text-xs text-green-700 bg-white border border-green-300 rounded hover:bg-green-50 transition-colors"
                title="暂停执行"
              >
                <Pause className="w-3 h-3" />
                暂停
              </button>
            )}
            <button
              onClick={handleAbandon}
              className="flex items-center gap-1 px-2 py-1 text-xs text-red-700 bg-white border border-red-300 rounded hover:bg-red-50 transition-colors"
              title="终止计划"
            >
              <X className="w-3 h-3" />
              终止
            </button>
          </div>
        )}
      </div>

      <div className="bg-white border border-green-200 rounded p-3">
        <div className="flex items-center gap-3 mb-2">
          <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
            <div
              className="h-full bg-green-500 transition-all duration-300"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
          <span className="text-sm font-medium text-gray-700">
            {tasks.done}/{tasks.total} ({progressPercent}%)
          </span>
        </div>

        {progress.task_list && progress.task_list.length > 0 && (
          <div className="space-y-1 mt-3">
            {progress.task_list.map((task) => (
              <div
                key={task.id}
                className="flex items-center gap-2 text-xs"
              >
                {task.status === 'done' && (
                  <CheckCircle className="w-4 h-4 text-green-500 flex-shrink-0" />
                )}
                {task.status === 'failed' && (
                  <XCircle className="w-4 h-4 text-red-500 flex-shrink-0" />
                )}
                {task.status === 'in-progress' && (
                  <Clock className="w-4 h-4 text-blue-500 animate-pulse flex-shrink-0" />
                )}
                {task.status === 'pending' && (
                  <Clock className="w-4 h-4 text-gray-400 flex-shrink-0" />
                )}
                {task.status === 'skipped' && (
                  <SkipForward className="w-4 h-4 text-gray-400 flex-shrink-0" />
                )}
                <span className={`flex-1 ${
                  task.status === 'done' ? 'text-green-700 line-through' :
                  task.status === 'failed' ? 'text-red-700' :
                  task.status === 'in-progress' ? 'text-blue-700 font-medium' :
                  task.status === 'skipped' ? 'text-gray-400 line-through' :
                  'text-gray-600'
                }`}>
                  {task.id}. {task.title}
                </span>
                {task.status === 'pending' && !isCompleted && (
                  <button
                    onClick={() => handleSkip(task.id)}
                    className="text-gray-400 hover:text-gray-600 transition-colors"
                    title="跳过此任务"
                  >
                    <SkipForward className="w-3 h-3" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {isCompleted && (
          <div className="mt-3 pt-3 border-t border-green-200">
            <p className="text-sm text-green-700 font-medium">
              ✅ 计划执行完成
            </p>
          </div>
        )}

        {tasks.failed > 0 && (
          <div className="mt-3 pt-3 border-t border-red-200">
            <p className="text-sm text-red-700">
              ⚠️ {tasks.failed} 个任务失败，请检查日志
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

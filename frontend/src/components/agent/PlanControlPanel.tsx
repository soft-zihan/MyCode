import { useState, useEffect } from 'react';
import { CheckCircle, XCircle, Clock, Play, Pause, SkipForward, X, FileText, List, History, ChevronDown, ChevronRight } from 'lucide-react';
import { getPlanStatus, planPause, planResume, planSkipTask, planAbandon, getPlanArtifacts, getPlanLedger } from '../../api/client';

interface PlanProgress {
  slug: string;
  status: string;
  tasks: {
    total: number;
    done: number;
    failed: number;
    pending: number;
  };
  task_list?: { id: number; description: string; status: string }[];
}

interface PlanControlPanelProps {
  sessionId: string;
  planSlug: string;
}

type SubTab = 'progress' | 'artifacts' | 'ledger';

export function PlanControlPanel({ sessionId, planSlug }: PlanControlPanelProps) {
  const [progress, setProgress] = useState<PlanProgress | null>(null);
  const [artifacts, setArtifacts] = useState<Record<string, string>>({});
  const [ledger, setLedger] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [subTab, setSubTab] = useState<SubTab>('progress');
  const [expandedArtifact, setExpandedArtifact] = useState<string | null>(null);

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

  const fetchArtifacts = async () => {
    try {
      const result = await getPlanArtifacts(sessionId, planSlug);
      if (result.success && result.data) {
        setArtifacts(result.data);
      }
    } catch (err) {
      // Silent fail for artifacts
    }
  };

  const fetchLedger = async () => {
    try {
      const result = await getPlanLedger(sessionId, planSlug);
      if (result.success && result.data) {
        setLedger(result.data);
      }
    } catch (err) {
      // Silent fail for ledger
    }
  };

  useEffect(() => {
    fetchProgress();
    fetchArtifacts();
    fetchLedger();
    const interval = setInterval(() => {
      fetchProgress();
      fetchLedger();
    }, 3000);
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
      return <div className="p-4 text-sm text-gray-500">计划已归档</div>;
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
    <div className="flex flex-col h-full">
      {/* Header with controls */}
      <div className="px-3 py-2 border-b border-gray-200 bg-green-50">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs font-medium text-green-800">
            📋 Plan: {progress.slug}
          </span>
          <span className={`text-xs px-1.5 py-0.5 rounded ${
            isCompleted ? 'bg-green-200 text-green-800' :
            isPaused ? 'bg-yellow-200 text-yellow-800' :
            'bg-blue-200 text-blue-800'
          }`}>
            {progress.status}
          </span>
          <div className="flex-1" />
          {!isCompleted && (
            <div className="flex items-center gap-1">
              {isPaused ? (
                <button
                  onClick={handleResume}
                  className="p-1 text-green-700 hover:bg-green-100 rounded"
                  title="继续执行"
                >
                  <Play className="w-3.5 h-3.5" />
                </button>
              ) : (
                <button
                  onClick={handlePause}
                  className="p-1 text-green-700 hover:bg-green-100 rounded"
                  title="暂停执行"
                >
                  <Pause className="w-3.5 h-3.5" />
                </button>
              )}
              <button
                onClick={handleAbandon}
                className="p-1 text-red-700 hover:bg-red-100 rounded"
                title="终止计划"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>

        {/* Progress bar */}
        <div className="flex items-center gap-2">
          <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
            <div
              className="h-full bg-green-500 transition-all duration-300"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
          <span className="text-xs font-medium text-gray-600">
            {tasks.done}/{tasks.total}
          </span>
        </div>
      </div>

      {/* Sub tabs */}
      <div className="flex border-b border-gray-200">
        <button
          onClick={() => setSubTab('progress')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'progress' ? 'text-blue-600 border-b-2 border-blue-600' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <List className="w-3 h-3 inline mr-1" />
          Tasks
        </button>
        <button
          onClick={() => setSubTab('artifacts')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'artifacts' ? 'text-blue-600 border-b-2 border-blue-600' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <FileText className="w-3 h-3 inline mr-1" />
          Artifacts
        </button>
        <button
          onClick={() => setSubTab('ledger')}
          className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
            subTab === 'ledger' ? 'text-blue-600 border-b-2 border-blue-600' : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          <History className="w-3 h-3 inline mr-1" />
          Ledger
        </button>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-auto">
        {subTab === 'progress' && (
          <div className="p-2">
            {progress.task_list && progress.task_list.length > 0 ? (
              <div className="space-y-1">
                {progress.task_list.map((task) => (
                  <div
                    key={task.id}
                    className="flex items-center gap-2 text-xs p-1.5 rounded hover:bg-gray-50"
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
                      {task.id}. {task.description}
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
            ) : (
              <div className="text-xs text-gray-500 p-2">No tasks</div>
            )}

            {isCompleted && (
              <div className="mt-3 pt-3 border-t border-green-200">
                <p className="text-xs text-green-700 font-medium">
                  ✅ 计划执行完成
                </p>
              </div>
            )}

            {tasks.failed > 0 && (
              <div className="mt-3 pt-3 border-t border-red-200">
                <p className="text-xs text-red-700">
                  ⚠️ {tasks.failed} 个任务失败
                </p>
              </div>
            )}
          </div>
        )}

        {subTab === 'artifacts' && (
          <div className="p-2">
            {Object.keys(artifacts).length > 0 ? (
              <div className="space-y-1">
                {Object.entries(artifacts).map(([filename, content]) => (
                  <div key={filename} className="border border-gray-200 rounded">
                    <button
                      onClick={() => setExpandedArtifact(expandedArtifact === filename ? null : filename)}
                      className="w-full flex items-center gap-2 px-2 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
                    >
                      {expandedArtifact === filename ? (
                        <ChevronDown className="w-3 h-3" />
                      ) : (
                        <ChevronRight className="w-3 h-3" />
                      )}
                      <FileText className="w-3 h-3 text-blue-500" />
                      {filename}
                      <span className="text-gray-400 ml-auto">{content.length} chars</span>
                    </button>
                    {expandedArtifact === filename && (
                      <div className="border-t border-gray-200 p-2 max-h-48 overflow-auto">
                        <pre className="text-xs text-gray-600 whitespace-pre-wrap font-mono">
                          {content}
                        </pre>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-gray-500 p-2">No artifacts</div>
            )}
          </div>
        )}

        {subTab === 'ledger' && (
          <div className="p-2">
            {ledger.length > 0 ? (
              <div className="space-y-1">
                {ledger.slice().reverse().map((entry, i) => (
                  <div key={i} className="text-xs p-1.5 rounded bg-gray-50 border border-gray-100">
                    <div className="flex items-center gap-2">
                      <span className={`px-1 py-0.5 rounded text-[10px] font-medium ${
                        entry.status === 'done' ? 'bg-green-100 text-green-700' :
                        entry.status === 'failed' ? 'bg-red-100 text-red-700' :
                        entry.status === 'started' ? 'bg-blue-100 text-blue-700' :
                        'bg-gray-100 text-gray-700'
                      }`}>
                        {entry.status}
                      </span>
                      {entry.task_id && <span className="text-gray-500">Task {entry.task_id}</span>}
                      {entry.finished && (
                        <span className="text-gray-400 ml-auto text-[10px]">
                          {new Date(entry.finished).toLocaleTimeString()}
                        </span>
                      )}
                    </div>
                    {entry.verification?.command && (
                      <div className="mt-1 text-[10px] text-gray-500 font-mono truncate">
                        $ {entry.verification.command}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-gray-500 p-2">No ledger entries</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

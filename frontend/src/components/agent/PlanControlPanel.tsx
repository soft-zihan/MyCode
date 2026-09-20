import { useState, useEffect } from 'react';
import { useSessionStore } from '../../store/SessionStore';
import { CheckCircle, XCircle, Clock, Play, Pause, SkipForward, RotateCcw, X, FileText, List, History, ChevronDown, ChevronRight, Settings } from 'lucide-react';
import { getPlanStatus, planPause, planResume, planSkipTask, planRedoTask, planAbandon, getPlanArtifacts, getPlanLedger, fetchPlanStrategies, savePlanStrategies, getPlanDraftArtifacts, PlanStrategies } from '../../api/client';

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
  permissionMode?: string;
}

export function PlanControlPanel({ sessionId, planSlug, permissionMode }: PlanControlPanelProps) {
  const [progress, setProgress] = useState<PlanProgress | null>(null);
  const [artifacts, setArtifacts] = useState<Record<string, string>>({});
  const [ledger, setLedger] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedArtifact, setExpandedArtifact] = useState<string | null>(null);
  const [showExecution, setShowExecution] = useState(true);
  const [showArtifacts, setShowArtifacts] = useState(true);
  const [showStrategy, setShowStrategy] = useState(false);
  const [strategies, setStrategies] = useState<PlanStrategies | null>(null);
  const [draftArtifacts, setDraftArtifacts] = useState<Record<string, string> | null>(null);
  const [expandedDraft, setExpandedDraft] = useState<string | null>(null);

  useEffect(() => {
    fetchPlanStrategies().then(setStrategies).catch(console.error);
  }, []);

  const handleStrategyChange = async (key: keyof PlanStrategies, value: string) => {
    if (!strategies) return;
    const updated = { ...strategies, [key]: value };
    setStrategies(updated);
    try {
      await savePlanStrategies(updated);
    } catch (err) {
      console.error('Failed to save strategy:', err);
    }
  };

  const fetchProgress = async () => {
    if (!planSlug) return;
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
    if (!planSlug) return;
    try {
      const result = await getPlanArtifacts(sessionId, planSlug);
      if (result.success && result.data) {
        setArtifacts(result.data);
      }
    } catch (err) {
    }
  };

  const fetchLedger = async () => {
    if (!planSlug) return;
    try {
      const result = await getPlanLedger(sessionId, planSlug);
      if (result.success && result.data) {
        setLedger(result.data);
      }
    } catch (err) {
    }
  };

  const planRevision = useSessionStore((s) => s.getPlanRevision(sessionId));
  const isDraftMode = !planSlug && permissionMode === 'plan';

  useEffect(() => {
    if (!isDraftMode) {
      setDraftArtifacts(null);
      return;
    }
    let cancelled = false;
    getPlanDraftArtifacts(sessionId)
      .then((result) => {
        if (!cancelled && result.success && result.data) {
          setDraftArtifacts(result.data.artifacts);
        }
      })
      .catch(() => { /* 草稿尚未创建 */ });
    return () => { cancelled = true; };
  }, [sessionId, isDraftMode, planRevision]);

  useEffect(() => {
    if (!planSlug) return;
    fetchProgress();
    fetchArtifacts();
    fetchLedger();
  }, [sessionId, planSlug, planRevision]);

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

  const handleRedo = async (taskId: number) => {
    await planRedoTask(sessionId, planSlug, taskId);
    fetchProgress();
  };

  const handleAbandon = async () => {
    if (confirm('确定要终止此计划吗？')) {
      await planAbandon(sessionId, planSlug);
      fetchProgress();
    }
  };

  if (!strategies) {
    return (
      <div className="p-4 flex items-center justify-center">
        <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-500" />
      </div>
    );
  }

  if (!planSlug) {
    return (
      <div className="flex-1 overflow-auto p-3">
        {isDraftMode && draftArtifacts && Object.keys(draftArtifacts).length > 0 && (
          <div className="mb-4">
            <div className="text-xs font-medium text-gray-700 mb-2 flex items-center gap-1">
              <FileText className="w-3.5 h-3.5" />
              规划草稿（实时）
            </div>
            <div className="space-y-1">
              {Object.entries(draftArtifacts).map(([filename, content]) => (
                <div key={filename} className="border border-gray-200 rounded">
                  <button
                    onClick={() => setExpandedDraft(expandedDraft === filename ? null : filename)}
                    className="w-full flex items-center gap-1 px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50"
                  >
                    {expandedDraft === filename ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                    {filename}
                  </button>
                  {expandedDraft === filename && (
                    <pre className="px-2 pb-2 text-[11px] text-gray-600 whitespace-pre-wrap max-h-64 overflow-y-auto">{content}</pre>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="text-xs font-medium text-gray-700 mb-2 flex items-center gap-1">
          <Settings className="w-3.5 h-3.5" />
          Plan 策略配置
        </div>
        <div className="space-y-3">
          <div>
            <label className="block text-[10px] text-gray-500 mb-1">需求澄清策略</label>
            <select
              value={strategies.grill_spec}
              onChange={(e) => handleStrategyChange('grill_spec', e.target.value)}
              className="w-full text-xs border border-gray-200 rounded px-2 py-1"
            >
              <option value="simple">simple - 简单需求直接跳过</option>
              <option value="design-tree">design-tree - 复杂需求设计树</option>
            </select>
          </div>
          <div>
            <label className="block text-[10px] text-gray-500 mb-1">任务分解策略</label>
            <select
              value={strategies.tasks}
              onChange={(e) => handleStrategyChange('tasks', e.target.value)}
              className="w-full text-xs border border-gray-200 rounded px-2 py-1"
            >
              <option value="structured">structured - 结构化分解</option>
              <option value="flat">flat - 扁平列表</option>
              <option value="vertical-slice">vertical-slice - 垂直切片</option>
            </select>
          </div>
          <div>
            <label className="block text-[10px] text-gray-500 mb-1">审查策略</label>
            <select
              value={strategies.review}
              onChange={(e) => handleStrategyChange('review', e.target.value)}
              className="w-full text-xs border border-gray-200 rounded px-2 py-1"
            >
              <option value="single-axis">single-axis - 单轴审查</option>
              <option value="dual-axis">dual-axis - 双轴审查</option>
              <option value="none">none - 跳过审查</option>
            </select>
          </div>
          <div>
            <label className="block text-[10px] text-gray-500 mb-1">执行策略</label>
            <select
              value={strategies.execute}
              onChange={(e) => handleStrategyChange('execute', e.target.value)}
              className="w-full text-xs border border-gray-200 rounded px-2 py-1"
            >
              <option value="direct">direct - 直接执行</option>
              <option value="subagent">subagent - 子智能体执行</option>
              <option value="tdd">tdd - TDD 循环执行</option>
            </select>
          </div>
          <div>
            <label className="block text-[10px] text-gray-500 mb-1">收敛策略</label>
            <select
              value={strategies.converge}
              onChange={(e) => handleStrategyChange('converge', e.target.value)}
              className="w-full text-xs border border-gray-200 rounded px-2 py-1"
            >
              <option value="none">none - 跳过收敛</option>
              <option value="gap-analysis">gap-analysis - 差距分析</option>
            </select>
          </div>
        </div>
      </div>
    );
  }

  if (loading && !progress) {
    return <div className="p-4 text-sm text-gray-500">加载中...</div>;
  }

  if (error) {
    if (error.includes('not found')) {
      return (
        <div className="flex-1 overflow-auto flex flex-col">
          <div className="border-b border-gray-200">
            <button
              onClick={() => setShowStrategy(!showStrategy)}
              className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50"
            >
              {showStrategy ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
              <Settings className="w-3.5 h-3.5" />
              策略配置
            </button>
            {showStrategy && strategies && (
              <div className="px-3 pb-3 space-y-2">
                <div>
                  <label className="block text-[10px] text-gray-500 mb-1">需求澄清</label>
                  <select value={strategies.grill_spec} onChange={(e) => handleStrategyChange('grill_spec', e.target.value)} className="w-full text-xs border border-gray-200 rounded px-2 py-1">
                    <option value="simple">simple</option>
                    <option value="design-tree">design-tree</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[10px] text-gray-500 mb-1">任务分解</label>
                  <select value={strategies.tasks} onChange={(e) => handleStrategyChange('tasks', e.target.value)} className="w-full text-xs border border-gray-200 rounded px-2 py-1">
                    <option value="structured">structured</option>
                    <option value="flat">flat</option>
                    <option value="vertical-slice">vertical-slice</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[10px] text-gray-500 mb-1">审查</label>
                  <select value={strategies.review} onChange={(e) => handleStrategyChange('review', e.target.value)} className="w-full text-xs border border-gray-200 rounded px-2 py-1">
                    <option value="single-axis">single-axis</option>
                    <option value="dual-axis">dual-axis</option>
                    <option value="none">none</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[10px] text-gray-500 mb-1">执行</label>
                  <select value={strategies.execute} onChange={(e) => handleStrategyChange('execute', e.target.value)} className="w-full text-xs border border-gray-200 rounded px-2 py-1">
                    <option value="direct">direct</option>
                    <option value="subagent">subagent</option>
                    <option value="tdd">tdd</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[10px] text-gray-500 mb-1">收敛</label>
                  <select value={strategies.converge} onChange={(e) => handleStrategyChange('converge', e.target.value)} className="w-full text-xs border border-gray-200 rounded px-2 py-1">
                    <option value="none">none</option>
                    <option value="gap-analysis">gap-analysis</option>
                  </select>
                </div>
              </div>
            )}
          </div>
          <div className="px-3 py-2 bg-gray-50 border-b border-gray-200">
            <span className="text-xs text-gray-500">📋 计划已归档: {planSlug}</span>
          </div>
          <div className="border-b border-gray-200">
            <button
              onClick={() => setShowArtifacts(!showArtifacts)}
              className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50"
            >
              {showArtifacts ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
              产物查看
              <span className="ml-auto text-[10px] text-gray-500">{Object.keys(artifacts).length} 个文件</span>
            </button>
            {showArtifacts && (
              <div className="px-3 pb-2 max-h-[300px] overflow-auto">
                {Object.keys(artifacts).length > 0 ? (
                  <div className="space-y-1">
                    {Object.entries(artifacts).map(([filename, content]) => (
                      <div key={filename} className="border border-gray-200 rounded">
                        <button
                          onClick={() => setExpandedArtifact(expandedArtifact === filename ? null : filename)}
                          className="w-full flex items-center gap-2 px-2 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
                        >
                          {expandedArtifact === filename ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                          <FileText className="w-3 h-3 text-blue-500" />
                          {filename}
                          <span className="text-gray-400 ml-auto text-[10px]">{content.length} chars</span>
                        </button>
                        {expandedArtifact === filename && (
                          <div className="border-t border-gray-200 p-2 max-h-48 overflow-auto">
                            <pre className="text-xs text-gray-600 whitespace-pre-wrap font-mono">{content}</pre>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-xs text-gray-500 p-2">暂无产物文件</div>
                )}
              </div>
            )}
          </div>
        </div>
      );
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
  const currentTask = progress.task_list?.find(t => t.status === 'in-progress');
  const currentPhase = isCompleted ? '已完成' : isPaused ? '已暂停' : currentTask ? '执行中' : '准备中';

  return (
    <div className="flex-1 overflow-auto flex flex-col">
      <div className="border-b border-gray-200">
        <button
          onClick={() => setShowStrategy(!showStrategy)}
          className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50"
        >
          {showStrategy ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          <Settings className="w-3.5 h-3.5" />
          策略配置
        </button>
        {showStrategy && (
          <div className="px-3 pb-3 space-y-2">
            <div>
              <label className="block text-[10px] text-gray-500 mb-1">需求澄清</label>
              <select
                value={strategies.grill_spec}
                onChange={(e) => handleStrategyChange('grill_spec', e.target.value)}
                className="w-full text-xs border border-gray-200 rounded px-2 py-1"
              >
                <option value="simple">simple</option>
                <option value="design-tree">design-tree</option>
              </select>
            </div>
            <div>
              <label className="block text-[10px] text-gray-500 mb-1">任务分解</label>
              <select
                value={strategies.tasks}
                onChange={(e) => handleStrategyChange('tasks', e.target.value)}
                className="w-full text-xs border border-gray-200 rounded px-2 py-1"
              >
                <option value="structured">structured</option>
                <option value="flat">flat</option>
                <option value="vertical-slice">vertical-slice</option>
              </select>
            </div>
            <div>
              <label className="block text-[10px] text-gray-500 mb-1">审查</label>
              <select
                value={strategies.review}
                onChange={(e) => handleStrategyChange('review', e.target.value)}
                className="w-full text-xs border border-gray-200 rounded px-2 py-1"
              >
                <option value="single-axis">single-axis</option>
                <option value="dual-axis">dual-axis</option>
                <option value="none">none</option>
              </select>
            </div>
            <div>
              <label className="block text-[10px] text-gray-500 mb-1">执行</label>
              <select
                value={strategies.execute}
                onChange={(e) => handleStrategyChange('execute', e.target.value)}
                className="w-full text-xs border border-gray-200 rounded px-2 py-1"
              >
                <option value="direct">direct</option>
                <option value="subagent">subagent</option>
                <option value="tdd">tdd</option>
              </select>
            </div>
            <div>
              <label className="block text-[10px] text-gray-500 mb-1">收敛</label>
              <select
                value={strategies.converge}
                onChange={(e) => handleStrategyChange('converge', e.target.value)}
                className="w-full text-xs border border-gray-200 rounded px-2 py-1"
              >
                <option value="none">none</option>
                <option value="gap-analysis">gap-analysis</option>
              </select>
            </div>
          </div>
        )}
      </div>

      <div className="px-3 py-2 bg-green-50 border-b border-green-100">
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

      <div className="border-b border-gray-200">
        <button
          onClick={() => setShowExecution(!showExecution)}
          className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50"
        >
          {showExecution ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          执行状态
          <span className="ml-auto text-[10px] text-gray-500">{currentPhase}</span>
        </button>
        {showExecution && (
          <div className="px-3 pb-2 max-h-[250px] overflow-auto">
            <div className="space-y-1.5">
              <div className="flex items-center gap-2 text-[11px]">
                <span className="text-gray-500 w-16">当前环节:</span>
                <span className="text-gray-700 font-medium">{currentPhase}</span>
              </div>
              {currentTask && (
                <div className="flex items-center gap-2 text-[11px]">
                  <span className="text-gray-500 w-16">当前任务:</span>
                  <span className="text-gray-700 truncate flex-1">#{currentTask.id}. {currentTask.description}</span>
                </div>
              )}
              <div className="flex items-center gap-2 text-[11px]">
                <span className="text-gray-500 w-16">进度:</span>
                <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-blue-500 transition-all duration-300"
                    style={{ width: `${progressPercent}%` }}
                  />
                </div>
                <span className="text-gray-600 font-medium">{progressPercent}%</span>
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="border-b border-gray-200">
        <div className="flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-50">
          <List className="w-3 h-3" />
          任务列表
        </div>
        <div className="p-2 max-h-[200px] overflow-auto">
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
                  {task.status === 'pending' && (
                    <button
                      onClick={() => handleSkip(task.id)}
                      className="p-0.5 text-gray-400 hover:text-gray-600"
                      title="跳过"
                    >
                      <SkipForward className="w-3 h-3" />
                    </button>
                  )}
                  {(task.status === 'done' || task.status === 'skipped') && (
                    <button
                      onClick={() => handleRedo(task.id)}
                      className="p-0.5 text-gray-400 hover:text-gray-600"
                      title="重做"
                    >
                      <RotateCcw className="w-3 h-3" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="text-xs text-gray-500 p-2">No tasks yet</div>
          )}
        </div>
      </div>

      <div className="border-b border-gray-200">
        <button
          onClick={() => setShowArtifacts(!showArtifacts)}
          className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50"
        >
          {showArtifacts ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          产物查看
          <span className="ml-auto text-[10px] text-gray-500">{Object.keys(artifacts).length} 个文件</span>
        </button>
        {showArtifacts && (
          <div className="px-3 pb-2 max-h-[300px] overflow-auto">
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
                      <span className="text-gray-400 ml-auto text-[10px]">{content.length} chars</span>
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
              <div className="text-xs text-gray-500 p-2">暂无产物文件</div>
            )}
          </div>
        )}
      </div>

      <div className="flex-1 min-h-0 flex flex-col">
        <div className="flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-50 border-b border-gray-200">
          <History className="w-3 h-3" />
          执行日志
        </div>
        <div className="flex-1 overflow-auto p-2">
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
            <div className="text-xs text-gray-500 p-2">暂无执行日志</div>
          )}
        </div>
      </div>
    </div>
  );
}

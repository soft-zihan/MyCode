import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Activity, ExternalLink, Play, RefreshCw, Square, Gavel } from 'lucide-react';
import { PageLayout } from '../components/PageLayout';
import { wsManager } from '../store';
import {
  abortEvalRun,
  evalReportMarkdownUrl,
  fetchEvalBenchmarks,
  fetchEvalLangfuseInfo,
  fetchEvalRun,
  fetchEvalRuns,
  judgeEvalRun,
  langfuseDatasetUrl,
  langfuseSessionUrl,
  langfuseTraceUrl,
  startEvalRun,
} from '../api/client';
import type {
  EvalBenchmark,
  EvalBenchmarkSpec,
  EvalRun,
  EvalTaskResult,
  LangfuseInfo,
  StartEvalRunRequest,
} from '../api/client';

const DEFAULT_FORM: StartEvalRunRequest = {
  benchmark: 'gaia',
  sample: 5,
  seed: 42,
  timeout_s: 0,
  suite: 'chain',
  execution_mode: 'backend_session',
  sync_langfuse_dataset: true,
  judge_after_run: false,
  skip_langfuse: false,
  keep_sessions: true,
  thinking: null,
  compression_arm: null,
};

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-700',
  running: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  passed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  error: 'bg-orange-100 text-orange-700',
  aborted: 'bg-yellow-100 text-yellow-700',
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_STYLES[status] || 'bg-gray-100 text-gray-700'}`}>
      {status}
    </span>
  );
}

function formatTime(value?: number | null) {
  if (!value) return '-';
  return new Date(value * 1000).toLocaleString('zh-CN');
}

function formatPercent(value?: number | null) {
  if (value === null || value === undefined) return '-';
  return `${(value * 100).toFixed(1)}%`;
}

export default function EvalPage() {
  const [benchmarks, setBenchmarks] = useState<EvalBenchmarkSpec[]>([]);
  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<EvalRun | null>(null);
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);
  const [langfuseInfo, setLangfuseInfo] = useState<LangfuseInfo | null>(null);
  const [form, setForm] = useState<StartEvalRunRequest>(DEFAULT_FORM);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedRunIdRef = useRef<string | null>(null);

  selectedRunIdRef.current = selectedRunId;

  const selectedBenchmark = useMemo(
    () => benchmarks.find((benchmark) => benchmark.id === form.benchmark),
    [benchmarks, form.benchmark],
  );

  const loadRuns = useCallback(async () => {
    const data = await fetchEvalRuns(100);
    const merged = new Map<string, EvalRun>();
    for (const run of [...data.runs, ...data.active]) {
      merged.set(run.run_id, run);
    }
    const all = Array.from(merged.values()).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    setRuns(all);
    return all;
  }, []);

  const loadRun = useCallback(async (runId: string) => {
    const run = await fetchEvalRun(runId);
    setSelectedRun(run);
    return run;
  }, []);

  const refreshAll = useCallback(async () => {
    const all = await loadRuns();
    const targetRunId = selectedRunIdRef.current || all[0]?.run_id || null;
    setSelectedRunId(targetRunId);
    if (targetRunId) {
      await loadRun(targetRunId);
    } else {
      setSelectedRun(null);
    }
  }, [loadRun, loadRuns]);

  useEffect(() => {
    let cancelled = false;

    async function init() {
      try {
        const [benchmarkData, runData] = await Promise.all([fetchEvalBenchmarks(), fetchEvalRuns(100)]);
        if (cancelled) return;
        setBenchmarks(benchmarkData.benchmarks);
        const merged = new Map<string, EvalRun>();
        for (const run of [...runData.runs, ...runData.active]) {
          merged.set(run.run_id, run);
        }
        const all = Array.from(merged.values()).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
        setRuns(all);
        const first = all[0];
        if (first) {
          setSelectedRunId(first.run_id);
          setSelectedRun(await fetchEvalRun(first.run_id));
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    }

    fetchEvalLangfuseInfo()
      .then((info) => !cancelled && setLangfuseInfo(info))
      .catch(() => !cancelled && setLangfuseInfo(null));
    init();

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    wsManager.connect();
    return wsManager.subscribe((event) => {
      const type = event?.type;
      if (typeof type !== 'string' || !type.startsWith('eval/')) return;
      setEvents((prev) => [event, ...prev].slice(0, 200));
      const runId = event.run_id as string | undefined;
      if (!runId) return;
      if (!selectedRunIdRef.current && type === 'eval/run_started') {
        selectedRunIdRef.current = runId;
        setSelectedRunId(runId);
      }
      refreshAll().catch(() => undefined);
    });
  }, [refreshAll]);

  useEffect(() => {
    if (!selectedRunId) {
      setSelectedRun(null);
      return;
    }
    loadRun(selectedRunId).catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [selectedRunId, loadRun]);

  const handleBenchmarkChange = (benchmark: EvalBenchmark) => {
    const spec = benchmarks.find((item) => item.id === benchmark);
    setForm((prev) => ({
      ...prev,
      ...spec?.default_options,
      benchmark,
      only: null,
      level: benchmark === 'gaia' ? prev.level : null,
      category: benchmark === 'hle' ? prev.category : null,
      suite: benchmark === 'smoke' ? (prev.suite || 'chain') : prev.suite,
    }));
  };

  const handleStart = async () => {
    setLoading(true);
    setError(null);
    try {
      const payload: StartEvalRunRequest = {
        ...form,
        only: form.only && form.only.length ? form.only : null,
        level: form.benchmark === 'gaia' ? form.level : null,
        category: form.benchmark === 'hle' ? form.category : null,
        suite: form.benchmark === 'smoke' ? (form.suite || 'chain') : form.suite,
        execution_mode: form.benchmark === 'smoke' ? 'backend_session' : (form.execution_mode || 'backend_session'),
      };
      const run = await startEvalRun(payload);
      setSelectedRunId(run.run_id);
      setSelectedRun(run);
      await refreshAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const handleAbort = async () => {
    if (!selectedRunId) return;
    setLoading(true);
    setError(null);
    try {
      await abortEvalRun(selectedRunId);
      await refreshAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const handleJudge = async () => {
    if (!selectedRunId) return;
    setLoading(true);
    setError(null);
    try {
      await judgeEvalRun(selectedRunId, true);
      await refreshAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const updateForm = <K extends keyof StartEvalRunRequest>(key: K, value: StartEvalRunRequest[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const datasetName = selectedRun?.options?.sync_langfuse_dataset
    ? selectedRun?.tasks?.[0]?.langfuse_dataset?.dataset || `${selectedRun.benchmark}-eval`
    : null;
  const tasks: EvalTaskResult[] = selectedRun?.tasks || [];

  return (
    <PageLayout sidebarContent={null}>
      <div className="h-full overflow-y-auto p-6 space-y-6 bg-gray-50">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
              <Activity className="w-6 h-6 text-indigo-600" />
              Eval Runner
            </h1>
            <p className="text-sm text-gray-600 mt-1">
              启动 GAIA / HLE / Comprehensive，实时观察 backend session，并同步 Langfuse Dataset。
            </p>
          </div>
          <button
            onClick={() => refreshAll().catch((e) => setError(e instanceof Error ? e.message : String(e)))}
            className="flex items-center gap-2 px-3 py-2 rounded-md bg-white border border-gray-200 text-sm text-gray-700 hover:bg-gray-100"
          >
            <RefreshCw className="w-4 h-4" />
            刷新
          </button>
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm whitespace-pre-wrap">{error}</div>
        )}

        <div className="grid grid-cols-1 xl:grid-cols-[380px_1fr] gap-6 items-start">
          <div className="space-y-6">
          <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
            <h2 className="font-semibold text-gray-900">启动评测</h2>

            <label className="block text-sm">
              <span className="text-gray-600">Benchmark</span>
              <select
                value={form.benchmark}
                onChange={(e) => handleBenchmarkChange(e.target.value as EvalBenchmark)}
                className="mt-1 w-full rounded-md border-gray-300 border px-3 py-2 bg-white"
              >
                {benchmarks.map((benchmark) => (
                  <option key={benchmark.id} value={benchmark.id}>{benchmark.name}</option>
                ))}
              </select>
            </label>

            {selectedBenchmark && <p className="text-xs text-gray-500">{selectedBenchmark.description}</p>}

            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm">
                <span className="text-gray-600">Sample</span>
                <input
                  type="number"
                  min={1}
                  value={form.sample ?? ''}
                  onChange={(e) => updateForm('sample', e.target.value ? Number(e.target.value) : null)}
                  className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2"
                />
              </label>
              <label className="block text-sm">
                <span className="text-gray-600">Seed</span>
                <input
                  type="number"
                  value={form.seed ?? 42}
                  onChange={(e) => updateForm('seed', Number(e.target.value))}
                  className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2"
                />
              </label>
            </div>

            {form.benchmark === 'gaia' && (
              <label className="block text-sm">
                <span className="text-gray-600">Level</span>
                <select
                  value={form.level ?? ''}
                  onChange={(e) => updateForm('level', e.target.value ? Number(e.target.value) : null)}
                  className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 bg-white"
                >
                  <option value="">全部</option>
                  <option value="1">1</option>
                  <option value="2">2</option>
                  <option value="3">3</option>
                </select>
              </label>
            )}

            {form.benchmark === 'hle' && (
              <label className="block text-sm">
                <span className="text-gray-600">Category</span>
                <input
                  value={form.category ?? ''}
                  onChange={(e) => updateForm('category', e.target.value || null)}
                  className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2"
                  placeholder="Physics"
                />
              </label>
            )}

            <label className="block text-sm">
              <span className="text-gray-600">Thinking</span>
              <select
                value={form.thinking === null || form.thinking === undefined ? 'default' : String(form.thinking)}
                onChange={(e) => updateForm('thinking', e.target.value === 'default' ? null : e.target.value === 'true')}
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 bg-white"
              >
                <option value="default">跟随全局配置</option>
                <option value="true">开</option>
                <option value="false">关（更快，考验鲁棒性）</option>
              </select>
            </label>

            <label className="block text-sm">
              <span className="text-gray-600">压缩策略（消融实验臂）</span>
              <select
                value={form.compression_arm ?? 'full'}
                onChange={(e) => updateForm('compression_arm', e.target.value === 'full' ? null : e.target.value as 'none' | 'tool_only' | 'session_only')}
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 bg-white"
              >
                <option value="full">full（现状：工具折叠+会话折叠）</option>
                <option value="none">none（不压缩 + 1M 窗口）</option>
                <option value="tool_only">tool_only（仅工具结果折叠）</option>
                <option value="session_only">session_only（仅会话折叠）</option>
              </select>
            </label>

            {form.benchmark === 'smoke' && (
              <label className="block text-sm">
                <span className="text-gray-600">Suite</span>
                <select
                  value={form.suite || 'chain'}
                  onChange={(e) => updateForm('suite', e.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 bg-white"
                >
                  <option value="chain">chain（全链路评测）</option>
                  <option value="smoke">smoke（单元）</option>
                </select>
              </label>
            )}

            <label className="block text-sm">
              <span className="text-gray-600">Only task ids（逗号分隔，可选）</span>
              <input
                value={(form.only || []).join(',')}
                onChange={(e) => updateForm('only', e.target.value ? e.target.value.split(',').map((v) => v.trim()).filter(Boolean) : null)}
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2"
                placeholder="task-1,task-2"
              />
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm">
                <span className="text-gray-600">Timeout(s)</span>
                <input
                  type="number"
                  min={0}
                  value={form.timeout_s ?? 0}
                  onChange={(e) => updateForm('timeout_s', Number(e.target.value))}
                  className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2"
                />
              </label>
              <label className="block text-sm">
                <span className="text-gray-600">Execution</span>
                <select
                  value={form.benchmark === 'smoke' ? 'backend_session' : (form.execution_mode || 'backend_session')}
                  disabled={form.benchmark === 'smoke'}
                  onChange={(e) => updateForm('execution_mode', e.target.value as 'backend_session' | 'in_process')}
                  className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 bg-white disabled:bg-gray-100"
                >
                  <option value="backend_session">backend_session</option>
                  <option value="in_process">in_process</option>
                </select>
              </label>
            </div>

            <div className="space-y-2 text-sm text-gray-700">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={!!form.sync_langfuse_dataset} onChange={(e) => updateForm('sync_langfuse_dataset', e.target.checked)} />
                同步 Langfuse Dataset
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={!!form.judge_after_run} onChange={(e) => updateForm('judge_after_run', e.target.checked)} />
                结束后运行 Judge
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={!!form.keep_sessions} onChange={(e) => updateForm('keep_sessions', e.target.checked)} />
                保留评测 session
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={!!form.skip_langfuse} onChange={(e) => updateForm('skip_langfuse', e.target.checked)} />
                跳过 Langfuse
              </label>
            </div>

            <button
              onClick={handleStart}
              disabled={loading || !benchmarks.length}
              className="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-md bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 disabled:bg-gray-300"
            >
              <Play className="w-4 h-4" />
              {loading ? '处理中...' : '启动'}
            </button>
          </div>

            <div className="bg-white rounded-lg border border-gray-200 p-4">
              <div className="flex items-center justify-between gap-3 mb-3">
                <h2 className="font-semibold text-gray-900">Runs</h2>
                <span className="text-xs text-gray-500">{runs.length} 个</span>
              </div>
              <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
                {runs.map((run) => (
                  <button
                    key={run.run_id}
                    onClick={() => setSelectedRunId(run.run_id)}
                    className={`w-full text-left rounded-md border p-3 transition-colors ${selectedRunId === run.run_id ? 'border-indigo-400 bg-indigo-50' : 'border-gray-200 hover:bg-gray-50'}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-mono text-xs text-gray-900 truncate">{run.run_id}</div>
                      <StatusBadge status={run.status} />
                    </div>
                    <div className="mt-1 text-xs text-gray-600 flex flex-wrap gap-x-3 gap-y-1">
                      <span>{run.benchmark}</span>
                      <span>model: {run.model || '-'}</span>
                      <span>pass: {formatPercent(run.summary?.pass_at_1)}</span>
                      <span>total: {run.summary?.total ?? '-'}</span>
                      <span>{formatTime(run.created_at)}</span>
                    </div>
                  </button>
                ))}
                {!runs.length && <div className="text-sm text-gray-500">暂无 run</div>}
              </div>
            </div>
          </div>

          <div className="space-y-6">
            {!selectedRun && (
              <div className="bg-white rounded-lg border border-gray-200 p-10 text-center text-sm text-gray-400">
                选择左侧 Run 查看详情
              </div>
            )}
            {selectedRun && (
              <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-semibold text-gray-900 font-mono text-sm">{selectedRun.run_id}</h2>
                    <div className="text-xs text-gray-600 mt-1 flex flex-wrap gap-x-3 gap-y-1">
                      <span>benchmark: {selectedRun.benchmark}</span>
                      <span>model: {selectedRun.model || '-'}</span>
                      <span>eval session: {selectedRun.eval_session_id}</span>
                      <span>started: {formatTime(selectedRun.started_at)}</span>
                      <span>finished: {formatTime(selectedRun.finished_at)}</span>
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button onClick={handleAbort} disabled={loading || !['pending', 'running'].includes(selectedRun.status)} className="flex items-center gap-1 px-3 py-1.5 rounded-md border border-gray-300 text-sm disabled:text-gray-400">
                      <Square className="w-3.5 h-3.5" />
                      Abort
                    </button>
                    <button onClick={handleJudge} disabled={loading || !selectedRun.tasks?.length} className="flex items-center gap-1 px-3 py-1.5 rounded-md border border-gray-300 text-sm disabled:text-gray-400">
                      <Gavel className="w-3.5 h-3.5" />
                      Judge
                    </button>
                    <a href={evalReportMarkdownUrl(selectedRun.run_id)} target="_blank" rel="noreferrer" className="flex items-center gap-1 px-3 py-1.5 rounded-md border border-gray-300 text-sm">
                      <ExternalLink className="w-3.5 h-3.5" />
                      Markdown
                    </a>
                  </div>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-6 gap-3 text-center">
                  {[
                    ['状态', selectedRun.status],
                    ['总数', selectedRun.summary?.total ?? 0],
                    ['完成', selectedRun.summary?.completed ?? selectedRun.summary?.total ?? 0],
                    ['通过', selectedRun.summary?.correct ?? selectedRun.summary?.passed ?? 0],
                    ['Pass@1', formatPercent(selectedRun.summary?.pass_at_1)],
                    ['平均耗时', `${selectedRun.summary?.avg_duration_s ?? 0}s`],
                  ].map(([label, value]) => (
                    <div key={String(label)} className="rounded-md border border-gray-200 p-3">
                      <div className="text-lg font-semibold text-gray-900 break-all">{String(value)}</div>
                      <div className="text-xs text-gray-500">{String(label)}</div>
                    </div>
                  ))}
                </div>

                <div className="flex flex-wrap gap-2 text-xs">
                  {langfuseSessionUrl(langfuseInfo, selectedRun.eval_session_id) && (
                    <a className="text-indigo-600 hover:underline flex items-center gap-1" href={langfuseSessionUrl(langfuseInfo, selectedRun.eval_session_id)!} target="_blank" rel="noreferrer">
                      <ExternalLink className="w-3 h-3" /> Langfuse Session
                    </a>
                  )}
                  {datasetName && langfuseDatasetUrl(langfuseInfo, datasetName) && (
                    <a className="text-indigo-600 hover:underline flex items-center gap-1" href={langfuseDatasetUrl(langfuseInfo, datasetName)!} target="_blank" rel="noreferrer">
                      <ExternalLink className="w-3 h-3" /> Langfuse Dataset
                    </a>
                  )}
                </div>

                {selectedRun.error && <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-2">{selectedRun.error}</div>}

                <div className="overflow-x-auto">
                  <table className="min-w-full text-sm">
                    <thead className="text-xs text-gray-500 border-b border-gray-200">
                      <tr>
                        <th className="text-left py-2 pr-3">Task</th>
                        <th className="text-left py-2 pr-3">Status</th>
                        <th className="text-left py-2 pr-3">Duration</th>
                        <th className="text-left py-2 pr-3">Expected / Predicted</th>
                        <th className="text-left py-2 pr-3">Session</th>
                        <th className="text-left py-2">Trace</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tasks.map((task) => (
                        <tr key={task.task_id} className="border-b border-gray-100 align-top">
                          <td className="py-2 pr-3">
                            <div className="font-mono text-xs text-gray-900">{task.task_id}</div>
                            {task.name && <div className="text-xs text-gray-500 max-w-xs truncate">{task.name}</div>}
                          </td>
                          <td className="py-2 pr-3"><StatusBadge status={task.status} /></td>
                          <td className="py-2 pr-3 text-xs text-gray-600">{task.duration_s}s</td>
                          <td className="py-2 pr-3 text-xs text-gray-700 max-w-md">
                            <div><span className="text-gray-500">expected:</span> {task.expected || '-'}</div>
                            <div><span className="text-gray-500">predicted:</span> {task.predicted || '-'}</div>
                            {task.error && <div className="text-red-600 mt-1">{task.error}</div>}
                          </td>
                          <td className="py-2 pr-3 text-xs">
                            {task.session_id ? <Link className="text-indigo-600 hover:underline" to={`/?session=${task.session_id}`}>open</Link> : '-'}
                          </td>
                          <td className="py-2 text-xs">
                            {task.trace_id && langfuseTraceUrl(langfuseInfo, task.trace_id) ? (
                              <a className="text-indigo-600 hover:underline flex items-center gap-1" href={langfuseTraceUrl(langfuseInfo, task.trace_id)!} target="_blank" rel="noreferrer">
                                <ExternalLink className="w-3 h-3" /> trace
                              </a>
                            ) : '-'}
                          </td>
                        </tr>
                      ))}
                      {!tasks.length && (
                        <tr><td colSpan={6} className="py-4 text-center text-gray-500">暂无任务</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <div className="bg-white rounded-lg border border-gray-200 p-4">
              <h2 className="font-semibold text-gray-900 mb-3">实时事件</h2>
              <div className="space-y-2 max-h-72 overflow-y-auto font-mono text-xs">
                {events.map((event, index) => (
                  <div key={`${event.type}-${index}`} className="border border-gray-100 rounded p-2 bg-gray-50">
                    <div className="text-gray-900">{String(event.type)} {event.run_id ? `run=${event.run_id}` : ''} {event.task_id ? `task=${event.task_id}` : ''}</div>
                    <pre className="mt-1 whitespace-pre-wrap break-all text-gray-600">{JSON.stringify(event, null, 2)}</pre>
                  </div>
                ))}
                {!events.length && <div className="text-sm text-gray-500 font-sans">等待 eval/* WebSocket 事件</div>}
              </div>
            </div>
          </div>
        </div>
      </div>
    </PageLayout>
  );
}

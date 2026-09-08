import { useState, useEffect, useRef, useCallback } from 'react';
import { Clock, RefreshCw, Power, Filter, Activity, Cpu, HardDrive, Zap, ExternalLink } from 'lucide-react';
import { fetchTraceEvents, fetchTraceFiles, toggleTrace, fetchTraceStatus, TraceEvent, TraceFile, TraceStatus } from '../api/client';
import { TrajectoryTimeline } from '../components/chat/TrajectoryTimeline';
import { PageLayout } from '../components/PageLayout';

interface TimelineEvent {
  id: string;
  timestamp: string;
  type: 'turn' | 'model' | 'tool' | 'sub_agent' | 'stream' | 'audit' | 'cost' | 'compact' | 'other';
  duration?: number;
  content?: string;
  metadata?: Record<string, unknown>;
  kind?: string;
}

interface TraceStats {
  totalEvents: number;
  modelCalls: number;
  toolCalls: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  totalLatencyMs: number;
  errors: number;
}

export default function TracePage() {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [tracePath, setTracePath] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [eventCount, setEventCount] = useState(500);
  const [filterType, setFilterType] = useState<string>('all');
  const [autoRefresh, setAutoRefresh] = useState(false);
  
  const [traceFiles, setTraceFiles] = useState<TraceFile[]>([]);
  const [selectedSession, setSelectedSession] = useState<string>('');
  const [filesLoading, setFilesLoading] = useState(false);
  
  const lastEventCountRef = useRef(0);
  const isInitialLoadRef = useRef(true);
  const [traceStatus, setTraceStatus] = useState<TraceStatus | null>(null);

  const loadTraceFiles = useCallback(async () => {
    setFilesLoading(true);
    try {
      const data = await fetchTraceFiles();
      const files = data.files || [];
      setTraceFiles(files);
      if (!selectedSession && files.length > 0) {
        setSelectedSession(files[0].session_id);
      }
    } catch (err) {
      console.error('Failed to load trace files:', err);
      setTraceFiles([]);
    } finally {
      setFilesLoading(false);
    }
  }, [selectedSession]);

  const loadTrace = useCallback(async (isRefresh = false) => {
    if (!isRefresh) {
      setLoading(true);
    }
    setError(null);
    try {
      const data = await fetchTraceEvents(eventCount, selectedSession || undefined);
      const newEvents = data.events;
      
      if (newEvents.length !== lastEventCountRef.current || isInitialLoadRef.current) {
        setEvents(newEvents);
        lastEventCountRef.current = newEvents.length;
        isInitialLoadRef.current = false;
      }
      
      setEnabled(data.enabled);
      setTracePath(data.path || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load trace');
    } finally {
      setLoading(false);
    }
  }, [eventCount, selectedSession]);

  useEffect(() => {
    loadTraceFiles();
    loadTrace();
    fetchTraceStatus().then(setTraceStatus).catch(() => {});
  }, []);

  useEffect(() => {
    if (selectedSession) {
      isInitialLoadRef.current = true;
      lastEventCountRef.current = 0;
      loadTrace();
    }
  }, [selectedSession]);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(() => loadTrace(true), 2000);
    return () => clearInterval(interval);
  }, [autoRefresh, loadTrace]);

  const handleToggle = async () => {
    try {
      await toggleTrace(!enabled);
      setEnabled(!enabled);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to toggle trace');
    }
  };

  const computeStats = (events: TraceEvent[]): TraceStats => {
    const stats: TraceStats = {
      totalEvents: events.length,
      modelCalls: 0,
      toolCalls: 0,
      totalInputTokens: 0,
      totalOutputTokens: 0,
      totalLatencyMs: 0,
      errors: 0,
    };

    for (const e of events) {
      if (e.kind === 'model_call.end' || e.kind === 'model.end') {
        stats.modelCalls++;
        stats.totalInputTokens += e.input_tokens || 0;
        stats.totalOutputTokens += e.output_tokens || 0;
        stats.totalLatencyMs += (e.duration_s || 0) * 1000;
      } else if (e.kind === 'tool_call.end' || e.kind === 'tool.end') {
        stats.toolCalls++;
        stats.totalLatencyMs += (e.duration_s || 0) * 1000;
        if (e.success === false) stats.errors++;
      } else if (e.kind?.startsWith('stream.') && e.kind.includes('error')) {
        stats.errors++;
      }
    }

    return stats;
  };

  const stats = computeStats(events);

  const timelineEvents: TimelineEvent[] = events
    .filter(e => {
      if (filterType === 'all') return true;
      if (filterType === 'turn') return e.kind?.startsWith('turn');
      if (filterType === 'model') return e.kind?.startsWith('model') || e.kind?.startsWith('stream.thinking');
      if (filterType === 'tool') return e.kind?.startsWith('tool') || e.kind?.startsWith('stream.tool');
      if (filterType === 'sub_agent') return e.kind?.startsWith('stream.sub_agent') || e.metadata?.sub_agent === true;
      if (filterType === 'stream') return e.kind?.startsWith('stream.');
      if (filterType === 'audit') return e.kind?.startsWith('audit');
      if (filterType === 'cost') return e.kind?.startsWith('cost');
      if (filterType === 'compact') return e.kind === 'compact';
      if (filterType === 'error') return e.kind?.includes('error') || (e.kind === 'tool.end' && !e.success);
      return e.kind?.startsWith(filterType);
    })
    .map((e, idx) => {
      const { ts, kind, ...rest } = e;

      let type: TimelineEvent['type'] = 'other';
      if (kind?.startsWith('turn')) type = 'turn';
      else if (kind?.startsWith('model') || kind === 'stream.thinking') type = 'model';
      else if (kind?.startsWith('tool') || kind?.startsWith('stream.tool')) type = 'tool';
      else if (kind?.startsWith('stream.sub_agent')) type = 'sub_agent';
      else if (rest.sub_agent === true) type = 'sub_agent';
      else if (kind?.startsWith('stream.')) type = 'stream';
      else if (kind?.startsWith('audit')) type = 'audit';
      else if (kind?.startsWith('cost')) type = 'cost';
      else if (kind === 'compact') type = 'compact';

      const duration = rest.duration_ms || rest.llm_ms || rest.tool_ms || (rest.duration_s ? rest.duration_s * 1000 : undefined);

      let content = '';
      if (kind === 'turn.start') {
        const userPreview = rest.user_preview || '';
        content = userPreview ? `👤 ${userPreview.slice(0, 200)}` : 'Turn started';
      }
      else if (kind === 'turn.end') {
        const assistantPreview = rest.assistant_preview || '';
        const thinkingPreview = rest.thinking_preview || '';
        const duration = rest.duration_s ? `${(rest.duration_s * 1000).toFixed(0)}ms` : '0ms';
        if (assistantPreview) {
          content = `🤖 ${assistantPreview.slice(0, 200)}`;
          if (thinkingPreview) {
            content += `\n💭 ${thinkingPreview.slice(0, 100)}`;
          }
          content += ` (${duration})`;
        } else {
          content = `Turn completed (${duration})`;
        }
      }
      else if (kind === 'model.start' || kind === 'model_call.start') content = `Calling ${rest.model || 'model'}...`;
      else if (kind === 'model.end' || kind === 'model_call.end') content = `${rest.input_tokens || 0} in / ${rest.output_tokens || 0} out (${(rest.duration_s || 0) * 1000}ms)`;
      else if (kind === 'tool.start' || kind === 'tool_call.start') content = `🔧 ${rest.tool || 'tool'}(${(rest.input || '').slice(0, 50)})`;
      else if (kind === 'tool.end' || kind === 'tool_call.end') content = `${rest.success ? '✓' : '✗'} ${(rest.preview || '').slice(0, 100)}`;
      else if (kind === 'stream.text') content = rest.preview || '';
      else if (kind === 'stream.thinking') content = rest.preview || '';
      else if (kind === 'stream.tool_call') content = `${rest.name || 'tool'}(call_id: ${rest.call_id || ''})`;
      else if (kind === 'stream.tool_result') content = `${rest.name || 'tool'} result`;
      else if (kind === 'stream.sub_agent_start') content = `🤖 Sub-agent ${rest.agent_type} started: ${rest.description || ''}`;
      else if (kind === 'stream.sub_agent_end') content = `🤖 Sub-agent ${rest.agent_type} ${rest.status}`;
      else if (kind?.startsWith('audit')) content = `${rest.tool || ''} → ${rest.decision || rest.operation || ''}`;
      else if (kind?.startsWith('cost')) content = JSON.stringify(rest).slice(0, 100);
      else if (kind === 'error') content = `❌ ${rest.error_type || 'Error'}: ${rest.message || ''}`;
      else content = JSON.stringify(rest, null, 2).slice(0, 200);

      return {
        id: `${ts}-${idx}`,
        timestamp: ts,
        type,
        duration: typeof duration === 'number' ? duration : undefined,
        content,
        metadata: rest,
        kind,
      };
    });

  if (loading && events.length === 0) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-gray-500">Loading trace...</div>
      </div>
    );
  }

  if (error && events.length === 0) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-red-500">{error}</div>
      </div>
    );
  }

  const sidebarContent = (
    <div className="flex flex-col h-full">
      <div className="p-3 border-b border-gray-200">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-gray-900">Trace Sessions</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {traceFiles.length} session{traceFiles.length !== 1 ? 's' : ''}
            </p>
          </div>
          <button
            onClick={loadTraceFiles}
            disabled={filesLoading}
            className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors"
            title="Refresh"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        {traceFiles.length === 0 ? (
          <div className="p-4 text-center text-xs text-gray-500">
            No trace sessions found
          </div>
        ) : (
          <div className="divide-y divide-gray-100">
            {traceFiles.map(file => (
              <div
                key={file.filename}
                className={`px-3 py-2 cursor-pointer hover:bg-gray-50 ${
                  selectedSession === file.session_id ? 'bg-blue-50 border-l-2 border-blue-500' : ''
                }`}
                onClick={() => setSelectedSession(file.session_id)}
              >
                <div className="flex items-center gap-1.5">
                  <Activity className="w-3.5 h-3.5 text-blue-500" />
                  <h3 className="text-xs font-medium text-gray-900 truncate">
                    {file.session_id.slice(0, 8)}
                  </h3>
                </div>
                <p className="text-[10px] text-gray-500 truncate mt-0.5">
                  {file.line_count} events • {file.size} bytes
                </p>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );

  const otelEnabled = traceStatus?.otel_enabled ?? enabled;
  const phoenixEndpoint = traceStatus?.phoenix_endpoint || tracePath;
  const phoenixReachable = traceStatus?.phoenix_reachable ?? false;

  if (otelEnabled && phoenixReachable) {
    return (
      <div className="h-full flex flex-col bg-white">
        <div className="flex items-center gap-3 px-4 py-2 border-b bg-green-50 border-green-200">
          <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
          <span className="text-sm font-medium text-green-800">OTel Active</span>
          <span className="text-sm text-gray-500">
            Phoenix: <a href={phoenixEndpoint} target="_blank" rel="noopener noreferrer" className="text-blue-500 hover:underline">{phoenixEndpoint}</a>
          </span>
          <button
            onClick={() => window.open(phoenixEndpoint, '_blank')}
            className="ml-auto flex items-center gap-1 text-sm px-3 py-1 bg-white border border-green-300 rounded hover:bg-green-50 transition-colors"
          >
            <ExternalLink className="w-3 h-3" />
            Open in new tab
          </button>
        </div>
        <iframe
          src={phoenixEndpoint}
          className="flex-1 w-full border-0"
          title="Phoenix Trace Viewer"
        />
      </div>
    );
  }

  return (
    <PageLayout sidebarContent={sidebarContent}>
      <div className="h-full flex flex-col bg-white">
        <div className="p-4 border-b border-gray-200">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-xl font-bold text-gray-900 flex items-center gap-2">
                <Activity className="w-5 h-5 text-blue-500" />
                Trace Observer
              </h1>
              <p className="text-xs text-gray-500 mt-1">
                {timelineEvents.length} events •
                Status: <span className={otelEnabled ? 'text-green-600 font-medium' : 'text-gray-400'}>
                  {otelEnabled ? 'ON' : 'OFF'}
                </span>
                {otelEnabled && !phoenixReachable && phoenixEndpoint && (
                  <span className="ml-2 text-yellow-600">
                    Phoenix unreachable — showing fallback view
                  </span>
                )}
                {phoenixEndpoint && (
                  <span className="ml-2">
                    <a href={phoenixEndpoint} target="_blank" rel="noopener noreferrer" className="text-blue-500 hover:underline">
                      Open Phoenix ↗
                    </a>
                  </span>
                )}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-1.5 text-xs text-gray-600">
                <input
                  type="checkbox"
                  checked={autoRefresh}
                  onChange={(e) => setAutoRefresh(e.target.checked)}
                  className="rounded w-3.5 h-3.5"
                />
                Auto
              </label>
              <select
                value={eventCount}
                onChange={(e) => setEventCount(Number(e.target.value))}
                className="px-2 py-1.5 border border-gray-300 rounded text-xs"
              >
                <option value={100}>100</option>
                <option value={500}>500</option>
                <option value={1000}>1000</option>
                <option value={5000}>5000</option>
              </select>
              <button
                onClick={() => loadTrace(true)}
                className="flex items-center px-3 py-1.5 bg-blue-500 text-white rounded text-xs hover:bg-blue-600 transition-colors"
              >
                <RefreshCw className="w-3 h-3 mr-1" />
                Refresh
              </button>
              <button
                onClick={handleToggle}
                className={`flex items-center px-3 py-1.5 rounded text-xs transition-colors ${
                  enabled
                    ? 'bg-red-500 text-white hover:bg-red-600'
                    : 'bg-green-500 text-white hover:bg-green-600'
                }`}
              >
                <Power className="w-3 h-3 mr-1" />
                {enabled ? 'Disable' : 'Enable'}
              </button>
            </div>
          </div>
        </div>

        <div className="p-4 border-b border-gray-200">
          <div className="grid grid-cols-4 gap-3 mb-3">
            <div className="p-2 bg-blue-50 rounded border border-blue-100">
              <div className="flex items-center gap-1.5 text-blue-700">
                <Cpu className="w-3.5 h-3.5" />
                <span className="text-xs font-medium">Model</span>
              </div>
              <div className="text-lg font-bold text-blue-900">{stats.modelCalls}</div>
              <div className="text-xs text-blue-600">{stats.totalInputTokens + stats.totalOutputTokens} tokens</div>
            </div>
            <div className="p-2 bg-orange-50 rounded border border-orange-100">
              <div className="flex items-center gap-1.5 text-orange-700">
                <Zap className="w-3.5 h-3.5" />
                <span className="text-xs font-medium">Tools</span>
              </div>
              <div className="text-lg font-bold text-orange-900">{stats.toolCalls}</div>
              <div className="text-xs text-orange-600">{stats.errors} errors</div>
            </div>
            <div className="p-2 bg-green-50 rounded border border-green-100">
              <div className="flex items-center gap-1.5 text-green-700">
                <HardDrive className="w-3.5 h-3.5" />
                <span className="text-xs font-medium">Tokens</span>
              </div>
              <div className="text-lg font-bold text-green-900">
                {((stats.totalInputTokens + stats.totalOutputTokens) / 1000).toFixed(1)}k
              </div>
              <div className="text-xs text-green-600">
                {stats.totalInputTokens} in / {stats.totalOutputTokens} out
              </div>
            </div>
            <div className="p-2 bg-purple-50 rounded border border-purple-100">
              <div className="flex items-center gap-1.5 text-purple-700">
                <Clock className="w-3.5 h-3.5" />
                <span className="text-xs font-medium">Latency</span>
              </div>
              <div className="text-lg font-bold text-purple-900">
                {(stats.totalLatencyMs / 1000).toFixed(1)}s
              </div>
              <div className="text-xs text-purple-600">total</div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Filter className="w-3.5 h-3.5 text-gray-500" />
            <select
              value={filterType}
              onChange={(e) => setFilterType(e.target.value)}
              className="px-2 py-1 border border-gray-300 rounded text-xs"
            >
              <option value="all">All Events</option>
              <option value="turn">Turns</option>
              <option value="model">Model Calls</option>
              <option value="tool">Tool Calls</option>
              <option value="sub_agent">Sub-Agents</option>
              <option value="stream">Stream Events</option>
              <option value="audit">Audit Logs</option>
              <option value="cost">Cost Events</option>
              <option value="compact">Compaction</option>
              <option value="error">Errors Only</option>
            </select>
            <span className="text-xs text-gray-500">
              {timelineEvents.length} / {events.length}
            </span>
          </div>

          <div className="text-xs text-gray-400 font-mono mt-2 truncate">
            {tracePath}
          </div>
        </div>

        <div className="flex-1 overflow-hidden">
          {timelineEvents.length === 0 ? (
            <div className="h-full flex items-center justify-center">
              <div className="text-center text-gray-500">
                <Clock className="w-10 h-10 mx-auto mb-3 opacity-50" />
                <p className="text-sm">No events to display</p>
                <p className="text-xs mt-1">
                  {enabled ? 'Events will appear here as they occur' : 'Enable trace to start recording events'}
                </p>
              </div>
            </div>
          ) : (
            <TrajectoryTimeline events={timelineEvents} />
          )}
        </div>
      </div>
    </PageLayout>
  );
}

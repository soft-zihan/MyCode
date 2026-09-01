import { useState, useEffect } from 'react';
import { Clock, RefreshCw, Power, Filter, Activity, Cpu, HardDrive, Zap } from 'lucide-react';
import { fetchTraceEvents, toggleTrace, TraceEvent } from '../api/client';
import { TrajectoryTimeline } from '../components/chat/TrajectoryTimeline';

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
  const [eventCount, setEventCount] = useState(100);
  const [filterType, setFilterType] = useState<string>('all');
  const [autoRefresh, setAutoRefresh] = useState(false);

  const loadTrace = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchTraceEvents(eventCount);
      setEvents(data.events);
      setEnabled(data.enabled);
      setTracePath(data.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load trace');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTrace();
  }, [eventCount]);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(loadTrace, 3000);
    return () => clearInterval(interval);
  }, [autoRefresh, eventCount]);

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
      if (e.kind === 'model.end') {
        stats.modelCalls++;
        stats.totalInputTokens += e.input_tokens || 0;
        stats.totalOutputTokens += e.output_tokens || 0;
        stats.totalLatencyMs += e.llm_ms || 0;
      } else if (e.kind === 'tool.end') {
        stats.toolCalls++;
        stats.totalLatencyMs += e.tool_ms || 0;
        if (!e.success) stats.errors++;
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
      if (filterType === 'sub_agent') return e.kind?.startsWith('stream.sub_agent');
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
      else if (kind?.startsWith('stream.')) type = 'stream';
      else if (kind?.startsWith('audit')) type = 'audit';
      else if (kind?.startsWith('cost')) type = 'cost';
      else if (kind === 'compact') type = 'compact';

      const duration = rest.duration_ms || rest.llm_ms || rest.tool_ms;

      let content = '';
      if (kind === 'turn.start') content = `Turn started`;
      else if (kind === 'turn.end') content = `Turn completed (${rest.total_ms || 0}ms)`;
      else if (kind === 'model.start') content = `Calling ${rest.model || 'model'}...`;
      else if (kind === 'model.end') content = `${rest.input_tokens || 0} in / ${rest.output_tokens || 0} out (${rest.llm_ms || 0}ms)`;
      else if (kind === 'tool.start') content = `${rest.tool || 'tool'}(${(rest.input || '').slice(0, 50)})`;
      else if (kind === 'tool.end') content = `${rest.success ? '✓' : '✗'} ${(rest.preview || '').slice(0, 100)}`;
      else if (kind === 'stream.text') content = rest.preview || '';
      else if (kind === 'stream.thinking') content = rest.preview || '';
      else if (kind === 'stream.tool_call') content = `${rest.name || 'tool'}(call_id: ${rest.call_id || ''})`;
      else if (kind === 'stream.tool_result') content = `${rest.name || 'tool'} result`;
      else if (kind === 'stream.sub_agent_start') content = `Sub-agent ${rest.agent_type} started`;
      else if (kind === 'stream.sub_agent_end') content = `Sub-agent ${rest.agent_type} ${rest.status}`;
      else if (kind?.startsWith('audit')) content = `${rest.tool || ''} → ${rest.decision || rest.operation || ''}`;
      else if (kind?.startsWith('cost')) content = JSON.stringify(rest).slice(0, 100);
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

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-gray-500">Loading trace...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-red-500">{error}</div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="p-6 border-b border-gray-200">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
              <Activity className="w-6 h-6 text-blue-500" />
              Trace Observer
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              {timelineEvents.length} events •
              Status: <span className={enabled ? 'text-green-600 font-medium' : 'text-gray-400'}>
                {enabled ? 'ON' : 'OFF'}
              </span>
            </p>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="rounded"
              />
              Auto-refresh
            </label>
            <select
              value={eventCount}
              onChange={(e) => setEventCount(Number(e.target.value))}
              className="px-3 py-2 border border-gray-300 rounded text-sm"
            >
              <option value={50}>50 events</option>
              <option value={100}>100 events</option>
              <option value={200}>200 events</option>
              <option value={500}>500 events</option>
            </select>
            <button
              onClick={loadTrace}
              className="flex items-center px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors"
            >
              <RefreshCw className="w-4 h-4 mr-2" />
              Refresh
            </button>
            <button
              onClick={handleToggle}
              className={`flex items-center px-4 py-2 rounded transition-colors ${
                enabled
                  ? 'bg-red-500 text-white hover:bg-red-600'
                  : 'bg-green-500 text-white hover:bg-green-600'
              }`}
            >
              <Power className="w-4 h-4 mr-2" />
              {enabled ? 'Disable' : 'Enable'}
            </button>
          </div>
        </div>

        {/* Stats Panel */}
        <div className="grid grid-cols-4 gap-4 mb-4">
          <div className="p-3 bg-blue-50 rounded-lg border border-blue-100">
            <div className="flex items-center gap-2 text-blue-700">
              <Cpu className="w-4 h-4" />
              <span className="text-xs font-medium">Model Calls</span>
            </div>
            <div className="text-2xl font-bold text-blue-900 mt-1">{stats.modelCalls}</div>
            <div className="text-xs text-blue-600">{stats.totalInputTokens + stats.totalOutputTokens} tokens</div>
          </div>
          <div className="p-3 bg-orange-50 rounded-lg border border-orange-100">
            <div className="flex items-center gap-2 text-orange-700">
              <Zap className="w-4 h-4" />
              <span className="text-xs font-medium">Tool Calls</span>
            </div>
            <div className="text-2xl font-bold text-orange-900 mt-1">{stats.toolCalls}</div>
            <div className="text-xs text-orange-600">{stats.errors} errors</div>
          </div>
          <div className="p-3 bg-green-50 rounded-lg border border-green-100">
            <div className="flex items-center gap-2 text-green-700">
              <HardDrive className="w-4 h-4" />
              <span className="text-xs font-medium">Tokens</span>
            </div>
            <div className="text-2xl font-bold text-green-900 mt-1">
              {((stats.totalInputTokens + stats.totalOutputTokens) / 1000).toFixed(1)}k
            </div>
            <div className="text-xs text-green-600">
              {stats.totalInputTokens} in / {stats.totalOutputTokens} out
            </div>
          </div>
          <div className="p-3 bg-purple-50 rounded-lg border border-purple-100">
            <div className="flex items-center gap-2 text-purple-700">
              <Clock className="w-4 h-4" />
              <span className="text-xs font-medium">Latency</span>
            </div>
            <div className="text-2xl font-bold text-purple-900 mt-1">
              {(stats.totalLatencyMs / 1000).toFixed(1)}s
            </div>
            <div className="text-xs text-purple-600">total</div>
          </div>
        </div>

        {/* Filter controls */}
        <div className="flex items-center gap-3 mb-3">
          <Filter className="w-4 h-4 text-gray-500" />
          <select
            value={filterType}
            onChange={(e) => setFilterType(e.target.value)}
            className="px-3 py-1.5 border border-gray-300 rounded text-sm"
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
            Showing {timelineEvents.length} of {events.length} events
          </span>
        </div>

        <div className="text-xs text-gray-500 font-mono">
          {tracePath}
        </div>
      </div>

      {/* Timeline */}
      <div className="flex-1 overflow-hidden">
        {timelineEvents.length === 0 ? (
          <div className="h-full flex items-center justify-center">
            <div className="text-center text-gray-500">
              <Clock className="w-12 h-12 mx-auto mb-4 opacity-50" />
              <p>No events to display</p>
              <p className="text-sm mt-2">
                {enabled ? 'Events will appear here as they occur' : 'Enable trace to start recording events'}
              </p>
            </div>
          </div>
        ) : (
          <TrajectoryTimeline events={timelineEvents} />
        )}
      </div>
    </div>
  );
}

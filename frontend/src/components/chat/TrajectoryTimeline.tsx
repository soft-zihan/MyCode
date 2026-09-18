import React, { useState, useMemo } from 'react';
import { Clock, Zap, Bot, Wrench, MessageSquare, ChevronDown, ChevronRight, AlertCircle, Shield, DollarSign, Layers, Radio, BarChart3 } from 'lucide-react';
import { OverviewBar } from '../trace/OverviewBar';
import './TrajectoryTimeline.css';

interface TrajectoryEvent {
  id: string;
  timestamp: string;
  type: 'turn' | 'model' | 'tool' | 'sub_agent' | 'stream' | 'audit' | 'cost' | 'compact' | 'other';
  duration?: number;
  content?: string;
  metadata?: Record<string, unknown>;
  kind?: string;
}

interface TrajectoryTimelineProps {
  events: TrajectoryEvent[];
  sessionId?: string;
}

interface DurationBar {
  id: string;
  label: string;
  type: TrajectoryEvent['type'];
  startMs: number;
  durationMs: number;
  offsetPercent: number;
  widthPercent: number;
  metadata?: Record<string, unknown>;
}

export const TrajectoryTimeline: React.FC<TrajectoryTimelineProps> = ({ events, sessionId }) => {
  const [expandedEvents, setExpandedEvents] = useState<Set<string>>(new Set());
  const [viewMode, setViewMode] = useState<'sequence' | 'duration'>('sequence');
  const [searchQuery, setSearchQuery] = useState('');

  const toggleEvent = (id: string) => {
    const newSet = new Set(expandedEvents);
    if (newSet.has(id)) {
      newSet.delete(id);
    } else {
      newSet.add(id);
    }
    setExpandedEvents(newSet);
  };

  const filteredEvents = useMemo(() => {
    if (!searchQuery) return events;
    const lowerQuery = searchQuery.toLowerCase();
    return events.filter(e =>
      e.type.toLowerCase().includes(lowerQuery) ||
      e.kind?.toLowerCase().includes(lowerQuery) ||
      e.content?.toLowerCase().includes(lowerQuery)
    );
  }, [events, searchQuery]);

  // 计算 Duration 视图的数据
  const durationBars = useMemo((): DurationBar[] => {
    if (events.length === 0) return [];
    
    // 找到第一个事件的时间戳作为基准
    const firstTs = new Date(events[0].timestamp).getTime();
    const lastTs = new Date(events[events.length - 1].timestamp).getTime();
    const totalDurationMs = Math.max(lastTs - firstTs, 1);
    
    const bars: DurationBar[] = [];
    
    for (const e of events) {
      // 只处理有 duration 的事件
      if (!e.duration || e.duration <= 0) continue;
      
      const eventTs = new Date(e.timestamp).getTime();
      const offsetMs = eventTs - firstTs;
      
      let label = '';
      if (e.kind === 'model_call.end' || e.kind === 'model.end') {
        const model = e.metadata?.model || 'model';
        label = `${model}`;
      } else if (e.kind === 'tool_call.end' || e.kind === 'tool.end') {
        const tool = e.metadata?.tool || 'tool';
        const success = e.metadata?.success !== false;
        label = `${success ? '' : '✗ '}${tool}`;
      } else if (e.kind === 'turn.end') {
        label = 'Turn';
      } else {
        continue;
      }
      
      bars.push({
        id: e.id,
        label,
        type: e.type,
        startMs: offsetMs,
        durationMs: e.duration,
        offsetPercent: (offsetMs / totalDurationMs) * 100,
        widthPercent: Math.max((e.duration / totalDurationMs) * 100, 0.5),
        metadata: e.metadata,
      });
    }
    
    return bars;
  }, [events]);

  const getEventIcon = (type: TrajectoryEvent['type']) => {
    switch (type) {
      case 'turn':
        return <Layers className="w-4 h-4 text-blue-500" />;
      case 'model':
        return <Bot className="w-4 h-4 text-purple-500" />;
      case 'tool':
        return <Wrench className="w-4 h-4 text-orange-500" />;
      case 'sub_agent':
        return <Bot className="w-4 h-4 text-indigo-500" />;
      case 'stream':
        return <Radio className="w-4 h-4 text-cyan-500" />;
      case 'audit':
        return <Shield className="w-4 h-4 text-red-500" />;
      case 'cost':
        return <DollarSign className="w-4 h-4 text-green-500" />;
      case 'compact':
        return <Zap className="w-4 h-4 text-yellow-500" />;
      case 'other':
        return <MessageSquare className="w-4 h-4 text-gray-500" />;
    }
  };

  const getEventColor = (type: TrajectoryEvent['type']) => {
    switch (type) {
      case 'turn':
        return 'border-blue-200 bg-blue-50';
      case 'model':
        return 'border-purple-200 bg-purple-50';
      case 'tool':
        return 'border-orange-200 bg-orange-50';
      case 'sub_agent':
        return 'border-indigo-200 bg-indigo-50';
      case 'stream':
        return 'border-cyan-200 bg-cyan-50';
      case 'audit':
        return 'border-red-200 bg-red-50';
      case 'cost':
        return 'border-green-200 bg-green-50';
      case 'compact':
        return 'border-yellow-200 bg-yellow-50';
      case 'other':
        return 'border-gray-200 bg-gray-50';
    }
  };

  const getBarColor = (type: TrajectoryEvent['type']) => {
    switch (type) {
      case 'turn':
        return 'bg-blue-400';
      case 'model':
        return 'bg-purple-400';
      case 'tool':
        return 'bg-orange-400';
      case 'sub_agent':
        return 'bg-indigo-400';
      default:
        return 'bg-gray-400';
    }
  };

  const getEventLabel = (type: TrajectoryEvent['type'], kind?: string) => {
    const labelMap: Record<string, string> = {
      'turn.start': 'Turn Start',
      'turn.end': 'Turn End',
      'model.start': 'Model Call',
      'model.end': 'Model Result',
      'model_call.start': 'Model Call',
      'model_call.end': 'Model Result',
      'tool.start': 'Tool Call',
      'tool.end': 'Tool Result',
      'tool_call.start': 'Tool Call',
      'tool_call.end': 'Tool Result',
      'stream.text': 'Text Stream',
      'stream.thinking': 'Thinking',
      'stream.tool_call': 'Tool Call (Stream)',
      'stream.tool_result': 'Tool Result (Stream)',
      'stream.sub_agent_start': 'Sub-Agent Start',
      'stream.sub_agent_end': 'Sub-Agent End',
      'compact': 'Context Compact',
    };

    if (kind && labelMap[kind]) return labelMap[kind];
    if (kind?.startsWith('audit')) return 'Audit';
    if (kind?.startsWith('cost')) return 'Cost';

    return type.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase());
  };

  const formatTime = (ts: string) => {
    return ts.substring(11, 23);
  };

  const formatDuration = (ms?: number) => {
    if (!ms) return null;
    if (ms < 1000) return `${ms.toFixed(0)}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  };

  const isError = (event: TrajectoryEvent) => {
    return event.kind?.includes('error') ||
           (event.kind === 'tool.end' && event.metadata?.success === false) ||
           (event.kind === 'tool_call.end' && event.metadata?.success === false) ||
           (event.kind === 'stream.sub_agent_end' && event.metadata?.status === 'error');
  };

  // Duration 视图（甘特图）
  const renderDurationView = () => {
    if (durationBars.length === 0) {
      return (
        <div className="p-8 text-center text-gray-500">
          <BarChart3 className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p>No duration data available</p>
          <p className="text-sm mt-1">Duration view requires events with timing information</p>
        </div>
      );
    }

    // 计算总时长用于显示
    const totalMs = Math.max(...durationBars.map(b => b.startMs + b.durationMs));

    return (
      <div className="p-4">
        {/* 时间轴标尺 */}
        <div className="mb-4 relative h-6 border-b border-gray-200">
          {[0, 25, 50, 75, 100].map(pct => (
            <div
              key={pct}
              className="absolute top-0 text-xs text-gray-400"
              style={{ left: `${pct}%`, transform: 'translateX(-50%)' }}
            >
              {formatDuration((pct / 100) * totalMs)}
            </div>
          ))}
        </div>

        {/* 甘特图条 */}
        <div className="space-y-1">
          {durationBars.map((bar) => (
            <div
              key={bar.id}
              className="relative h-7 flex items-center group"
              onClick={() => toggleEvent(bar.id)}
            >
              {/* 行背景 */}
              <div className="absolute inset-0 bg-gray-50 hover:bg-gray-100 rounded" />
              
              {/* 条形 */}
              <div
                className={`absolute h-5 rounded ${getBarColor(bar.type)} opacity-80 group-hover:opacity-100 transition-opacity cursor-pointer`}
                style={{
                  left: `${bar.offsetPercent}%`,
                  width: `${bar.widthPercent}%`,
                  minWidth: '4px',
                }}
              />
              
              {/* 标签 */}
              <div className="relative z-10 flex items-center gap-2 px-2">
                <span className="text-xs font-medium text-gray-700 min-w-[100px]">
                  {bar.label}
                </span>
                <span className="text-xs text-gray-500">
                  {formatDuration(bar.durationMs)}
                </span>
              </div>

              {/* Tooltip */}
              {expandedEvents.has(bar.id) && bar.metadata && (
                <div className="absolute top-8 left-0 z-20 bg-white border border-gray-200 rounded shadow-lg p-2 max-w-sm">
                  <pre className="text-xs text-gray-600 whitespace-pre-wrap font-mono">
                    {JSON.stringify(bar.metadata, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          ))}
        </div>

        {/* 图例 */}
        <div className="mt-6 flex items-center gap-4 text-xs text-gray-500">
          <span className="flex items-center gap-1">
            <div className="w-3 h-3 rounded bg-purple-400" /> Model
          </span>
          <span className="flex items-center gap-1">
            <div className="w-3 h-3 rounded bg-orange-400" /> Tool
          </span>
          <span className="flex items-center gap-1">
            <div className="w-3 h-3 rounded bg-blue-400" /> Turn
          </span>
          <span className="flex items-center gap-1">
            <div className="w-3 h-3 rounded bg-indigo-400" /> Sub-Agent
          </span>
        </div>
      </div>
    );
  };

  // Sequence 视图（列表）
  const renderSequenceView = () => {
    if (filteredEvents.length === 0) {
      return (
        <div className="p-8 text-center text-gray-500">
          <Clock className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p>No events match your filter</p>
        </div>
      );
    }

    return (
      <div className="relative">
        {/* Vertical line */}
        <div className="absolute left-6 top-0 bottom-0 w-0.5 bg-gray-200" />

        {/* Events */}
        <div className="space-y-1.5">
          {filteredEvents.map((event) => (
            <div key={event.id} className="relative flex gap-2">
              {/* Timeline dot */}
              <div className="relative z-10 flex-shrink-0">
                <div className={`w-6 h-6 rounded-full flex items-center justify-center border ${getEventColor(event.type)}`}>
                  {React.cloneElement(getEventIcon(event.type) as React.ReactElement, { className: 'w-3 h-3' })}
                </div>
              </div>

              {/* Event card */}
              <div
                className={`flex-1 p-2 rounded border cursor-pointer transition-colors ${getEventColor(event.type)} ${
                  isError(event) ? 'ring-1 ring-red-300' : ''
                }`}
                onClick={() => toggleEvent(event.id)}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5">
                    {isError(event) && <AlertCircle className="w-3 h-3 text-red-500" />}
                    <span className="text-xs font-medium text-gray-700">
                      {getEventLabel(event.type, event.kind)}
                    </span>
                    {event.metadata?.sub_agent === true && (
                      <span className="text-xs px-1 py-0.5 bg-indigo-100 text-indigo-600 rounded">
                        sub-agent
                      </span>
                    )}
                    <span className="text-xs text-gray-400 font-mono">
                      {formatTime(event.timestamp)}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    {event.duration && (
                      <span className="text-xs text-gray-500 flex items-center gap-0.5">
                        <Zap className="w-2.5 h-2.5" />
                        {formatDuration(event.duration)}
                      </span>
                    )}
                    {expandedEvents.has(event.id) ? (
                      <ChevronDown className="w-3 h-3 text-gray-400" />
                    ) : (
                      <ChevronRight className="w-3 h-3 text-gray-400" />
                    )}
                  </div>
                </div>

                {/* Content preview */}
                {event.content && !expandedEvents.has(event.id) && (
                  <p className="text-xs text-gray-500 truncate mt-0.5 font-mono">
                    {event.content.slice(0, 100)}
                  </p>
                )}

                {/* Expanded content */}
                {expandedEvents.has(event.id) && event.content && (
                  <pre className="text-xs text-gray-600 whitespace-pre-wrap mt-1.5 max-h-48 overflow-y-auto font-mono bg-white/50 p-1.5 rounded">
                    {event.content}
                  </pre>
                )}

                {/* Metadata */}
                {expandedEvents.has(event.id) && event.metadata && Object.keys(event.metadata).length > 0 && (
                  <div className="mt-1.5 pt-1.5 border-t border-gray-200">
                    <pre className="text-xs text-gray-500 whitespace-pre-wrap font-mono bg-white/50 p-1.5 rounded">
                      {JSON.stringify(event.metadata, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  };

  if (events.length === 0) {
    return (
      <div className="p-8 text-center text-gray-500">
        <Clock className="w-12 h-12 mx-auto mb-4 opacity-50" />
        <p>No trajectory events yet</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="p-3 border-b border-gray-200">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-gray-900 flex items-center gap-1.5">
            <Clock className="w-4 h-4 text-blue-500" />
            Timeline
            {sessionId && <span className="text-xs text-gray-400 font-normal">({sessionId})</span>}
          </h3>
          <div className="flex gap-1">
            <button
              onClick={() => setViewMode('sequence')}
              className={`px-2 py-1 text-xs rounded ${
                viewMode === 'sequence'
                  ? 'bg-blue-100 text-blue-700'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              Sequence
            </button>
            <button
              onClick={() => setViewMode('duration')}
              className={`px-2 py-1 text-xs rounded ${
                viewMode === 'duration'
                  ? 'bg-blue-100 text-blue-700'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              Duration
            </button>
          </div>
        </div>
        <input
          type="text"
          placeholder="Search events..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full px-2 py-1 text-xs border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
      </div>

      {/* Overview Bar */}
      <OverviewBar events={events} onSeek={() => {}} />

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-3">
        {viewMode === 'sequence' ? renderSequenceView() : renderDurationView()}
      </div>
    </div>
  );
};

export default TrajectoryTimeline;

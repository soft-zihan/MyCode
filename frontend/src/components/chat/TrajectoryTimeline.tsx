import React, { useState, useMemo } from 'react';
import { Clock, Zap, Bot, Wrench, MessageSquare, ChevronDown, ChevronRight, AlertCircle, Shield, DollarSign, Layers, Radio } from 'lucide-react';

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

  const getEventLabel = (type: TrajectoryEvent['type'], kind?: string) => {
    const labelMap: Record<string, string> = {
      'turn.start': 'Turn Start',
      'turn.end': 'Turn End',
      'model.start': 'Model Call',
      'model.end': 'Model Result',
      'tool.start': 'Tool Call',
      'tool.end': 'Tool Result',
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
    return `${(ms / 1000).toFixed(1)}s`;
  };

  const isError = (event: TrajectoryEvent) => {
    return event.kind?.includes('error') ||
           (event.kind === 'tool.end' && event.metadata?.success === false) ||
           (event.kind === 'stream.sub_agent_end' && event.metadata?.status === 'error');
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
      <div className="p-4 border-b border-gray-200">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-gray-900 flex items-center gap-2">
            <Clock className="w-4 h-4 text-blue-500" />
            Event Timeline
            {sessionId && <span className="text-xs text-gray-500 font-normal">({sessionId})</span>}
          </h3>
          <div className="flex gap-2">
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
          className="w-full px-3 py-1.5 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
      </div>

      {/* Timeline */}
      <div className="flex-1 overflow-y-auto p-4">
        <div className="relative">
          {/* Vertical line */}
          <div className="absolute left-4 top-0 bottom-0 w-0.5 bg-gray-200" />

          {/* Events */}
          <div className="space-y-2">
            {filteredEvents.map((event) => (
              <div key={event.id} className="relative flex gap-3">
                {/* Timeline dot */}
                <div className="relative z-10 flex-shrink-0">
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center border-2 ${getEventColor(event.type)}`}>
                    {getEventIcon(event.type)}
                  </div>
                </div>

                {/* Event card */}
                <div
                  className={`flex-1 p-3 rounded-lg border cursor-pointer transition-colors ${getEventColor(event.type)} ${
                    isError(event) ? 'ring-2 ring-red-300' : ''
                  }`}
                  onClick={() => toggleEvent(event.id)}
                >
                  <div className="flex items-center justify-between mb-1">
                    <div className="flex items-center gap-2">
                      {isError(event) && <AlertCircle className="w-3 h-3 text-red-500" />}
                      <span className="text-xs font-medium text-gray-700">
                        {getEventLabel(event.type, event.kind)}
                      </span>
                      <span className="text-xs text-gray-500 font-mono">
                        {formatTime(event.timestamp)}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      {event.duration && (
                        <span className="text-xs text-gray-500 flex items-center gap-1">
                          <Zap className="w-3 h-3" />
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
                    <p className="text-xs text-gray-600 truncate font-mono">
                      {event.content.slice(0, 120)}
                    </p>
                  )}

                  {/* Expanded content */}
                  {expandedEvents.has(event.id) && event.content && (
                    <pre className="text-xs text-gray-700 whitespace-pre-wrap mt-2 max-h-64 overflow-y-auto font-mono bg-white/50 p-2 rounded">
                      {event.content}
                    </pre>
                  )}

                  {/* Metadata */}
                  {expandedEvents.has(event.id) && event.metadata && Object.keys(event.metadata).length > 0 && (
                    <div className="mt-2 pt-2 border-t border-gray-200">
                      <div className="text-xs text-gray-500 mb-1">Metadata:</div>
                      <pre className="text-xs text-gray-600 whitespace-pre-wrap font-mono bg-white/50 p-2 rounded">
                        {JSON.stringify(event.metadata, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

export default TrajectoryTimeline;

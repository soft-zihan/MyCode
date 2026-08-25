import { useState, useEffect } from 'react';
import { fetchTraceEvents, toggleTrace, TraceEvent } from '../api/client';
import { Activity, RefreshCw, Power } from 'lucide-react';

export default function TracePage() {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [tracePath, setTracePath] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [eventCount, setEventCount] = useState(50);

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

  const handleToggle = async () => {
    try {
      await toggleTrace(!enabled);
      setEnabled(!enabled);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to toggle trace');
    }
  };

  const formatTime = (ts: string) => {
    return ts.substring(11, 23); // HH:MM:SS.mmm
  };

  const getKindColor = (kind: string) => {
    if (kind.startsWith('turn')) return 'text-blue-600 bg-blue-50';
    if (kind.startsWith('tool')) return 'text-green-600 bg-green-50';
    if (kind.startsWith('bg')) return 'text-purple-600 bg-purple-50';
    if (kind === 'compact') return 'text-orange-600 bg-orange-50';
    return 'text-gray-600 bg-gray-50';
  };

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-gray-500">Loading trace events...</div>
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
      <div className="p-6 border-b border-gray-200">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Trace</h1>
            <p className="text-sm text-gray-500 mt-1">
              {events.length} event{events.length !== 1 ? 's' : ''} | 
              Status: <span className={enabled ? 'text-green-600' : 'text-gray-400'}>
                {enabled ? 'ON' : 'OFF'}
              </span>
            </p>
          </div>
          <div className="flex items-center gap-3">
            <select
              value={eventCount}
              onChange={(e) => setEventCount(Number(e.target.value))}
              className="px-3 py-2 border border-gray-300 rounded text-sm"
            >
              <option value={20}>20 events</option>
              <option value={50}>50 events</option>
              <option value={100}>100 events</option>
              <option value={200}>200 events</option>
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
        <div className="text-xs text-gray-500 font-mono">
          {tracePath}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {events.length === 0 ? (
          <div className="p-8 text-center text-gray-500">
            <Activity className="w-12 h-12 mx-auto mb-4 opacity-50" />
            <p>No trace events yet</p>
            <p className="text-sm mt-2">
              {enabled ? 'Events will appear here as they occur' : 'Enable trace to start recording events'}
            </p>
          </div>
        ) : (
          <table className="w-full">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Time
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Kind
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Details
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {events.map((event, idx) => {
                const { ts, kind, ...rest } = event;
                return (
                  <tr key={idx} className="hover:bg-gray-50">
                    <td className="px-6 py-4 whitespace-nowrap text-sm font-mono text-gray-500">
                      {formatTime(ts)}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <span className={`px-2 py-1 text-xs font-medium rounded ${getKindColor(kind)}`}>
                        {kind}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-700">
                      <pre className="whitespace-pre-wrap font-mono text-xs">
                        {JSON.stringify(rest, null, 2)}
                      </pre>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

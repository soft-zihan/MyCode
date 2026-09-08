import { useState, useEffect, useCallback } from 'react';
import { ChevronDown, ChevronRight, RefreshCw, Database, Clock } from 'lucide-react';
import { fetchCompressionStats, fetchContextStore, CompressionStats, ContextStoreData } from '../../api/client';

interface ContextPanelProps {
  sessionId: string | null;
  isStreaming: boolean;
}

function GaugeChart({ utilization, tokenCount, effectiveWindow }: { utilization: number; tokenCount: number; effectiveWindow: number }) {
  const pct = Math.min(utilization * 100, 100);
  const angle = (pct / 100) * 180 - 90;
  const color = pct >= 70 ? '#ef4444' : pct >= 50 ? '#eab308' : '#22c55e';

  const r = 60;
  const cx = 80;
  const cy = 75;
  const arcStart = { x: cx - r, y: cy };
  const arcEnd = { x: cx + r, y: cy };

  const needleRad = (angle * Math.PI) / 180;
  const needleLen = r - 8;
  const needleTip = { x: cx + needleLen * Math.cos(needleRad), y: cy + needleLen * Math.sin(needleRad) };

  const formatK = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;

  return (
    <div className="flex flex-col items-center">
      <svg width="160" height="95" viewBox="0 0 160 95">
        <path d={`M ${arcStart.x} ${arcStart.y} A ${r} ${r} 0 0 1 ${arcEnd.x} ${arcEnd.y}`} fill="none" stroke="#e5e7eb" strokeWidth="10" strokeLinecap="round" />
        {pct > 0 && (
          <path d={`M ${arcStart.x} ${arcStart.y} A ${r} ${r} 0 ${pct > 50 ? 1 : 0} 1 ${needleTip.x} ${needleTip.y}`} fill="none" stroke={color} strokeWidth="10" strokeLinecap="round" />
        )}
        <line x1={cx} y1={cy} x2={needleTip.x} y2={needleTip.y} stroke="#374151" strokeWidth="2" />
        <circle cx={cx} cy={cy} r="4" fill="#374151" />
        <text x={cx} y={cy - 12} textAnchor="middle" className="text-lg font-bold" fill="#111827" fontSize="18">{pct.toFixed(0)}%</text>
        <text x={cx} y={cy + 4} textAnchor="middle" fill="#6b7280" fontSize="10">{formatK(tokenCount)} / {formatK(effectiveWindow)}</text>
      </svg>
      <span className="text-[10px] text-gray-500 -mt-1">Effective Window</span>
    </div>
  );
}

function CompressionPipeline({ stats }: { stats: CompressionStats }) {
  const totalSaved = (stats.l1_budget.tokens_saved || 0) + (stats.l2_snip.tokens_saved || 0) + (stats.l3_microcompact.tokens_saved || 0);
  const formatK = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;

  const rows = [
    { label: 'L1 Budget', icon: '📏', triggered: stats.l1_budget.triggered, saved: stats.l1_budget.tokens_saved },
    { label: 'L2 Snip', icon: '✂️', triggered: stats.l2_snip.triggered, saved: stats.l2_snip.tokens_saved },
    { label: 'L3 Microcompact', icon: '🧹', triggered: stats.l3_microcompact.triggered, saved: stats.l3_microcompact.tokens_saved },
    { label: 'L4 Fold', icon: '📦', triggered: stats.l4_fold.triggered, saved: null },
  ];

  return (
    <div className="space-y-1">
      {rows.map(row => (
        <div key={row.label} className="flex items-center justify-between text-xs py-1 px-2 rounded hover:bg-gray-50">
          <span className="text-gray-700">{row.icon} {row.label}</span>
          <div className="flex items-center gap-3">
            <span className="text-gray-500">{row.triggered} trigger{row.triggered !== 1 ? 's' : ''}</span>
            {row.saved !== null && (
              <span className="text-green-600 font-mono text-[11px]">{formatK(row.saved)} saved</span>
            )}
          </div>
        </div>
      ))}
      <div className="flex items-center justify-between text-xs py-1.5 px-2 border-t border-gray-200 mt-1">
        <span className="font-medium text-gray-700">Total saved</span>
        <span className="text-green-700 font-semibold font-mono">{formatK(totalSaved)} tokens</span>
      </div>
    </div>
  );
}

function ContextStoreList({ data }: { data: ContextStoreData }) {
  const [expanded, setExpanded] = useState(false);
  const activeEntries = data.entries.filter(e => !e.dropped);
  const droppedEntries = data.entries.filter(e => e.dropped);

  const formatSize = (bytes: number) => bytes >= 1024 ? `${(bytes / 1024).toFixed(1)}KB` : `${bytes}B`;

  return (
    <div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-xs font-medium text-gray-700 hover:text-blue-600 w-full"
      >
        {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        Reversible Entries ({activeEntries.length} active)
      </button>
      {expanded && (
        <div className="mt-1 space-y-0.5 max-h-48 overflow-y-auto">
          {activeEntries.map(entry => (
            <div key={entry.key} className="flex items-center justify-between text-[11px] py-1 px-2 bg-gray-50 rounded">
              <span className="text-gray-600 font-mono truncate max-w-[160px]" title={entry.key}>{entry.key}</span>
              <span className="text-gray-400">{formatSize(entry.raw_size)}</span>
            </div>
          ))}
          {droppedEntries.map(entry => (
            <div key={entry.key} className="flex items-center justify-between text-[11px] py-1 px-2 bg-red-50 rounded opacity-60">
              <span className="text-gray-500 font-mono truncate max-w-[160px] line-through" title={entry.key}>{entry.key}</span>
              <span className="text-red-400">dropped</span>
            </div>
          ))}
          {data.entries.length === 0 && (
            <div className="text-[11px] text-gray-400 px-2 py-1">No entries</div>
          )}
        </div>
      )}
    </div>
  );
}

function FoldHistory({ memories }: { memories: any[] }) {
  const [expanded, setExpanded] = useState(false);

  if (!memories || memories.length === 0) return null;

  return (
    <div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-xs font-medium text-gray-700 hover:text-blue-600 w-full"
      >
        {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        Fold History ({memories.length})
      </button>
      {expanded && (
        <div className="mt-1 space-y-2 max-h-64 overflow-y-auto">
          {memories.map((mem, idx) => (
            <div key={idx} className="text-[11px] bg-gray-50 rounded p-2 border-l-2 border-purple-300">
              <div className="flex items-center gap-2 mb-1">
                <span className="w-1.5 h-1.5 bg-purple-400 rounded-full" />
                <span className="text-gray-500">{mem.time || `Fold ${idx + 1}`}</span>
                <span className="px-1.5 py-0.5 bg-purple-100 text-purple-700 rounded text-[10px]">{mem.trigger || 'auto'}</span>
              </div>
              {mem.episode && <div className="text-gray-600 ml-3.5">episode: {mem.episode}</div>}
              {mem.working && <div className="text-gray-600 ml-3.5">working: {mem.working}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ContextPanel({ sessionId, isStreaming }: ContextPanelProps) {
  const [compressionStats, setCompressionStats] = useState<CompressionStats | null>(null);
  const [contextStore, setContextStore] = useState<ContextStoreData | null>(null);
  const [loading, setLoading] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const loadData = useCallback(async () => {
    if (!sessionId) return;
    setLoading(true);
    try {
      const [stats, store] = await Promise.all([
        fetchCompressionStats(sessionId),
        fetchContextStore(sessionId),
      ]);
      setCompressionStats(stats);
      setContextStore(store);
    } catch (err) {
      console.error('Failed to load context panel data:', err);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    if (!isStreaming && sessionId) {
      loadData();
    }
  }, [isStreaming, sessionId, loadData]);

  if (!sessionId) {
    return (
      <div className="p-3 text-xs text-gray-400 text-center">
        Start a session to view context engine
      </div>
    );
  }

  if (collapsed) {
    return (
      <div className="border-b border-gray-200 px-3 py-1.5 flex items-center justify-between">
        <span className="text-[10px] font-medium text-gray-500">Context Engine</span>
        <button onClick={() => setCollapsed(false)} className="p-0.5 hover:bg-gray-100 rounded">
          <ChevronDown className="w-3 h-3 text-gray-400" />
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200">
        <div className="flex items-center gap-1.5">
          <Database className="w-3.5 h-3.5 text-blue-500" />
          <span className="text-xs font-semibold text-gray-900">Context Engine</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={loadData}
            disabled={loading}
            className="p-1 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded transition-colors"
            title="Refresh"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button onClick={() => setCollapsed(true)} className="p-0.5 hover:bg-gray-100 rounded">
            <ChevronDown className="w-3 h-3 text-gray-400 rotate-[-90deg]" />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        {compressionStats && (
          <>
            <GaugeChart
              utilization={compressionStats.utilization}
              tokenCount={compressionStats.token_count}
              effectiveWindow={compressionStats.effective_window}
            />

            <div>
              <h3 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-1.5">Compression Pipeline</h3>
              <CompressionPipeline stats={compressionStats} />
            </div>
          </>
        )}

        {contextStore && contextStore.total_entries > 0 && (
          <div>
            <h3 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-1.5">Context Store</h3>
            <ContextStoreList data={contextStore} />
          </div>
        )}

        {compressionStats && compressionStats.folded_memories && compressionStats.folded_memories.length > 0 && (
          <div>
            <h3 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-1.5">Memory</h3>
            <FoldHistory memories={compressionStats.folded_memories} />
          </div>
        )}

        {compressionStats?.l4_fold?.last_fold_time && (
          <div className="flex items-center gap-1.5 text-[10px] text-gray-400">
            <Clock className="w-3 h-3" />
            Last fold: {new Date(compressionStats.l4_fold.last_fold_time).toLocaleString()}
          </div>
        )}
      </div>
    </div>
  );
}

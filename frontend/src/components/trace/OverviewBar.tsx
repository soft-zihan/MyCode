import React, { useRef, useState, useCallback, useEffect } from 'react';

interface TimelineEvent {
  id: string;
  timestamp: string;
  type: 'turn' | 'model' | 'tool' | 'sub_agent' | 'stream' | 'audit' | 'cost' | 'compact' | 'other';
  duration?: number;
}

interface OverviewBarProps {
  events: TimelineEvent[];
  onSeek: (index: number) => void;
  currentIndex?: number;
}

const getTypeColor = (type: TimelineEvent['type']): string => {
  switch (type) {
    case 'turn': return 'bg-indigo-400';
    case 'model': return 'bg-purple-400';
    case 'tool': return 'bg-orange-400';
    case 'sub_agent': return 'bg-indigo-400';
    case 'stream': return 'bg-cyan-400';
    case 'audit': return 'bg-red-400';
    case 'cost': return 'bg-green-400';
    case 'compact': return 'bg-yellow-400';
    default: return 'bg-gray-400';
  }
};

export const OverviewBar: React.FC<OverviewBarProps> = ({ events, onSeek, currentIndex }) => {
  const barRef = useRef<HTMLDivElement>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const getIndexFromPosition = useCallback((clientX: number): number => {
    if (!barRef.current || events.length === 0) return 0;
    const rect = barRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(clientX - rect.left, rect.width));
    const ratio = x / rect.width;
    return Math.min(Math.floor(ratio * events.length), events.length - 1);
  }, [events]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    setIsDragging(true);
    const index = getIndexFromPosition(e.clientX);
    onSeek(index);
  }, [getIndexFromPosition, onSeek]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    const index = getIndexFromPosition(e.clientX);
    setHoverIndex(index);
    if (isDragging) {
      onSeek(index);
    }
  }, [getIndexFromPosition, isDragging, onSeek]);

  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  const handleMouseLeave = useCallback(() => {
    setHoverIndex(null);
    setIsDragging(false);
  }, []);

  useEffect(() => {
    const handleGlobalMouseUp = () => setIsDragging(false);
    window.addEventListener('mouseup', handleGlobalMouseUp);
    return () => window.removeEventListener('mouseup', handleGlobalMouseUp);
  }, []);

  if (events.length === 0) return null;

  const firstTs = new Date(events[0].timestamp).getTime();
  const lastTs = new Date(events[events.length - 1].timestamp).getTime();
  const totalDuration = Math.max(lastTs - firstTs, 1);

  return (
    <div className="overview-bar-container">
      <div
        ref={barRef}
        className="overview-bar"
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseLeave}
      >
        {/* Event markers */}
        {events.map((event, index) => {
          const eventTs = new Date(event.timestamp).getTime();
          const offsetPercent = ((eventTs - firstTs) / totalDuration) * 100;
          const isHovered = hoverIndex === index;
          const isCurrent = currentIndex === index;

          return (
            <div
              key={event.id}
              className={`overview-bar-marker ${getTypeColor(event.type)} ${isHovered ? 'hovered' : ''} ${isCurrent ? 'current' : ''}`}
              style={{ left: `${offsetPercent}%` }}
              title={`${event.type} @ ${event.timestamp.substring(11, 19)}`}
            />
          );
        })}

        {/* Playhead */}
        {currentIndex !== undefined && (
          <div
            className="overview-bar-playhead"
            style={{ left: `${(currentIndex / events.length) * 100}%` }}
          />
        )}

        {/* Hover indicator */}
        {hoverIndex !== null && (
          <div
            className="overview-bar-hover"
            style={{ left: `${(hoverIndex / events.length) * 100}%` }}
          />
        )}
      </div>

      {/* Time labels */}
      <div className="overview-bar-labels">
        <span>{events[0].timestamp.substring(11, 19)}</span>
        <span>{((totalDuration / 1000)).toFixed(1)}s total</span>
        <span>{events[events.length - 1].timestamp.substring(11, 19)}</span>
      </div>

      {/* Hover tooltip */}
      {hoverIndex !== null && events[hoverIndex] && (
        <div className="overview-bar-tooltip">
          <span className="overview-bar-tooltip-type">{events[hoverIndex].type}</span>
          <span className="overview-bar-tooltip-time">
            {events[hoverIndex].timestamp.substring(11, 19)}
          </span>
        </div>
      )}
    </div>
  );
};

export default OverviewBar;

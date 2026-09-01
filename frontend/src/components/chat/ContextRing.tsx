import React from 'react';

interface ContextRingProps {
  used: number;
  total: number;
  size?: number;
  strokeWidth?: number;
}

export const ContextRing: React.FC<ContextRingProps> = ({
  used,
  total,
  size = 32,
  strokeWidth = 3,
}) => {
  const percentage = Math.min((used / total) * 100, 100);
  const radius = (size - strokeWidth) / 2;
  const circumference = radius * 2 * Math.PI;
  const offset = circumference - (percentage / 100) * circumference;

  const getColor = () => {
    if (percentage >= 90) return 'text-red-500';
    if (percentage >= 70) return 'text-yellow-500';
    return 'text-green-500';
  };

  const formatNumber = (n: number) => {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
    return n.toString();
  };

  return (
    <div className="relative inline-flex items-center justify-center" title={`${formatNumber(used)} / ${formatNumber(total)} tokens`}>
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-gray-200"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className={`${getColor()} transition-all duration-300`}
        />
      </svg>
      <span className="absolute text-[8px] font-bold text-gray-600">
        {Math.round(percentage)}%
      </span>
    </div>
  );
};

export default ContextRing;

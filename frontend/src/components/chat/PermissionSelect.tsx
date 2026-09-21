import React, { useState, useRef, useEffect } from 'react';
import { Shield, ShieldCheck, ShieldAlert, Compass, ChevronDown } from 'lucide-react';

type PermissionMode = 'default' | 'acceptEdits' | 'plan' | 'bypassPermissions';

interface PermissionSelectProps {
  mode: PermissionMode;
  onChange: (mode: PermissionMode) => void;
  disabled?: boolean;
}

const MODES: { value: PermissionMode; label: string; description: string; icon: React.FC<{className?: string}>; color: string }[] = [
  { 
    value: 'default', 
    label: 'Default', 
    description: 'Write operations require confirmation',
    icon: Shield,
    color: 'gray',
  },
  { 
    value: 'acceptEdits', 
    label: 'Edits', 
    description: 'Edit tools auto-approved, dangerous commands need confirmation',
    icon: ShieldAlert,
    color: 'yellow',
  },
  { 
    value: 'plan', 
    label: 'Plan', 
    description: '只读研究并制定计划，审批通过后执行',
    icon: Compass,
    color: 'blue',
  },
  { 
    value: 'bypassPermissions', 
    label: 'YOLO', 
    description: 'All operations auto-approved',
    icon: ShieldCheck,
    color: 'green',
  },
];

export const PermissionSelect: React.FC<PermissionSelectProps> = ({
  mode,
  onChange,
  disabled = false,
}) => {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const current = MODES.find(m => m.value === mode) || MODES[0];
  const Icon = current.icon;

  const colorClasses: Record<string, { active: string; hover: string; badge: string }> = {
    gray: {
      active: 'text-gray-700 bg-white border-gray-300',
      hover: 'hover:bg-gray-50',
      badge: 'bg-gray-100 text-gray-700',
    },
    yellow: {
      active: 'text-yellow-700 bg-yellow-50 border-yellow-300',
      hover: 'hover:bg-yellow-50',
      badge: 'bg-yellow-100 text-yellow-700',
    },
    green: {
      active: 'text-green-700 bg-green-50 border-green-300',
      hover: 'hover:bg-green-50',
      badge: 'bg-green-100 text-green-700',
    },
    blue: {
      active: 'text-indigo-700 bg-indigo-50 border-indigo-300',
      hover: 'hover:bg-indigo-50',
      badge: 'bg-indigo-100 text-indigo-700',
    },
  };

  return (
    <div ref={ref} className="permission-select relative">
      <button
        onClick={() => !disabled && setOpen(!open)}
        disabled={disabled}
        className={`permission-select-trigger flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded border transition-colors ${
          colorClasses[current.color].active
        } ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
      >
        <Icon className="w-3.5 h-3.5" />
        {current.label}
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="permission-select-dropdown absolute bottom-full left-0 mb-1 w-64 bg-white border border-gray-200 rounded-lg shadow-lg z-50 overflow-hidden">
          {MODES.map((m) => {
            const MIcon = m.icon;
            const isActive = m.value === mode;
            return (
              <button
                key={m.value}
                onClick={() => {
                  onChange(m.value);
                  setOpen(false);
                }}
                className={`permission-select-option w-full flex items-start gap-2.5 px-3 py-2.5 text-left transition-colors ${
                  isActive ? 'bg-indigo-50' : 'hover:bg-gray-50'
                }`}
              >
                <MIcon className={`w-4 h-4 mt-0.5 flex-shrink-0 ${
                  m.color === 'green' ? 'text-green-600' :
                  m.color === 'yellow' ? 'text-yellow-600' :
                  m.color === 'blue' ? 'text-indigo-600' : 'text-gray-500'
                }`} />
                <div className="flex-1 min-w-0">
                  <div className={`text-xs font-medium ${isActive ? 'text-indigo-700' : 'text-gray-700'}`}>
                    {m.label}
                  </div>
                  <div className="text-[10px] text-gray-500 mt-0.5 leading-tight">
                    {m.description}
                  </div>
                </div>
                {isActive && (
                  <div className="w-1.5 h-1.5 rounded-full bg-indigo-500 mt-1.5 flex-shrink-0" />
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default PermissionSelect;

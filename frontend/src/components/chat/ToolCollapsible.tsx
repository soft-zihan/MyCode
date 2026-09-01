import { useState, useEffect, ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';

interface ToolCollapsibleProps {
  label: string;
  icon: string;
  defaultOpen?: boolean;
  children: ReactNode;
}

export function ToolCollapsible({ label, icon, defaultOpen = false, children }: ToolCollapsibleProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  
  useEffect(() => {
    setIsOpen(defaultOpen);
  }, [defaultOpen]);
  
  return (
    <details
      open={isOpen}
      onToggle={(e) => setIsOpen((e.target as HTMLDetailsElement).open)}
      className="my-1 border border-gray-200 rounded-lg bg-gray-50 overflow-hidden group/details"
    >
      <summary className="flex items-center gap-2 px-3 py-1.5 cursor-pointer hover:bg-gray-100 transition-colors select-none text-sm font-medium text-gray-700 list-none [&::-webkit-details-marker]:hidden">
        <span className="text-xs">{icon}</span>
        <span className="flex-1">{label}</span>
        <ChevronDown className="w-3 h-3 text-gray-400 transition-transform group-open/details:rotate-180" />
      </summary>
      <div className="px-3 pb-2 border-t border-gray-200">
        {children}
      </div>
    </details>
  );
}

export default ToolCollapsible;

import { useState, useEffect, useCallback } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { MessageSquare, Brain, Sparkles, Bot, Wrench, LineChart, PanelLeft, PanelRight } from 'lucide-react';

interface AppSidebarProps {
  children: React.ReactNode;
}

const navItems = [
  { path: '/', icon: MessageSquare, label: 'Chat' },
  { path: '/agents', icon: Bot, label: 'Agents' },
  { path: '/mcp', icon: Wrench, label: 'Tools' },
  { path: '/memory', icon: Brain, label: 'Memory' },
  { path: '/skills', icon: Sparkles, label: 'Skills' },
  { path: '/trace', icon: LineChart, label: 'Trace' },
];

const MIN_WIDTH = 180;
const MAX_WIDTH = 400;
const DEFAULT_WIDTH = 224;
const STORAGE_KEY = 'sidebar-width';

export function AppSidebar({ children }: AppSidebarProps) {
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const [width, setWidth] = useState(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    return saved ? parseInt(saved, 10) : DEFAULT_WIDTH;
  });
  const [isDragging, setIsDragging] = useState(false);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, String(width));
  }, [width]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!isDragging) return;
    const newWidth = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, e.clientX));
    setWidth(newWidth);
  }, [isDragging]);

  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  useEffect(() => {
    if (isDragging) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    } else {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isDragging, handleMouseMove, handleMouseUp]);

  const navBar = (
    <div className="border-t border-gray-200 bg-gray-50">
      <div className="flex flex-col gap-0.5 px-2 py-1.5">
        {navItems.map(item => (
          <Link
            key={item.path}
            to={item.path}
            className={`flex items-center gap-2 px-2.5 py-1.5 text-xs rounded transition-colors ${
              location.pathname === item.path
                ? 'bg-blue-100 text-blue-600 font-medium'
                : 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'
            }`}
          >
            <item.icon className="w-3.5 h-3.5" />
            <span>{item.label}</span>
          </Link>
        ))}
      </div>
      <div className="flex justify-center px-2 pb-2">
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="w-full flex items-center justify-center gap-1.5 p-1.5 text-gray-400 hover:bg-gray-200 rounded transition-colors"
          title="Collapse sidebar"
        >
          <PanelLeft className="w-4 h-4" />
          <span className="text-xs font-semibold tracking-wide text-gray-500">MyCode</span>
        </button>
      </div>
    </div>
  );

  if (collapsed) {
    return (
      <div className="w-12 border-r border-gray-200 bg-white flex flex-col h-full">
        <div className="flex-1" />
        <div className="border-t border-gray-200 bg-gray-50 py-2">
          <div className="flex flex-col items-center gap-1 px-1">
            {navItems.map(item => (
              <Link
                key={item.path}
                to={item.path}
                className={`p-2 rounded-lg transition-colors ${
                  location.pathname === item.path
                    ? 'bg-blue-100 text-blue-600'
                    : 'text-gray-500 hover:bg-gray-200 hover:text-gray-700'
                }`}
                title={item.label}
              >
                <item.icon className="w-4 h-4" />
              </Link>
            ))}
          </div>
          <div className="flex justify-center px-2 mt-2">
            <button
              onClick={() => setCollapsed(false)}
              className="w-full flex items-center justify-center p-1.5 text-gray-400 hover:bg-gray-200 rounded transition-colors"
              title="Expand sidebar"
            >
              <PanelRight className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative border-r border-gray-200 bg-white flex flex-col h-full" style={{ width: `${width}px` }}>
      <div className="flex-1 overflow-y-auto">
        {children}
      </div>
      {navBar}
      <div
        className="absolute top-0 right-0 w-1 h-full cursor-col-resize hover:bg-blue-400 active:bg-blue-500 transition-colors"
        onMouseDown={handleMouseDown}
      />
    </div>
  );
}

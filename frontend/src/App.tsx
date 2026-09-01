import { BrowserRouter, Link, useLocation } from 'react-router-dom';
import { MessageSquare, Database, Wrench, Clock, Server, Bot } from 'lucide-react';
import ChatPage from './pages/ChatPage';
import MemoryPage from './pages/MemoryPage';
import SkillsPage from './pages/SkillsPage';
import TracePage from './pages/TracePage';
import McpPage from './pages/McpPage';
import AgentsPage from './pages/AgentsPage';

function Navigation() {
  const location = useLocation();
  
  const navItems = [
    { path: '/', label: 'Chat', icon: MessageSquare },
    { path: '/mcp', label: 'Tools', icon: Server },
    { path: '/agents', label: 'Agents', icon: Bot },
    { path: '/memory', label: 'Memory', icon: Database },
    { path: '/skills', label: 'Skills', icon: Wrench },
    { path: '/trace', label: 'Trajectory', icon: Clock },
  ];

  return (
    <>
      {/* Desktop sidebar */}
      <nav className="hidden md:flex w-64 bg-gray-900 text-white flex-col">
        <div className="p-6 border-b border-gray-700">
          <h1 className="text-2xl font-bold">MyCode</h1>
          <p className="text-sm text-gray-400 mt-1">Visual Interface</p>
        </div>
        <div className="flex-1 py-4">
          {navItems.map(item => {
            const Icon = item.icon;
            const isActive = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                className={`flex items-center px-6 py-3 text-sm transition-colors ${
                  isActive
                    ? 'bg-gray-800 text-white border-r-2 border-blue-500'
                    : 'text-gray-400 hover:bg-gray-800 hover:text-white'
                }`}
              >
                <Icon className="w-5 h-5 mr-3" />
                {item.label}
              </Link>
            );
          })}
        </div>
        <div className="p-4 border-t border-gray-700 text-xs text-gray-500">
          <div>Backend: localhost:8000</div>
          <div>Frontend: localhost:5173</div>
        </div>
      </nav>

      {/* Mobile top navigation */}
      <nav className="md:hidden bg-gray-900 border-b border-gray-700">
        <div className="flex justify-around items-center">
          {navItems.map(item => {
            const Icon = item.icon;
            const isActive = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                className={`flex flex-col items-center justify-center py-3 px-2 min-w-0 flex-1 ${
                  isActive
                    ? 'text-blue-500'
                    : 'text-gray-400'
                }`}
              >
                <Icon className="w-5 h-5" />
                <span className="text-xs mt-1 truncate">{item.label}</span>
              </Link>
            );
          })}
        </div>
      </nav>
    </>
  );
}

function PageContainer() {
  const location = useLocation();
  const path = location.pathname;
  
  return (
    <main className="flex-1 overflow-hidden">
      <div className={path === '/' ? 'block h-full' : 'hidden h-full'}>
        <ChatPage />
      </div>
      <div className={path === '/mcp' ? 'block h-full' : 'hidden h-full'}>
        <McpPage />
      </div>
      <div className={path === '/agents' ? 'block h-full' : 'hidden h-full'}>
        <AgentsPage />
      </div>
      <div className={path === '/memory' ? 'block h-full' : 'hidden h-full'}>
        <MemoryPage />
      </div>
      <div className={path === '/skills' ? 'block h-full' : 'hidden h-full'}>
        <SkillsPage />
      </div>
      <div className={path === '/trace' ? 'block h-full' : 'hidden h-full'}>
        <TracePage />
      </div>
    </main>
  );
}

function App() {
  return (
    <BrowserRouter>
      <div className="flex h-screen bg-gray-100">
        <Navigation />
        <PageContainer />
      </div>
    </BrowserRouter>
  );
}

export default App;

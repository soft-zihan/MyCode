import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom';
import { MessageSquare, Database, Wrench, Activity, FolderTree, Server, Bot } from 'lucide-react';
import ChatPage from './pages/ChatPage';
import SessionsPage from './pages/SessionsPage';
import MemoryPage from './pages/MemoryPage';
import SkillsPage from './pages/SkillsPage';
import TracePage from './pages/TracePage';
import McpPage from './pages/McpPage';
import AgentsPage from './pages/AgentsPage';

function Navigation() {
  const location = useLocation();
  
  const navItems = [
    { path: '/', label: 'Chat', icon: MessageSquare },
    { path: '/agents', label: 'Agents', icon: Bot },
    { path: '/memory', label: 'Memory', icon: Database },
    { path: '/skills', label: 'Skills', icon: Wrench },
    { path: '/mcp', label: 'MCP', icon: Server },
    { path: '/trace', label: 'Trace', icon: Activity },
  ];

  return (
    <nav className="w-64 bg-gray-900 text-white flex flex-col">
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
  );
}

function App() {
  return (
    <BrowserRouter>
      <div className="flex h-screen bg-gray-100">
        <Navigation />
        <main className="flex-1 overflow-hidden">
          <Routes>
            <Route path="/" element={<ChatPage />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/skills" element={<SkillsPage />} />
            <Route path="/mcp" element={<McpPage />} />
            <Route path="/trace" element={<TracePage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

export default App;

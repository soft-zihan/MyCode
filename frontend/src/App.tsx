import { BrowserRouter, Routes, Route } from 'react-router-dom';
import ChatPage from './pages/ChatPage';
import MemoryPage from './pages/MemoryPage';
import SkillsPage from './pages/SkillsPage';
import TracePage from './pages/TracePage';
import ToolsPage from './pages/ToolsPage';
import AgentsPage from './pages/AgentsPage';

function App() {
  return (
    <BrowserRouter>
      <div className="flex h-screen bg-gray-100 overflow-hidden">
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/mcp" element={<ToolsPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/trace" element={<TracePage />} />
        </Routes>
      </div>
    </BrowserRouter>
  );
}

export default App;

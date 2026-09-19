import { useState, useEffect } from 'react';
import { Server, CheckCircle, Loader } from 'lucide-react';
import { fetchMcpServers, McpServer } from '../../api/client';

export function McpPanel() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        setLoading(true);
        const data = await fetchMcpServers();
        setServers(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load');
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  if (loading) {
    return (
      <div className="p-3 flex items-center gap-2 text-xs text-gray-500">
        <Loader className="w-3 h-3 animate-spin" />
        加载中...
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-3 text-xs text-red-500">
        错误: {error}
      </div>
    );
  }

  if (servers.length === 0) {
    return (
      <div className="p-3 text-xs text-gray-500">
        暂无 MCP 服务
      </div>
    );
  }

  return (
    <div className="p-2 space-y-1">
      {servers.map((server) => (
        <div
          key={server.name}
          className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50"
        >
          <Server className="w-3.5 h-3.5 text-gray-400" />
          <span className="text-xs font-medium text-gray-700 flex-1">
            {server.name}
          </span>
          <CheckCircle className="w-3.5 h-3.5 text-green-500" />
        </div>
      ))}
    </div>
  );
}

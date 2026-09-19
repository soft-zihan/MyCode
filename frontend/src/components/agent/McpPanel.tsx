import { useState, useEffect } from 'react';
import { Server, Loader, Power } from 'lucide-react';
import { fetchMcpStatus, enableMcpServer, disableMcpServer, McpServerStatus } from '../../api/client';

export function McpPanel() {
  const [servers, setServers] = useState<McpServerStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [toggling, setToggling] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = async () => {
    try {
      setLoading(true);
      const data = await fetchMcpStatus();
      setServers(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleToggle = async (serverName: string, enabled: boolean) => {
    setToggling(serverName);
    try {
      if (enabled) {
        await disableMcpServer(serverName);
      } else {
        await enableMcpServer(serverName);
      }
      await loadStatus();
    } catch (err) {
      console.error('Toggle failed:', err);
    } finally {
      setToggling(null);
    }
  };

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
          <Server className={`w-3.5 h-3.5 ${server.enabled ? 'text-green-500' : 'text-gray-400'}`} />
          <span className="text-xs font-medium text-gray-700 flex-1">
            {server.name}
          </span>
          <span className="text-[10px] text-gray-400">
            {server.tool_count} tools
          </span>
          <button
            onClick={() => handleToggle(server.name, server.enabled)}
            disabled={toggling === server.name}
            className={`p-1 rounded transition-colors ${
              server.enabled
                ? 'text-green-600 hover:bg-green-100'
                : 'text-gray-400 hover:bg-gray-100'
            }`}
            title={server.enabled ? '禁用' : '启用'}
          >
            {toggling === server.name ? (
              <Loader className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Power className="w-3.5 h-3.5" />
            )}
          </button>
        </div>
      ))}
    </div>
  );
}

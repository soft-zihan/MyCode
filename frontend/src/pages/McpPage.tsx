import { useState, useEffect } from 'react';
import { Server, RefreshCw, Wrench, Cpu } from 'lucide-react';
import { fetchMcpServers, fetchNativeTools } from '../api/client';

interface McpServer {
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
}

interface McpTool {
  name: string;
  full_name: string;
  description: string;
  input_schema: any;
}

interface McpServerWithTools {
  server: string;
  tools: McpTool[];
  tool_count: number;
}

interface NativeTool {
  name: string;
  description: string;
  input_schema: any;
}

export default function McpPage() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [serverTools, setServerTools] = useState<McpServerWithTools[]>([]);
  const [nativeTools, setNativeTools] = useState<NativeTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingTools, setLoadingTools] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedServer, setExpandedServer] = useState<string | null>(null);
  const [expandedNative, setExpandedNative] = useState<boolean>(true);

  const loadServers = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMcpServers();
      setServers(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load MCP servers');
    } finally {
      setLoading(false);
    }
  };

  const loadTools = async () => {
    setLoadingTools(true);
    try {
      const response = await fetch('/api/mcp/tools');
      const data = await response.json();
      setServerTools(data);
    } catch (err) {
      console.error('Failed to load MCP tools:', err);
    } finally {
      setLoadingTools(false);
    }
  };

  const loadNativeTools = async () => {
    try {
      const data = await fetchNativeTools();
      setNativeTools(data);
    } catch (err) {
      console.error('Failed to load native tools:', err);
    }
  };

  useEffect(() => {
    loadServers();
    loadTools();
    loadNativeTools();
  }, []);

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-gray-500">Loading MCP servers...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-red-500">{error}</div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white">
      <div className="p-6 border-b border-gray-200">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">MCP Servers</h1>
            <p className="text-sm text-gray-500 mt-1">
              {servers.length} server{servers.length !== 1 ? 's' : ''} configured • {serverTools.reduce((sum, s) => sum + s.tool_count, 0)} tools available
            </p>
          </div>
          <button
            onClick={() => { loadServers(); loadTools(); }}
            className="flex items-center px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors"
          >
            <RefreshCw className="w-4 h-4 mr-2" />
            Refresh
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {/* Native Tools Section */}
        <div className="p-6 border-b border-gray-200">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <Cpu className="w-5 h-5 text-orange-500" />
              <h2 className="text-lg font-semibold text-gray-900">Native Tools</h2>
              <span className="text-sm text-gray-500">({nativeTools.length} tools)</span>
            </div>
            <button
              onClick={() => setExpandedNative(!expandedNative)}
              className="text-sm text-blue-600 hover:text-blue-700"
            >
              {expandedNative ? '▼ Collapse' : '▶ Expand'}
            </button>
          </div>
          {expandedNative && (
            <div className="space-y-3">
              {nativeTools.map(tool => (
                <div key={tool.name} className="bg-orange-50 rounded-lg p-4 border border-orange-200">
                  <div className="flex items-center gap-2 mb-2">
                    <Wrench className="w-4 h-4 text-orange-500" />
                    <span className="font-mono font-semibold text-gray-900">{tool.name}</span>
                  </div>
                  {tool.description && (
                    <p className="text-sm text-gray-600 mb-2">{tool.description}</p>
                  )}
                  {tool.input_schema && Object.keys(tool.input_schema.properties || {}).length > 0 && (
                    <div>
                      <span className="text-xs font-medium text-gray-500 uppercase">Parameters</span>
                      <div className="mt-1 space-y-1">
                        {Object.entries(tool.input_schema.properties).map(([paramName, paramDef]: [string, any]) => (
                          <div key={paramName} className="text-xs font-mono bg-white rounded px-2 py-1 border border-orange-100">
                            <span className="text-purple-600">{paramName}</span>
                            <span className="text-gray-500">: {paramDef.type || 'any'}</span>
                            {paramDef.description && (
                              <span className="text-gray-400 ml-2">- {paramDef.description}</span>
                            )}
                            {tool.input_schema.required?.includes(paramName) && (
                              <span className="text-red-500 ml-2">*</span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* MCP Servers Section */}
        <div className="p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4 flex items-center gap-2">
            <Server className="w-5 h-5 text-green-500" />
            MCP Servers
          </h2>
        {servers.length === 0 ? (
          <div className="p-8 text-center text-gray-500">
            <Server className="w-12 h-12 mx-auto mb-4 opacity-50" />
            <p>No MCP servers configured</p>
            <p className="text-sm mt-2">Add MCP servers in .mcp.json</p>
          </div>
        ) : (
          <div className="divide-y divide-gray-200">
            {servers.map(server => {
              const tools = serverTools.find(s => s.server === server.name);
              const isExpanded = expandedServer === server.name;
              
              return (
                <div key={server.name} className="p-6 hover:bg-gray-50">
                  <div className="flex items-start">
                    <Server className="w-6 h-6 mr-3 text-green-500 flex-shrink-0 mt-1" />
                    <div className="flex-1">
                      <div className="flex items-center justify-between mb-2">
                        <h3 className="text-lg font-semibold text-gray-900">
                          {server.name}
                        </h3>
                        {tools && (
                          <button
                            onClick={() => setExpandedServer(isExpanded ? null : server.name)}
                            className="text-sm text-blue-600 hover:text-blue-700"
                          >
                            {tools.tool_count} tools {isExpanded ? '▼' : '▶'}
                          </button>
                        )}
                      </div>
                      <div className="space-y-2">
                        <div>
                          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Command</span>
                          <div className="mt-1 text-sm text-gray-700 font-mono bg-gray-50 rounded px-3 py-2">
                            {server.command} {server.args.join(' ')}
                          </div>
                        </div>
                        {Object.keys(server.env).length > 0 && (
                          <div>
                            <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Environment Variables</span>
                            <div className="mt-1 space-y-1">
                              {Object.entries(server.env).map(([key, value]) => (
                                <div key={key} className="text-sm font-mono bg-gray-50 rounded px-3 py-1.5">
                                  <span className="text-blue-600">{key}</span>
                                  <span className="text-gray-400">=</span>
                                  <span className="text-gray-700">{value}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                      
                      {isExpanded && tools && (
                        <div className="mt-4 space-y-3">
                          <div className="text-sm font-medium text-gray-700">Available Tools:</div>
                          {tools.tools.map(tool => (
                            <div key={tool.name} className="bg-gray-50 rounded p-3 border border-gray-200">
                              <div className="flex items-start justify-between mb-2">
                                <div className="flex items-center gap-2">
                                  <Wrench className="w-4 h-4 text-blue-500" />
                                  <span className="font-mono font-semibold text-gray-900">{tool.name}</span>
                                </div>
                              </div>
                              {tool.description && (
                                <p className="text-sm text-gray-600 mb-2">{tool.description}</p>
                              )}
                              {tool.input_schema && Object.keys(tool.input_schema.properties || {}).length > 0 && (
                                <div>
                                  <span className="text-xs font-medium text-gray-500 uppercase">Parameters</span>
                                  <div className="mt-1 space-y-1">
                                    {Object.entries(tool.input_schema.properties).map(([paramName, paramDef]: [string, any]) => (
                                      <div key={paramName} className="text-xs font-mono bg-white rounded px-2 py-1 border border-gray-200">
                                        <span className="text-purple-600">{paramName}</span>
                                        <span className="text-gray-500">: {paramDef.type || 'any'}</span>
                                        {paramDef.description && (
                                          <span className="text-gray-400 ml-2">- {paramDef.description}</span>
                                        )}
                                        {tool.input_schema.required?.includes(paramName) && (
                                          <span className="text-red-500 ml-2">*</span>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
        </div>
      </div>
    </div>
  );
}

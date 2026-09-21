import { useState, useEffect } from 'react';
import { Server, RefreshCw, Wrench, Cpu, Power, Zap, ChevronRight } from 'lucide-react';
import { fetchMcpServers } from '../api/client';
import { PageLayout } from '../components/PageLayout';

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
  enabled?: boolean;
}

interface NativeTool {
  name: string;
  description: string;
  deferred: boolean;
  input_schema: any;
}

interface ToolItem {
  name: string;
  fullName: string;
  description: string;
  deferred: boolean;
  inputSchema: any;
  source: 'native' | 'mcp';
  serverName?: string;
}

const toolDetails: Record<string, {
  idempotent: boolean;
  returns: string;
  errorHandling: string;
  executionMode: string;
  category: string;
}> = {
  // File Operations
  read_file: {
    idempotent: true,
    returns: '文件内容，带行号。大文件可用 offset/limit 分段读取。',
    errorHandling: '文件不存在返回错误信息；权限不足返回错误信息。',
    executionMode: 'parallel',
    category: '文件操作',
  },
  outline_file: {
    idempotent: true,
    returns: '文件结构大纲：Python 类/函数、Markdown 标题、TS/JS 声明，均带行号范围。',
    errorHandling: '文件不存在或不支持的扩展名返回错误信息。',
    executionMode: 'parallel',
    category: '文件操作',
  },
  write_file: {
    idempotent: false,
    returns: '写入成功的确认信息。',
    errorHandling: '父目录不存在或权限不足返回错误。',
    executionMode: 'sequential',
    category: '文件操作',
  },
  edit_file: {
    idempotent: false,
    returns: '替换成功的确认信息，显示修改前后的差异。',
    errorHandling: 'old_string 未找到或匹配多处时返回错误。',
    executionMode: 'sequential',
    category: '文件操作',
  },
  list_files: {
    idempotent: true,
    returns: '匹配的文件路径列表。',
    errorHandling: '无匹配时返回空列表。',
    executionMode: 'parallel',
    category: '文件操作',
  },
  grep_search: {
    idempotent: true,
    returns: '匹配的行及文件路径、行号。',
    errorHandling: '无匹配或正则无效时返回空结果。',
    executionMode: 'parallel',
    category: '文件操作',
  },
  // Shell
  run_shell: {
    idempotent: false,
    returns: '命令的 stdout 输出。background=true 时返回 job_id。',
    errorHandling: '非零退出码返回 stderr；超时返回部分输出和错误信息。',
    executionMode: 'sequential',
    category: 'Shell',
  },
  shell_status: {
    idempotent: true,
    returns: '后台任务的状态和输出。省略 job_id 时返回所有任务列表。',
    errorHandling: 'job_id 不存在时返回错误。',
    executionMode: 'parallel',
    category: 'Shell',
  },
  // Context
  compact_context: {
    idempotent: false,
    returns: '压缩后的上下文摘要。',
    errorHandling: '压缩失败时保留原始上下文。',
    executionMode: 'sequential',
    category: '上下文管理',
  },
  context_restore: {
    idempotent: true,
    returns: '恢复的原始工具结果内容。',
    errorHandling: 'key 不存在时返回错误。',
    executionMode: 'sequential',
    category: '上下文管理',
  },
  search_history: {
    idempotent: true,
    returns: '匹配的历史消息列表。',
    errorHandling: '无匹配时返回空列表。',
    executionMode: 'parallel',
    category: '上下文管理',
  },
  // Wiki & Memory
  remember: {
    idempotent: false,
    returns: 'JSON：{action: created|merged|appended, path, message}。',
    errorHandling: '参数非法或写入失败时返回 {action: "error"}。',
    executionMode: 'sequential',
    category: 'Wiki & 记忆',
  },
  list_session_notes: {
    idempotent: true,
    returns: '会话笔记列表，包含最新笔记内容。',
    errorHandling: '无笔记时返回空列表。',
    executionMode: 'parallel',
    category: 'Wiki & 记忆',
  },
  git_diff_before_last_compress: {
    idempotent: true,
    returns: '压缩前修改的文件列表（路径、状态、行数）。',
    errorHandling: '无压缩历史时返回提示。',
    executionMode: 'parallel',
    category: '上下文管理',
  },
  git_diff_session: {
    idempotent: true,
    returns: '指定会话修改的文件列表（路径、状态、行数）。',
    errorHandling: '会话不存在时返回错误。',
    executionMode: 'parallel',
    category: '上下文管理',
  },
  // Skills
  skill_create: {
    idempotent: false,
    returns: '技能创建成功的确认信息。',
    errorHandling: '创建失败时返回错误信息。',
    executionMode: 'sequential',
    category: '技能',
  },
  // Plan
  plan_propose: {
    idempotent: false,
    returns: '计划创建成功的确认信息。',
    errorHandling: '创建失败时返回错误信息。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_status: {
    idempotent: true,
    returns: '计划状态和任务列表。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'parallel',
    category: '计划管理',
  },
  plan_list: {
    idempotent: true,
    returns: '计划列表。',
    errorHandling: '无计划时返回空列表。',
    executionMode: 'parallel',
    category: '计划管理',
  },
  plan_update: {
    idempotent: false,
    returns: '计划更新成功的确认信息。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_task_start: {
    idempotent: false,
    returns: '任务状态更新为 in-progress 的确认信息。',
    errorHandling: '任务不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_task_done: {
    idempotent: false,
    returns: '任务标记为完成的确认信息。',
    errorHandling: '任务不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_task_failed: {
    idempotent: false,
    returns: '任务标记为失败的确认信息。',
    errorHandling: '任务不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_complete: {
    idempotent: false,
    returns: '计划标记为完成的确认信息。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_add_artifact: {
    idempotent: false,
    returns: '产物添加成功的确认信息。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_read_artifact: {
    idempotent: true,
    returns: '产物内容。',
    errorHandling: '产物不存在时返回错误。',
    executionMode: 'parallel',
    category: '计划管理',
  },
  plan_archive: {
    idempotent: false,
    returns: '计划归档成功的确认信息。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_explore: {
    idempotent: true,
    returns: '探索主题的提示词。',
    errorHandling: '无错误。',
    executionMode: 'parallel',
    category: '计划管理',
  },
  plan_save_explore: {
    idempotent: false,
    returns: '探索结果保存成功的确认信息。',
    errorHandling: '保存失败时返回错误信息。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_continue: {
    idempotent: true,
    returns: '下一个任务的执行提示。',
    errorHandling: '计划不存在或无待执行任务时返回错误。',
    executionMode: 'parallel',
    category: '计划管理',
  },
  plan_retry: {
    idempotent: true,
    returns: '重试任务的执行提示。',
    errorHandling: '任务不存在时返回错误。',
    executionMode: 'parallel',
    category: '计划管理',
  },
  plan_recall: {
    idempotent: true,
    returns: '匹配的计划列表。',
    errorHandling: '无匹配时返回空列表。',
    executionMode: 'parallel',
    category: '计划管理',
  },
  plan_abandon: {
    idempotent: false,
    returns: '计划放弃成功的确认信息。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_reopen: {
    idempotent: false,
    returns: '计划重新打开成功的确认信息。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_check_expired: {
    idempotent: true,
    returns: '过期计划列表。',
    errorHandling: '无过期计划时返回空列表。',
    executionMode: 'parallel',
    category: '计划管理',
  },
  plan_pause: {
    idempotent: false,
    returns: '计划暂停成功的确认信息。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_resume: {
    idempotent: false,
    returns: '计划恢复成功的确认信息。',
    errorHandling: '计划不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_skip: {
    idempotent: false,
    returns: '任务跳过成功的确认信息。',
    errorHandling: '任务不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_redo: {
    idempotent: false,
    returns: '任务重置成功的确认信息。',
    errorHandling: '任务不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  plan_rollback: {
    idempotent: false,
    returns: '计划回滚成功的确认信息。',
    errorHandling: '任务不存在时返回错误。',
    executionMode: 'sequential',
    category: '计划管理',
  },
  // Mode
  enter_plan_mode: {
    idempotent: false,
    returns: '进入规划模式的确认信息。',
    errorHandling: '已在规划模式时返回提示。',
    executionMode: 'sequential',
    category: '模式切换',
  },
  exit_plan_mode: {
    idempotent: false,
    returns: '退出规划模式的确认信息。',
    errorHandling: '未在规划模式时返回提示。',
    executionMode: 'sequential',
    category: '模式切换',
  },
  // Agent
  agent: {
    idempotent: false,
    returns: '子智能体的执行结果。',
    errorHandling: '子智能体执行失败时返回错误信息。',
    executionMode: 'sequential',
    category: '智能体',
  },
  tool_search: {
    idempotent: true,
    returns: '匹配的延迟加载工具列表，包含完整 schema。',
    errorHandling: '无匹配时返回空列表。',
    executionMode: 'sequential',
    category: '智能体',
  },
  // User
  ask_user: {
    idempotent: true,
    returns: '用户的回答。',
    errorHandling: '用户取消时返回取消信息。',
    executionMode: 'sequential',
    category: '用户交互',
  },
  todolist: {
    idempotent: false,
    returns: '任务清单操作结果。',
    errorHandling: '操作失败时返回错误信息。',
    executionMode: 'sequential',
    category: '用户交互',
  },
};

export default function ToolsPage() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [serverTools, setServerTools] = useState<McpServerWithTools[]>([]);
  const [nativeTools, setNativeTools] = useState<NativeTool[]>([]);
  const [mcpLoading, setMcpLoading] = useState(false);
  const [selectedTool, setSelectedTool] = useState<ToolItem | null>(null);
  const [expandedServers, setExpandedServers] = useState<Set<string>>(new Set(['native']));

  const loadNativeTools = async () => {
    try {
      const response = await fetch('/api/tools');
      const data = await response.json();
      setNativeTools(data);
    } catch (err) {
      console.error('Failed to load native tools:', err);
    }
  };

  const loadMcpData = async () => {
    setMcpLoading(true);
    try {
      // 获取所有配置的服务器状态（包括未连接的）
      const statusResponse = await fetch('/api/mcp/status');
      const statusData = await statusResponse.json();
      
      // 获取已连接服务器的工具列表
      const toolsResponse = await fetch('/api/mcp/tools');
      const toolsData = await toolsResponse.json();
      
      // 合并服务器状态和工具列表
      const allServers: McpServerWithTools[] = statusData.map((server: any) => {
        const toolsInfo = toolsData.find((t: any) => t.server === server.name);
        return {
          server: server.name,
          tools: toolsInfo?.tools || [],
          tool_count: server.tool_count || 0,
          enabled: server.enabled,
        };
      });
      
      setServerTools(allServers);
      
      const [serversData] = await Promise.all([
        fetchMcpServers(),
      ]);
      setServers(serversData);
    } catch (err) {
      console.error('Failed to load MCP data:', err);
    } finally {
      setMcpLoading(false);
    }
  };

  useEffect(() => {
    loadNativeTools();
    loadMcpData();
  }, []);

  const toggleServerExpanded = (serverName: string) => {
    const newExpanded = new Set(expandedServers);
    if (newExpanded.has(serverName)) {
      newExpanded.delete(serverName);
    } else {
      newExpanded.add(serverName);
    }
    setExpandedServers(newExpanded);
  };

  const sidebarContent = (
    <div className="flex flex-col h-full">
      <div className="p-3 border-b border-gray-200">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-gray-900">All Tools</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {nativeTools.length} native • {serverTools.reduce((sum, s) => sum + s.tool_count, 0)} mcp
              {mcpLoading && ' • Loading...'}
            </p>
          </div>
          <button
            onClick={() => { loadNativeTools(); loadMcpData(); }}
            className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors"
            title="Refresh"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${mcpLoading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        {/* Native Tools Section */}
        <div className="border-b border-gray-200">
          <div
            className="px-3 py-2 bg-gray-50 flex items-center gap-1.5 cursor-pointer hover:bg-gray-100"
            onClick={() => toggleServerExpanded('native')}
          >
            <ChevronRight className={`w-3 h-3 text-gray-400 transition-transform ${expandedServers.has('native') ? 'rotate-90' : ''}`} />
            <Cpu className="w-3.5 h-3.5 text-orange-500" />
            <span className="text-xs font-semibold text-gray-700">Native</span>
            <span className="text-xs text-gray-500">({nativeTools.length})</span>
          </div>
          {expandedServers.has('native') && (
            <div className="divide-y divide-gray-50">
              {nativeTools.map(tool => {
                const toolItem: ToolItem = {
                  name: tool.name,
                  fullName: tool.name,
                  description: tool.description,
                  deferred: tool.deferred,
                  inputSchema: tool.input_schema,
                  source: 'native',
                };
                const isPlanTool = tool.name.startsWith('plan_') || tool.name === 'enter_plan_mode' || tool.name === 'exit_plan_mode';
                return (
                  <div
                    key={tool.name}
                    className={`px-3 py-1.5 cursor-pointer hover:bg-gray-50 ${
                      selectedTool?.fullName === tool.name ? 'bg-indigo-50 border-l-2 border-indigo-500' : ''
                    }`}
                    onClick={() => setSelectedTool(toolItem)}
                  >
                    <div className="flex items-center gap-1.5">
                      <h3 className="text-xs font-medium text-gray-900 truncate flex-1">{tool.name}</h3>
                      {isPlanTool && (
                        <span className="text-[9px] px-1 py-0.5 bg-indigo-100 text-indigo-700 rounded">P</span>
                      )}
                      {tool.deferred && (
                        <span className="text-[9px] px-1 py-0.5 bg-purple-100 text-purple-700 rounded">D</span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* MCP Servers Section */}
        <div>
          <div className="px-3 py-2 bg-gray-50 flex items-center gap-1.5">
            <Server className="w-3.5 h-3.5 text-green-500" />
            <span className="text-xs font-semibold text-gray-700">MCP Servers</span>
            <span className="text-xs text-gray-500">({serverTools.length})</span>
          </div>
          <div className="divide-y divide-gray-100">
            {serverTools.map(st => {
              const isExpanded = expandedServers.has(st.server);
              const isEnabled = st.enabled !== false; // 默认启用，除非明确为 false
              return (
                <div key={st.server} className={!isEnabled ? 'opacity-50' : ''}>
                  <div
                    className="px-3 py-2 cursor-pointer hover:bg-gray-50 flex items-center gap-1.5"
                    onClick={() => toggleServerExpanded(st.server)}
                  >
                    <ChevronRight className={`w-3 h-3 text-gray-400 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                    <Server className={`w-3.5 h-3.5 ${isEnabled ? 'text-green-500' : 'text-gray-400'}`} />
                    <span className="text-xs font-medium text-gray-900 truncate flex-1">{st.server}</span>
                    <span className="text-xs text-gray-500">{st.tool_count}</span>
                    <button
                      onClick={async (e) => {
                        e.stopPropagation();
                        const endpoint = isEnabled ? `/api/mcp/${st.server}/disable` : `/api/mcp/${st.server}/enable`;
                        await fetch(endpoint, { method: 'POST' });
                        await loadMcpData(); // 重新加载数据
                      }}
                      className={`p-0.5 rounded transition-colors ${
                        isEnabled
                          ? 'bg-green-100 text-green-700 hover:bg-green-200'
                          : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
                      }`}
                      title={isEnabled ? 'Disable' : 'Enable'}
                    >
                      <Power className="w-2.5 h-2.5" />
                    </button>
                  </div>
                  {isExpanded && (
                    <div className="bg-gray-50 divide-y divide-gray-100">
                      {st.tools.length === 0 ? (
                        <div className="pl-8 pr-3 py-2 text-xs text-gray-500">
                          {isEnabled ? 'No tools available' : 'Server disabled - click power button to enable'}
                        </div>
                      ) : (
                        st.tools.map(t => {
                          const toolItem: ToolItem = {
                            name: t.name,
                            fullName: t.full_name,
                            description: t.description,
                            deferred: false,
                            inputSchema: t.input_schema,
                            source: 'mcp',
                            serverName: st.server,
                          };
                          return (
                            <div
                              key={t.full_name}
                              className={`pl-8 pr-3 py-1.5 cursor-pointer hover:bg-gray-100 ${
                                selectedTool?.fullName === t.full_name ? 'bg-indigo-50 border-l-2 border-indigo-500' : ''
                              }`}
                              onClick={() => setSelectedTool(toolItem)}
                            >
                              <h3 className="text-xs font-medium text-gray-900 truncate">{t.name}</h3>
                            </div>
                          );
                        })
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );

  const details = selectedTool ? toolDetails[selectedTool.name] : null;

  return (
    <PageLayout sidebarContent={sidebarContent}>
      <div className="h-full flex flex-col bg-white">
        <div className="flex-1 overflow-y-auto p-6">
          {selectedTool ? (
            <div className="max-w-3xl">
              <div className="flex items-center gap-2 mb-4">
                {selectedTool.source === 'native' ? (
                  <Cpu className="w-6 h-6 text-orange-500" />
                ) : (
                  <Server className="w-6 h-6 text-green-500" />
                )}
                <h2 className="text-xl font-bold text-gray-900">{selectedTool.name}</h2>
                <span className={`px-2 py-0.5 text-xs rounded ${
                  selectedTool.source === 'native'
                    ? 'bg-orange-100 text-orange-700'
                    : 'bg-green-100 text-green-700'
                }`}>
                  {selectedTool.source === 'native' ? 'Native' : selectedTool.serverName}
                </span>
                {selectedTool.deferred && (
                  <span className="px-2 py-0.5 text-xs bg-purple-100 text-purple-700 rounded">
                    Deferred
                  </span>
                )}
              </div>

              {selectedTool.description && (
                <p className="text-sm text-gray-600 mb-6">{selectedTool.description}</p>
              )}

              {selectedTool.deferred && (
                <div className="mb-6 p-4 bg-purple-50 rounded-lg border border-purple-200">
                  <div className="flex items-center gap-2 mb-2">
                    <Zap className="w-4 h-4 text-purple-600" />
                    <h3 className="text-sm font-semibold text-purple-900">延迟加载工具</h3>
                  </div>
                  <p className="text-xs text-purple-800">
                    此工具初始时不发送给模型，以减少 token 消耗。模型可通过调用 <code className="px-1 py-0.5 bg-purple-100 rounded font-mono">tool_search</code> 工具搜索并激活它。激活后，该工具会加入后续请求的 tools 列表。
                  </p>
                </div>
              )}

              {details && (
                <div className="mb-6 space-y-4">
                  <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                    <h3 className="text-sm font-semibold text-gray-700 mb-2">分类</h3>
                    <p className="text-xs text-gray-600">{details.category}</p>
                  </div>
                  <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                    <h3 className="text-sm font-semibold text-gray-700 mb-2">幂等性</h3>
                    <p className="text-xs text-gray-600">
                      {details.idempotent ? (
                        <span className="text-green-600">✓ 幂等 - 可安全重试，不会产生副作用</span>
                      ) : (
                        <span className="text-orange-600">✗ 非幂等 - 有副作用，重复调用可能产生不同结果</span>
                      )}
                    </p>
                  </div>
                  <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                    <h3 className="text-sm font-semibold text-gray-700 mb-2">返回值</h3>
                    <p className="text-xs text-gray-600">{details.returns}</p>
                  </div>
                  <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                    <h3 className="text-sm font-semibold text-gray-700 mb-2">错误处理</h3>
                    <p className="text-xs text-gray-600">{details.errorHandling}</p>
                  </div>
                  <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                    <h3 className="text-sm font-semibold text-gray-700 mb-2">执行模式</h3>
                    <p className="text-xs text-gray-600">
                      {details.executionMode === 'parallel' ? (
                        <span className="text-indigo-600">parallel - 可与其他工具并行执行</span>
                      ) : (
                        <span className="text-purple-600">sequential - 必须顺序执行</span>
                      )}
                    </p>
                  </div>
                </div>
              )}

              {selectedTool.source === 'mcp' && selectedTool.serverName && (
                <div className="mb-6 p-4 bg-gray-50 rounded-lg border border-gray-200">
                  <h3 className="text-sm font-semibold text-gray-700 mb-2">Server Info</h3>
                  {(() => {
                    const server = servers.find(s => s.name === selectedTool.serverName);
                    const serverTool = serverTools.find(s => s.server === selectedTool.serverName);
                    if (!server) return null;
                    const isEnabled = serverTool?.enabled !== false;
                    return (
                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <span className="text-xs text-gray-500">Status</span>
                          <button
                            onClick={async () => {
                              const endpoint = isEnabled ? `/api/mcp/${server.name}/disable` : `/api/mcp/${server.name}/enable`;
                              await fetch(endpoint, { method: 'POST' });
                              await loadMcpData();
                            }}
                            className={`flex items-center gap-1 px-2 py-1 text-xs rounded transition-colors ${
                              isEnabled
                                ? 'bg-green-100 text-green-700 hover:bg-green-200'
                                : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
                            }`}
                          >
                            <Power className="w-3 h-3" />
                            {isEnabled ? 'Enabled' : 'Disabled'}
                          </button>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-xs text-gray-500">Command</span>
                          <span className="text-xs font-mono text-gray-700">{server.command} {server.args.join(' ')}</span>
                        </div>
                      </div>
                    );
                  })()}
                </div>
              )}

              {selectedTool.inputSchema && Object.keys(selectedTool.inputSchema.properties || {}).length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-gray-700 mb-3">Parameters</h3>
                  <div className="space-y-3">
                    {Object.entries(selectedTool.inputSchema.properties).map(([paramName, paramDef]: [string, any]) => (
                      <div key={paramName} className="p-3 bg-gray-50 rounded-lg border border-gray-200">
                        <div className="flex items-center gap-2 mb-1">
                          <span className="font-mono text-sm font-semibold text-gray-900">{paramName}</span>
                          <span className="text-xs px-1.5 py-0.5 bg-indigo-100 text-indigo-700 rounded">{paramDef.type || 'any'}</span>
                          {selectedTool.inputSchema.required?.includes(paramName) && (
                            <span className="text-xs px-1.5 py-0.5 bg-red-100 text-red-700 rounded">required</span>
                          )}
                        </div>
                        {paramDef.description && (
                          <p className="text-xs text-gray-600">{paramDef.description}</p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-center justify-center h-full text-gray-400">
              <div className="text-center">
                <Wrench className="w-12 h-12 mx-auto mb-4 opacity-50" />
                <p>Select a tool to view details</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </PageLayout>
  );
}

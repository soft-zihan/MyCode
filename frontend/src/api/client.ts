// API client for MyCode backend
import type { components, paths } from './schema';

const API_BASE = '/api';

export interface Session {
  id: string;
  startTime: string;
  model: string;
  cwd: string;
  messageCount: number;
  name?: string;
  plan_slug?: string;
}

export interface SessionDetail {
  id: string;
  messages: any[];
  metadata: Session;
}

export interface Skill {
  name: string;
  description: string;
  skill_dir: string;
  model?: string;
  source: string;
}

export interface SkillDetail extends Skill {
  prompt_template: string;
  raw_content: string;
  content?: string;
}

export interface WorkspaceNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size?: number;
  children?: WorkspaceNode[];
}

export interface Agent {
  name: string;
  description: string;
  model_ref: string;
  is_custom: boolean;
  has_override: boolean;
  allowed_tools?: string[];
  has_system_prompt: boolean;
  category: 'primary' | 'sub' | 'hidden' | 'custom';
}

export interface AgentDetail extends Agent {
  tools_count: number;
  tools: string[];
  system_prompt_preview: string;
  custom_config: any;
  category: 'primary' | 'sub' | 'hidden' | 'custom';
}

export interface Endpoint {
  id: string;
  model: string;
  base_url: string;
  has_api_key: boolean;
  protocol: 'openai';
}

export interface Project {
  cwd: string;
  name?: string;
  createdAt: string;
  updatedAt: string;
}

export interface PrimaryEndpoint {
  model: string;
  base_url: string;
  has_api_key: boolean;
}

export async function fetchSessions(): Promise<Session[]> {
  const res = await fetch(`${API_BASE}/sessions`);
  if (!res.ok) throw new Error('Failed to fetch sessions');
  const data = await res.json();
  console.log('[API] fetchSessions:', data.length, 'sessions');
  return data;
}

export async function fetchSession(id: string): Promise<SessionDetail> {
  console.log('[API] fetchSession:', id);
  const res = await fetch(`${API_BASE}/sessions/${id}`);
  if (!res.ok) throw new Error('Failed to fetch session');
  return res.json();
}

export async function deleteSession(id: string): Promise<void> {
  console.log('[API] deleteSession:', id);
  const res = await fetch(`${API_BASE}/sessions/${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete session');
}

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`${API_BASE}/projects`);
  if (!res.ok) throw new Error('Failed to fetch projects');
  return res.json();
}

export async function registerProject(cwd: string, name?: string): Promise<Project> {
  const res = await fetch(`${API_BASE}/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cwd, name })
  });
  if (!res.ok) throw new Error('Failed to register project');
  return res.json();
}

export async function deleteProject(cwd: string): Promise<void> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(cwd)}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete project');
}

// U11 worktree（契约单源：类型来自 openapi 生成的 schema.d.ts——U12 首个消费点）
export type WorktreeEntry = components['schemas']['WorktreeEntry'];

export interface WorktreeListResult {
  worktrees: WorktreeEntry[];
  git: boolean;   // false = 非 git 项目（无 worktree 能力，UI 隐藏入口）
}

export class WorktreeRemoveError extends Error {
  forceRequired: boolean;
  constructor(message: string, forceRequired: boolean) {
    super(message);
    this.forceRequired = forceRequired;
  }
}

export async function fetchWorktrees(cwd: string): Promise<WorktreeListResult> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(cwd)}/worktrees`);
  if (!res.ok) throw new Error('Failed to fetch worktrees');
  return res.json();
}

export async function createWorktree(cwd: string, name?: string): Promise<WorktreeEntry> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(cwd)}/worktrees`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name ?? null })
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data?.detail || 'Failed to create worktree');
  }
  return res.json();
}

export async function removeWorktree(directory: string, force = false): Promise<void> {
  const res = await fetch(
    `${API_BASE}/worktrees/${encodeURIComponent(directory)}${force ? '?force=true' : ''}`,
    { method: 'DELETE' }
  );
  if (res.status === 409) {
    const data = await res.json().catch(() => ({}));
    throw new WorktreeRemoveError(data?.detail?.message || 'Worktree has uncommitted changes', true);
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data?.detail || 'Failed to remove worktree');
  }
}

export async function updateSessionName(id: string, name: string): Promise<void> {
  console.log('[API] updateSessionName:', { id, name });
  const res = await fetch(`${API_BASE}/sessions/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name })
  });
  if (!res.ok) throw new Error('Failed to update session name');
  console.log('[API] updateSessionName done');
}

export async function fetchSkills(): Promise<Skill[]> {
  const res = await fetch(`${API_BASE}/skills`);
  if (!res.ok) throw new Error('Failed to fetch skills');
  return res.json();
}

export async function fetchSkill(name: string): Promise<Skill> {
  const res = await fetch(`${API_BASE}/skills/${name}`);
  if (!res.ok) throw new Error('Failed to fetch skill');
  return res.json();
}

export async function fetchSkillStats(): Promise<any> {
  const res = await fetch(`${API_BASE}/skills/stats`);
  if (!res.ok) throw new Error('Failed to fetch skill stats');
  return res.json();
}

export async function fetchSkillEvolutionReport(): Promise<any> {
  const res = await fetch(`${API_BASE}/skill-evolution/report`);
  if (!res.ok) throw new Error('Failed to fetch evolution report');
  return res.json();
}

export async function fetchSkillEvolutionProvenance(): Promise<any[]> {
  const res = await fetch(`${API_BASE}/skill-evolution/provenance`);
  if (!res.ok) throw new Error('Failed to fetch evolution provenance');
  return res.json();
}

export async function deleteSkill(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/skills/${name}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete skill');
}

export async function fetchNativeTools(): Promise<any[]> {
  const res = await fetch(`${API_BASE}/tools/native`);
  if (!res.ok) throw new Error('Failed to fetch native tools');
  return res.json();
}

export interface DirectoryItem {
  name: string;
  path: string;
}

export interface DirectoryList {
  current: string;
  parent: string | null;
  directories: DirectoryItem[];
}

export async function fetchDirectories(path?: string): Promise<DirectoryList> {
  const url = path ? `${API_BASE}/directories?path=${encodeURIComponent(path)}` : `${API_BASE}/directories`;
  const res = await fetch(url);
  if (!res.ok) throw new Error('Failed to fetch directories');
  return res.json();
}
export async function fetchWorkspaceTree(cwd?: string): Promise<WorkspaceNode> {
  const url = cwd ? `${API_BASE}/workspace/tree?cwd=${encodeURIComponent(cwd)}` : `${API_BASE}/workspace/tree`;
  const res = await fetch(url);
  if (!res.ok) throw new Error('Failed to fetch workspace tree');
  return res.json();
}

export async function deleteWorkspaceFile(path: string, cwd?: string): Promise<{ success: boolean; message: string }> {
  const url = cwd 
    ? `${API_BASE}/workspace/file?path=${encodeURIComponent(path)}&cwd=${encodeURIComponent(cwd)}`
    : `${API_BASE}/workspace/file?path=${encodeURIComponent(path)}`;
  const res = await fetch(url, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Delete failed' }));
    throw new Error(err.detail || 'Delete failed');
  }
  return res.json();
}

export async function createWorkspaceFile(path: string, cwd?: string): Promise<{ success: boolean; path: string }> {
  const res = await fetch(`${API_BASE}/workspace/create`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, cwd }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Create failed' }));
    throw new Error(err.detail || 'Create failed');
  }
  return res.json();
}

export async function renameWorkspaceFile(oldPath: string, newName: string, cwd?: string): Promise<{ success: boolean; new_path: string }> {
  const res = await fetch(`${API_BASE}/workspace/rename`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_path: oldPath, new_name: newName, cwd }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Rename failed' }));
    throw new Error(err.detail || 'Rename failed');
  }
  return res.json();
}

export async function moveWorkspaceFile(sourcePath: string, targetPath: string, cwd?: string): Promise<{ success: boolean; source: string; target: string }> {
  const res = await fetch(`${API_BASE}/workspace/move`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_path: sourcePath, target_path: targetPath, cwd }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Move failed' }));
    throw new Error(err.detail || 'Move failed');
  }
  return res.json();
}

export async function fetchWorkspaceFile(path: string, cwd?: string): Promise<{ path: string; content: string; size: number; frontmatter?: Record<string, any> }> {
  const url = cwd
    ? `${API_BASE}/workspace/file?path=${encodeURIComponent(path)}&cwd=${encodeURIComponent(cwd)}`
    : `${API_BASE}/workspace/file?path=${encodeURIComponent(path)}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error('Failed to fetch file');
  return res.json();
}

export async function writeWorkspaceFile(path: string, content: string, cwd?: string): Promise<{ success: boolean; path: string; size: number }> {
  const res = await fetch(`${API_BASE}/workspace/file`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content, cwd }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Write failed' }));
    throw new Error(err.detail || 'Write failed');
  }
  return res.json();
}

export async function fetchAgents(): Promise<Agent[]> {
  const res = await fetch(`${API_BASE}/agents`);
  if (!res.ok) throw new Error('Failed to fetch agents');
  return res.json();
}

export async function fetchAgent(name: string): Promise<AgentDetail> {
  const res = await fetch(`${API_BASE}/agents/${name}`);
  if (!res.ok) throw new Error('Failed to fetch agent');
  return res.json();
}

export async function fetchEndpoints(): Promise<Endpoint[]> {
  const res = await fetch(`${API_BASE}/endpoints`);
  if (!res.ok) throw new Error('Failed to fetch endpoints');
  return res.json();
}

export async function fetchPrimaryEndpoint(): Promise<PrimaryEndpoint> {
  const res = await fetch(`${API_BASE}/endpoints/primary`);
  if (!res.ok) throw new Error('Failed to fetch primary endpoint');
  return res.json();
}

// ── MCP ──────────────────────────────────────────────────────────────────────

export interface McpServer {
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
}

export interface McpServerStatus {
  name: string;
  enabled: boolean;
  tool_count: number;
  type: 'stdio' | 'http';
}

export async function fetchMcpServers(): Promise<McpServer[]> {
  const res = await fetch(`${API_BASE}/mcp`);
  if (!res.ok) throw new Error('Failed to fetch MCP servers');
  return res.json();
}

export async function fetchMcpStatus(): Promise<McpServerStatus[]> {
  const res = await fetch(`${API_BASE}/mcp/status`);
  if (!res.ok) throw new Error('Failed to fetch MCP status');
  return res.json();
}

export async function enableMcpServer(serverName: string): Promise<{ success: boolean }> {
  const res = await fetch(`${API_BASE}/mcp/${encodeURIComponent(serverName)}/enable`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error('Failed to enable MCP server');
  return res.json();
}

export async function disableMcpServer(serverName: string): Promise<{ success: boolean }> {
  const res = await fetch(`${API_BASE}/mcp/${encodeURIComponent(serverName)}/disable`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error('Failed to disable MCP server');
  return res.json();
}

// ── Config (JSON) ────────────────────────────────────────────────────────────

export const DEFAULT_CONTEXT_WINDOW = 1_000_000;
export interface ModelEndpointConfig {
  model: string;
  base_url: string;
  api_key: string;
  provider_name?: string;
  context_window?: number;
  thinking?: boolean | null;  // null=跟随模型默认
}

export interface AgentRoutingConfig {
  [agentType: string]: string;
}

export interface AppConfig {
  endpoints: Record<string, ModelEndpointConfig>;
  routing: AgentRoutingConfig;
}

export async function fetchConfig(): Promise<AppConfig> {
  const res = await fetch(`${API_BASE}/config`);
  if (!res.ok) throw new Error('Failed to fetch config');
  return res.json();
}

export async function saveConfig(config: AppConfig): Promise<void> {
  const res = await fetch(`${API_BASE}/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  if (!res.ok) throw new Error('Failed to save config');
}

export interface PlanStrategies {
  grill_spec: string;
  tasks: string;
}

export async function fetchPlanStrategies(): Promise<PlanStrategies> {
  const res = await fetch(`${API_BASE}/config/plan-strategies`);
  if (!res.ok) throw new Error('Failed to fetch plan strategies');
  return res.json();
}

export async function savePlanStrategies(strategies: PlanStrategies): Promise<void> {
  const res = await fetch(`${API_BASE}/config/plan-strategies`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(strategies),
  });
  if (!res.ok) throw new Error('Failed to save plan strategies');
}

// ── Prompt APIs ─────────────────────────────────────────────────────────────

export interface PromptInfo {
  id: string;
  name: string;
  description: string;
  path: string;
}

export interface PromptContent {
  id: string;
  content: string;
  path: string;
}

export async function fetchPrompts(): Promise<PromptInfo[]> {
  const res = await fetch(`${API_BASE}/prompts`);
  if (!res.ok) throw new Error('Failed to fetch prompts');
  return res.json();
}

export async function fetchPrompt(promptId: string): Promise<PromptContent> {
  const res = await fetch(`${API_BASE}/prompts/${promptId}`);
  if (!res.ok) throw new Error('Failed to fetch prompt');
  return res.json();
}

export async function savePrompt(promptId: string, content: string): Promise<void> {
  const res = await fetch(`${API_BASE}/prompts/${promptId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) throw new Error('Failed to save prompt');
}

// ── Session Control ──────────────────────────────────────────────────────────

export async function abortSession(sessionId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/abort`, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to abort session');
}

export async function compactSession(sessionId: string): Promise<{ success: boolean; folded?: boolean; message: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/compact`, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to compact session');
  return res.json();
}

export interface SessionStats {
  /** 最近一次模型调用（per-call） */
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  cache_hit_rate: number;
  /** 会话累计 */
  total_input_tokens: number;
  total_output_tokens: number;
  total_cached_tokens: number;
  context_window: number;
  effective_window: number;
  estimated_context_tokens: number;
}

export interface SessionSummary {
  metadata: { id: string; name: string; cwd: string };
  projections: { title?: string; cwd?: string; running?: boolean; updated_at?: string; plan_slug?: string };
  stats: SessionStats;
  breakdown: TokenBreakdown;
  permission_mode: string;
}

export async function fetchSessionSummary(sessionId: string): Promise<SessionSummary> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/summary`);
  if (!res.ok) {
    const err = new Error(`Failed to fetch session summary: ${res.status}`) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export async function updatePermissionMode(sessionId: string, mode: string): Promise<{ success: boolean; permission_mode: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/permission-mode`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  });
  if (!res.ok) throw new Error('Failed to update permission mode');
  return res.json();
}

export interface SteerResult {
  success: boolean;
  message: string;
}

export async function steerSession(sessionId: string, message: string, contextFiles?: string[]): Promise<SteerResult> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/steer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      context_files: contextFiles && contextFiles.length > 0 ? contextFiles : undefined,
    }),
  });
  if (!res.ok) throw new Error(`Failed to steer session: ${res.status}`);
   return res.json();
}

/** U3a：把正在前台阻塞的子代理转为后台（run 不中断，完成后经 subagent/completed 通知） */
export async function backgroundSubagent(sessionId: string, subSessionId: string): Promise<SteerResult> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/subagents/${subSessionId}/background`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(`Failed to background subagent: ${res.status}`);
  return res.json();
}

/** U4：硬中止后台子代理（软 abort + 硬 cancel 双通道，子会话仍可续跑） */
export async function cancelSubagent(sessionId: string, subSessionId: string): Promise<SteerResult> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/subagents/${subSessionId}/cancel`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(`Failed to cancel subagent: ${res.status}`);
  return res.json();
}

export async function forkSession(
  sessionId: string, 
  options?: { at_seq?: number; keep_user_messages?: number }
): Promise<{ new_session_id: string; fork_name: string; message: string }> {
  console.log('[API] forkSession:', sessionId, options);
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/fork`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options || {}),
  });
  if (!res.ok) throw new Error('Failed to fork session');
  const data = await res.json();
  console.log('[API] forkSession result:', data);
  return data;
}

export interface PermissionRequest {
  rpc_id: string;
  request_id: string;
  command: string;
  tool_name: string;
  message?: string;
  sub_agent_id?: string;
}

export async function respondToPermission(sessionId: string, requestId: string, allowed: boolean, feedback?: string, choice?: string): Promise<void> {
  const res = await fetch(`${API_BASE}/events/respond`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rpc_id: requestId, allowed, session_id: sessionId, feedback: feedback || '', choice: choice || '' }),
  });
  if (!res.ok) throw new Error('Failed to respond to permission');
}

export async function getPlanDraftArtifacts(sessionId: string): Promise<{ success: boolean; message?: string; data?: { draft_dir: string; artifacts: Record<string, string> } }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan-draft/artifacts`);
  if (!res.ok) throw new Error('Failed to fetch plan draft artifacts');
  return res.json();
}

export async function updatePlanDraftArtifact(sessionId: string, filename: string, content: string): Promise<{ success: boolean; message?: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan-draft/artifacts`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, content }),
  });
  if (!res.ok) throw new Error('Failed to update plan draft artifact');
  return res.json();
}

export async function respondToQuestion(sessionId: string, requestId: string, answer: string): Promise<void> {
  const res = await fetch(`${API_BASE}/events/question-respond`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, answer, session_id: sessionId }),
  });
  if (!res.ok) throw new Error('Failed to respond to question');
}

// ── Task List（task_list store 的 REST 读写面，Plan 3b B1）────────────────────
//
// **这四个方法照着生成的 `paths` 类型写**，与文件里其余「手写 fetch + 手写内联返回类型」
// 的方法不同。理由：client.ts 此前只 import `components`，从不 import `paths`，于是
// URL 或响应形状与后端漂移时 `tsc --noEmit` 一声不响——故障落在运行时（404，或字段名
// 对不上导致静默解析成 undefined）。按 `paths[...]` 取类型把编译期耦合接回来了：后端改
// 路由或改 pydantic 模型 → 重新生成契约 → 这里先编译不过，而不是先在浏览器里坏掉。
// 既有方法不在本轮回改（那是另一件事）。

type TasksGetOp = paths['/api/tasks/{session_id}']['get'];
type TasksPostOp = paths['/api/tasks/{session_id}']['post'];
type TaskPatchOp = paths['/api/tasks/{session_id}/{task_id}']['patch'];
type TaskDeleteOp = paths['/api/tasks/{session_id}/{task_id}']['delete'];

/** GET 的响应体 `{ tasks, focus_id }`。focus_id 由后端用 task_store.find_focus 算。 */
export type TaskListResponse = TasksGetOp['responses'][200]['content']['application/json'];
/** 单个 task（TaskOut）。含服务端自有的 detail_origin_seq / started_seq：读得到、写不了。 */
export type Task = TaskListResponse['tasks'][number];
/** POST 请求体。刻意不含 detail_origin_seq / started_seq —— 后端 schema 不接受。 */
export type TaskCreateInput = TasksPostOp['requestBody']['content']['application/json'];
/** PATCH 请求体。给了的字段才改。 */
export type TaskUpdateInput = TaskPatchOp['requestBody']['content']['application/json'];
/** DELETE 响应体。未知 id 是 `success: false`（200），不是 HTTP 错误。 */
export type TaskDeleteResult = TaskDeleteOp['responses'][200]['content']['application/json'];
/** 五值状态词表（后端 TaskStatus Literal 生成的联合类型）。 */
export type TaskStatus = NonNullable<TaskUpdateInput['status']>;

/** 任务端点的失败：`_taskError` 把 HTTP 状态码挂在 Error 上（带 detail 时消息里没有码）。
 *  导出给调用方（useChat 的日志、TaskListPanel 的错误行）用同一个形状，
 *  免得每处各写一遍 `err as Error & { status?: number }`（F11）。 */
export type TaskApiError = Error & { status?: number };

async function _taskError(res: Response, fallback: string): Promise<Error> {
  const data = await res.json().catch(() => ({}));
  const detail = data?.detail;
  // 404 的 detail 是字符串；422 的是 pydantic 错误数组（含 msg 与合法词表），
  // 展平成一行，面板才报得出「status 只能是这五个」而不是「422」。
  const message = typeof detail === 'string'
    ? detail
    : Array.isArray(detail)
      ? detail.map((d: any) => d?.msg).filter(Boolean).join('; ')
      : '';
  const err = new Error(message ? `${fallback}: ${message}` : `${fallback} (${res.status})`) as TaskApiError;
  // 带 detail 时消息里**没有**状态码，调用方就没法把「路由 500（形状损坏）」与「404/422」
  // 区分开——两种故障在控制台里长得一模一样。把 status 挂在 Error 上，日志才报得出状态码。
  err.status = res.status;
  return err;
}

/** 读会话的任务清单 + 后端算出的焦点条 id（前端不自己推导焦点）。 */
export async function fetchTasks(sessionId: string): Promise<TaskListResponse> {
  const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(sessionId)}`);
  if (!res.ok) throw await _taskError(res, 'Failed to fetch tasks');
  return res.json();
}

/** 新建一条任务。`after_id` 省略 = 追加末尾，0 = 插到最前。 */
export async function createTask(sessionId: string, input: TaskCreateInput): Promise<Task> {
  const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(sessionId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await _taskError(res, 'Failed to create task');
  return res.json();
}

/**
 * 改一条任务，返回改完的样子。
 *
 * 改 `detail` 时后端会清空该条的 `detail_origin_seq`，于是下一次模型调用会重新披露
 * 新的 detail —— 这就是「UI 编辑回灌模型」的全部机制，前端不需要做任何额外的事
 * （改 content/acceptance/status/顺序由常驻摘要 S 每请求现读自动反映）。
 */
export async function updateTask(
  sessionId: string,
  taskId: number,
  input: TaskUpdateInput,
): Promise<Task> {
  const res = await fetch(
    `${API_BASE}/tasks/${encodeURIComponent(sessionId)}/${encodeURIComponent(taskId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    },
  );
  if (!res.ok) throw await _taskError(res, 'Failed to update task');
  return res.json();
}

/** 删一条任务。未知 id → `success: false`（调用方要看返回值，不能只看有没有抛）。 */
export async function deleteTask(sessionId: string, taskId: number): Promise<TaskDeleteResult> {
  const res = await fetch(
    `${API_BASE}/tasks/${encodeURIComponent(sessionId)}/${encodeURIComponent(taskId)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) throw await _taskError(res, 'Failed to delete task');
  return res.json();
}

// ── 统一回退（对话 + 文件原子回退，三阶段 stage/commit/clear） ──

export interface RewindFileChange {
  path: string;
  status: 'added' | 'modified' | 'deleted';
  patch: string;
  additions: number;
  deletions: number;
}

export interface RewindPlan {
  plan_id: string;
  session_id: string;
  truncate_at_seq: number;
  target_message: { seq: number; content: string };
  removed_user_messages: number;
  removed_events: number;
  has_snapshot: boolean;
  file_changes: RewindFileChange[];
  expires_at: number;
}

export interface RewindResult {
  plan_id: string;
  truncate_at_seq: number;
  removed_events: number;
  removed_user_messages: number;
  restored_files: string[];
}

export async function stageRewind(
  sessionId: string,
  target: { turns?: number; keepUserMessages?: number }
): Promise<RewindPlan> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/rewind/stage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ turns: target.turns, keep_user_messages: target.keepUserMessages }),
  });
  if (!res.ok) throw new Error('Failed to stage rewind');
  const data = await res.json();
  return data.plan;
}

export async function commitRewind(sessionId: string, planId: string): Promise<RewindResult> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/rewind/commit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId }),
  });
  if (!res.ok) throw new Error('Failed to commit rewind');
  return res.json();
}

export async function clearRewind(sessionId: string, planId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/rewind/clear`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId }),
  });
  if (!res.ok) throw new Error('Failed to clear rewind');
}

export interface FoldedMemory {
  time: string | number;
  trigger: string;
  summary: string;
  session_notes: string;
  project_knowledge: string;
}

export interface CompressionStats {
  utilization: number;
  token_count: number;
  effective_window: number;
  context_window: number;
  tool_fold: { triggered: number };
  session_fold: { triggered: number };
  total_folds: { triggered: number; last_fold_time: number | string | null };
  folded_memories: FoldedMemory[];
}

export async function fetchCompressionStats(sessionId: string): Promise<CompressionStats> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/compression-stats`);
  if (!res.ok) throw new Error('Failed to fetch compression stats');
  return res.json();
}

export interface ContextStoreEntry {
  seq: number;
  type: string;
  call_id: string;
  content_size: number;
}

export interface ContextStoreData {
  entries: ContextStoreEntry[];
  total_entries: number;
  total_raw_size: number;
  active_entries: number;
}

export async function fetchContextStore(sessionId: string): Promise<ContextStoreData> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/context-store`);
  if (!res.ok) throw new Error('Failed to fetch context store');
  return res.json();
}

export interface TokenBreakdown {
  // System prompt 细粒度
  base_prompt_tokens: number;
  claude_md_tokens: number;
  skills_tokens: number;
  wiki_tokens: number;
  // Tools
  tools_tokens: number;
  builtin_tool_count: number;
  mcp_tool_count: number;
  // Messages
  messages_tokens: number;
  message_count: number;
  user_tokens: number;
  assistant_tokens: number;
  tool_tokens: number;
  // 按工具名拆分的结果 token
  tool_result_by_name: Record<string, number>;
  // 总计
  total_tokens: number;
  // Plan mode
  is_plan_mode: boolean;
  plan_mode_tokens: number;
}

export async function fetchTokenBreakdown(sessionId: string): Promise<TokenBreakdown> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/token-breakdown`);
  if (!res.ok) throw new Error('Failed to fetch token breakdown');
  return res.json();
}

export interface SkillEvolutionStatus {
  skill_name: string;
  status: string;
  reasons: string[];
  rule_summary: any;
  replay_pool_size: number;
  replay: any;
  champion: any;
  rules: any[];
  current_version: string;
  lineage_id: string;
}

export async function fetchSkillEvolutionStatus(skillName: string): Promise<SkillEvolutionStatus> {
  const res = await fetch(`${API_BASE}/skill-evolution/status/${skillName}`);
  if (!res.ok) throw new Error('Failed to fetch skill evolution status');
  return res.json();
}

export interface ReplayPoolSample {
  sample_id: string;
  source_type: string;
  split: string;
  time: string;
  ok: boolean;
  latest_user: string;
  latest_assistant: string;
}

export interface ReplayPoolData {
  skill_name: string;
  total_samples: number;
  dev_count: number;
  test_count: number;
  samples: ReplayPoolSample[];
}

export async function fetchReplayPool(skillName: string): Promise<ReplayPoolData> {
  const res = await fetch(`${API_BASE}/skill-evolution/replay-pool/${skillName}`);
  if (!res.ok) throw new Error('Failed to fetch replay pool');
  return res.json();
}

export interface ChampionData {
  skill_name: string;
  has_champion: boolean;
  champion?: {
    version: string;
    average_score: number;
    hard_failures: number;
    promoted_at: string;
    summary: any;
  };
}

export async function fetchChampion(skillName: string): Promise<ChampionData> {
  const res = await fetch(`${API_BASE}/skill-evolution/champion/${skillName}`);
  if (!res.ok) throw new Error('Failed to fetch champion');
  return res.json();
}

export async function fetchSkillProvenance(skillName: string): Promise<any[]> {
  const res = await fetch(`${API_BASE}/skill-evolution/provenance/${skillName}`);
  if (!res.ok) throw new Error('Failed to fetch skill provenance');
  return res.json();
}

export type EvalBenchmark = 'gaia' | 'hle' | 'smoke';
export type EvalRunStatus = 'pending' | 'running' | 'completed' | 'aborted' | 'failed';
export type EvalTaskStatus = 'pending' | 'running' | 'passed' | 'failed' | 'error' | 'aborted';

export interface EvalSuiteSpec {
  id: string;
  name: string;
  description: string;
}

export interface BenchmarkDetail {
  purpose: string;
  data_source: string;
  scoring: string;
  execution: string;
  duration: string;
  commands: string[];
}

export interface EvalBenchmarkSpec {
  id: EvalBenchmark;
  name: string;
  description: string;
  detail?: BenchmarkDetail;
  execution_modes: string[];
  default_options: Record<string, any>;
  suites?: EvalSuiteSpec[];
}

export interface EvalTaskResult {
  task_id: string;
  benchmark: EvalBenchmark;
  name: string;
  status: EvalTaskStatus;
  expected: string;
  predicted: string;
  correct: boolean | null;
  passed: boolean | null;
  duration_s: number;
  tokens: Record<string, any>;
  trace_id: string | null;
  session_id: string | null;
  error: string | null;
  langfuse_dataset: Record<string, any>;
  metadata: Record<string, any>;
}

export interface EvalRunSummary {
  total: number;
  completed?: number;
  correct?: number;
  passed?: number;
  failed?: number;
  errors?: number;
  aborted?: number;
  pass_at_1?: number;
  avg_duration_s?: number;
}

export interface EvalRun {
  run_id: string;
  benchmark: EvalBenchmark;
  eval_session_id: string;
  status: EvalRunStatus;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  model: string;
  options?: Record<string, any>;
  tasks?: EvalTaskResult[];
  summary: EvalRunSummary;
  report_json_path?: string | null;
  report_md_path?: string | null;
  error?: string | null;
}

export interface EvalRunList {
  active: EvalRun[];
  runs: EvalRun[];
}

export interface StartEvalRunRequest {
  benchmark: EvalBenchmark;
  sample?: number | null;
  seed?: number;
  category?: string | null;
  include_image?: boolean;
  only?: string[] | null;
  suite?: string;
  timeout_s?: number;
  model?: string | null;
  api_base?: string | null;
  execution_mode?: 'backend_session' | 'in_process';
  sync_langfuse_dataset?: boolean;
  judge_after_run?: boolean;
  skip_langfuse?: boolean;
  thinking?: boolean | null;
  compression_arm?: 'none' | 'tool_only' | 'session_only' | 'full' | null;
  keep_sessions?: boolean;
  base_url?: string;
  ws_url?: string;
}

async function evalRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/eval${path}`, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || `Eval request failed: ${res.status}`);
  }
  return res.json();
}

export async function fetchEvalBenchmarks(): Promise<{ benchmarks: EvalBenchmarkSpec[] }> {
  return evalRequest('/benchmarks');
}

export interface EvalTaskSpec {
  id: string;
  name: string;
  meta?: Record<string, string>;
}

export async function fetchEvalTasks(
  benchmark: string,
  suite?: string,
): Promise<{ benchmark: string; suite: string | null; total: number; tasks: EvalTaskSpec[] }> {
  const q = new URLSearchParams({ benchmark });
  if (suite) q.set('suite', suite);
  return evalRequest(`/tasks?${q.toString()}`);
}

export async function fetchEvalRuns(limit = 100): Promise<EvalRunList> {
  return evalRequest(`/runs?limit=${limit}`);
}

export async function startEvalRun(request: StartEvalRunRequest): Promise<EvalRun> {
  return evalRequest('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
}

export async function fetchEvalRun(runId: string): Promise<EvalRun> {
  return evalRequest(`/runs/${encodeURIComponent(runId)}`);
}

export async function abortEvalRun(runId: string): Promise<{ run_id: string; aborted: boolean }> {
  return evalRequest(`/runs/${encodeURIComponent(runId)}/abort`, { method: 'POST' });
}

export async function judgeEvalRun(runId: string, judge = true): Promise<Record<string, any>> {
  return evalRequest(`/runs/${encodeURIComponent(runId)}/judge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ judge }),
  });
}

export async function fetchEvalReports(limit = 100): Promise<{ runs: EvalRun[] }> {
  return evalRequest(`/reports?limit=${limit}`);
}

export async function fetchEvalReport(runId: string): Promise<Record<string, any>> {
  return evalRequest(`/reports/${encodeURIComponent(runId)}`);
}

export interface LangfuseInfo {
  configured: boolean;
  base_url: string;
  project_id?: string;
  project_name?: string;
}

export async function fetchEvalLangfuseInfo(): Promise<LangfuseInfo> {
  return evalRequest('/langfuse');
}

export function evalReportMarkdownUrl(runId: string): string {
  return `${API_BASE}/eval/reports/${encodeURIComponent(runId)}/markdown`;
}

export function langfuseProjectUrl(info: LangfuseInfo | null, path: string): string | null {
  if (!info?.base_url || !info.project_id) return null;
  return `${info.base_url.replace(/\/$/, '')}/project/${encodeURIComponent(info.project_id)}${path}`;
}

export function langfuseTraceUrl(info: LangfuseInfo | null, traceId: string): string | null {
  return langfuseProjectUrl(info, `/traces/${encodeURIComponent(traceId)}`);
}

export function langfuseDatasetUrl(info: LangfuseInfo | null, datasetName: string): string | null {
  return langfuseProjectUrl(info, `/datasets/${encodeURIComponent(datasetName)}`);
}

export function langfuseSessionUrl(info: LangfuseInfo | null, sessionId: string): string | null {
  return langfuseProjectUrl(info, `/sessions/${encodeURIComponent(sessionId)}`);
}

// ── Embedding 模型配置（wiki 语义召回/编译去重后端，BC-30） ──

export interface EmbeddingConfig {
  backend: string;      // openai=OpenAI 兼容 API；ollama=本地
  base_url: string;
  model: string;
  api_key_set: boolean; // 密钥不回显，仅报是否已配置
}

export interface EmbeddingConfigInput {
  backend: string;
  base_url: string;
  model: string;
  api_key?: string | null; // null/缺省=保留已存密钥
}

export interface EmbeddingVerifyResult {
  status: 'success' | 'error';
  message: string;
  dim?: number;
  latency_ms?: number;
}

export async function fetchEmbeddingConfig(): Promise<EmbeddingConfig> {
  const res = await fetch(`${API_BASE}/embedding-config`);
  if (!res.ok) throw new Error('Failed to fetch embedding config');
  return res.json();
}

export async function saveEmbeddingConfig(cfg: EmbeddingConfigInput): Promise<void> {
  const res = await fetch(`${API_BASE}/embedding-config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  if (!res.ok) throw new Error('Failed to save embedding config');
}

export async function verifyEmbeddingConfig(cfg: EmbeddingConfigInput): Promise<EmbeddingVerifyResult> {
  const res = await fetch(`${API_BASE}/embedding-config/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  if (!res.ok) throw new Error('Failed to verify embedding config');
  return res.json();
}

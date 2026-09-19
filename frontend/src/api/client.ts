// API client for MyCode backend

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

export interface Memory {
  name: string;
  description: string;
  type: string;
  filename: string;
  content: string;
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

export interface TraceEvent {
  ts: string;
  kind: string;
  [key: string]: any;
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
  protocol: 'openai' | 'anthropic';
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

export async function fetchMemories(): Promise<Memory[]> {
  const res = await fetch(`${API_BASE}/memories`);
  if (!res.ok) throw new Error('Failed to fetch memories');
  return res.json();
}

export async function fetchMemory(filename: string): Promise<Memory> {
  const res = await fetch(`${API_BASE}/memories/${filename}`);
  if (!res.ok) throw new Error('Failed to fetch memory');
  return res.json();
}

export async function createMemory(data: Omit<Memory, 'filename'>): Promise<{ filename: string }> {
  const res = await fetch(`${API_BASE}/memories`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to create memory');
  return res.json();
}

export async function deleteMemory(filename: string): Promise<void> {
  const res = await fetch(`${API_BASE}/memories/${filename}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete memory');
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

export async function fetchSkillEvolutionUsage(): Promise<any> {
  const res = await fetch(`${API_BASE}/skill-evolution/usage`);
  if (!res.ok) throw new Error('Failed to fetch evolution usage');
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
export async function fetchTraceEvents(n: number = 50, session?: string): Promise<{ enabled: boolean; path: string; session?: string; events: TraceEvent[] }> {
  const params = new URLSearchParams({ n: String(n) });
  if (session) params.set('session', session);
  const res = await fetch(`${API_BASE}/trace?${params}`);
  if (!res.ok) throw new Error('Failed to fetch trace events');
  return res.json();
}

export interface TraceFile {
  filename: string;
  session_id: string;
  created_at: string | null;
  size: number;
  line_count: number;
  modified: number;
}

export async function fetchTraceFiles(): Promise<{ files: TraceFile[] }> {
  const res = await fetch(`${API_BASE}/trace/files`);
  if (!res.ok) throw new Error('Failed to fetch trace files');
  return res.json();
}

export async function toggleTrace(enabled: boolean): Promise<void> {
  const res = await fetch(`${API_BASE}/trace/toggle`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error('Failed to toggle trace');
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

export async function fetchWorkspaceFile(path: string, cwd?: string): Promise<{ path: string; content: string; size: number }> {
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

export interface ModelEndpointConfig {
  model: string;
  base_url: string;
  api_key: string;
  provider_name?: string;
  context_window?: number;
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
  execute: string;
  review: string;
  converge: string;
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

export async function compactSession(sessionId: string): Promise<{ success: boolean; message: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/compact`, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to compact session');
  return res.json();
}

export async function rewindSession(sessionId: string, turns: number): Promise<{ message: string }> {
  console.log('[API] rewindSession:', { sessionId, turns });
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/rewind`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, turns }),
  });
  if (!res.ok) throw new Error('Failed to rewind session');
  const data = await res.json();
  console.log('[API] rewindSession result:', data);
  return data;
}

export interface SessionStats {
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  context_window: number;
  effective_window: number;
  last_input_token_count: number;
}

export async function fetchSessionStats(sessionId: string): Promise<SessionStats> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/stats`);
  if (!res.ok) throw new Error('Failed to fetch session stats');
  return res.json();
}

export async function togglePlanMode(sessionId: string, enabled: boolean): Promise<{ mode: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan-mode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, enabled }),
  });
  if (!res.ok) throw new Error('Failed to toggle plan mode');
  return res.json();
}

export async function getSessionStatus(sessionId: string): Promise<{
  active: boolean;
  model?: string;
  permission_mode?: string;
  total_input_tokens?: number;
  total_output_tokens?: number;
  current_turns?: number;
  context?: {
    used_tokens: number;
    total_tokens: number;
    occupancy_percent: number;
  };
}> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/status`);
  if (!res.ok) throw new Error('Failed to get session status');
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

export async function activateSession(sessionId: string): Promise<{ success: boolean; session_id: string; message: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/activate`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error('Failed to activate session');
  return res.json();
}

export async function getSessionStats(sessionId: string): Promise<{
  session_id: string;
  model: string;
  total_input_tokens: number;
  total_output_tokens: number;
  current_turns: number;
  tool_execution_stats: Record<string, { count: number; total_ms: number }>;
}> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/stats`);
  if (!res.ok) throw new Error('Failed to get session stats');
  return res.json();
}

export async function steerSession(sessionId: string, message: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/steer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!res.ok) throw new Error('Failed to steer session');
}

export async function followUpSession(sessionId: string, message: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/follow-up`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!res.ok) throw new Error('Failed to follow-up session');
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

export async function respondToPermission(sessionId: string, requestId: string, allowed: boolean): Promise<void> {
  const res = await fetch(`${API_BASE}/events/respond`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rpc_id: requestId, allowed, session_id: sessionId }),
  });
  if (!res.ok) throw new Error('Failed to respond to permission');
}

export async function updatePlanFile(sessionId: string, planFilePath: string, content: string): Promise<{ success: boolean; message?: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan/update`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_file_path: planFilePath, content }),
  });
  if (!res.ok) throw new Error('Failed to update plan file');
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

export async function truncateSession(sessionId: string, keepUserMessages: number): Promise<void> {
  console.log('[API] truncateSession:', { sessionId, keepUserMessages });
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/truncate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keep_user_messages: keepUserMessages }),
  });
  if (!res.ok) throw new Error('Failed to truncate session');
  console.log('[API] truncateSession done');
}

export interface Snapshot {
  id: string;
  tree_hash: string;
  file_count: number;
  created_at: number;
  label?: string;
  message_id?: string;
}

export interface FileDiff {
  path: string;
  status: 'added' | 'modified' | 'deleted';
  patch: string;
  additions: number;
  deletions: number;
}

export interface SnapshotInspection {
  id: string;
  tree_hash: string;
  created_at: number;
  label?: string;
  files: FileDiff[];
}

export interface RevertPlan {
  id: string;
  session_id: string;
  snapshot_id: string;
  original_snapshot_id: string;
  changes: FileDiff[];
  created_at: number;
  expires_at: number;
  message_id?: string;
}

export interface RevertResult {
  plan_id: string;
  restored_files: string[];
  action: 'committed' | 'cleared';
}

export async function createSnapshot(
  sessionId: string,
  label?: string,
  messageId?: string
): Promise<Snapshot> {
  const res = await fetch(`${API_BASE}/snapshots`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, label, message_id: messageId }),
  });
  if (!res.ok) throw new Error('Failed to create snapshot');
  const data = await res.json();
  return data.snapshot;
}

export async function listSnapshots(sessionId?: string): Promise<Snapshot[]> {
  const params = sessionId ? `?session_id=${sessionId}` : '';
  const res = await fetch(`${API_BASE}/snapshots${params}`);
  if (!res.ok) throw new Error('Failed to list snapshots');
  const data = await res.json();
  return data.snapshots;
}

export async function inspectSnapshot(snapshotId: string): Promise<SnapshotInspection> {
  const res = await fetch(`${API_BASE}/snapshots/${snapshotId}`);
  if (!res.ok) throw new Error('Failed to inspect snapshot');
  const data = await res.json();
  return data.inspection;
}

export async function restoreSnapshot(snapshotId: string, files?: string[]): Promise<string[]> {
  const res = await fetch(`${API_BASE}/snapshots/${snapshotId}/restore`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ snapshot_id: snapshotId, files }),
  });
  if (!res.ok) throw new Error('Failed to restore snapshot');
  const data = await res.json();
  return data.restored_files;
}

export async function stageRevert(
  sessionId: string,
  snapshotId: string,
  messageId?: string
): Promise<RevertPlan> {
  const res = await fetch(`${API_BASE}/revert/stage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, snapshot_id: snapshotId, message_id: messageId }),
  });
  if (!res.ok) throw new Error('Failed to stage revert');
  const data = await res.json();
  return data.plan;
}

export async function commitRevert(planId: string): Promise<RevertResult> {
  const res = await fetch(`${API_BASE}/revert/commit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId }),
  });
  if (!res.ok) throw new Error('Failed to commit revert');
  const data = await res.json();
  return data.result;
}

export async function clearRevert(planId: string): Promise<RevertResult> {
  const res = await fetch(`${API_BASE}/revert/clear`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId }),
  });
  if (!res.ok) throw new Error('Failed to clear revert');
  const data = await res.json();
  return data.result;
}

export interface CompressionStats {
  utilization: number;
  token_count: number;
  effective_window: number;
  context_window: number;
  l1_budget: { triggered: number; tokens_saved: number };
  l2_snip: { triggered: number; tokens_saved: number };
  l3_microcompact: { triggered: number; tokens_saved: number };
  l4_fold: { triggered: number; last_fold_time: string | null };
  folded_memories: any[];
}

export async function fetchCompressionStats(sessionId: string): Promise<CompressionStats> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/compression-stats`);
  if (!res.ok) throw new Error('Failed to fetch compression stats');
  return res.json();
}

export interface ContextStoreEntry {
  key: string;
  raw_size: number;
  abstract: string;
  dropped: boolean;
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
  memory_tokens: number;
  wiki_tokens: number;
  agents_tokens: number;
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
  usage_stats: { retrieved: number; relevant: number; used: number; relevance_rate: number; used_rate: number };
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

export interface TraceStatus {
  enabled: boolean;
  otel_enabled: boolean;
  phoenix_endpoint: string;
  phoenix_reachable: boolean;
  path: string;
  events: any[];
}

export async function fetchTraceStatus(): Promise<TraceStatus> {
  const res = await fetch(`${API_BASE}/trace`);
  if (!res.ok) throw new Error('Failed to fetch trace status');
  return res.json();
}

// Plan APIs
export async function getPlanStatus(sessionId: string, slug: string): Promise<{ success: boolean; data?: any; message?: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan/${slug}/status`);
  if (!res.ok) return { success: false, message: 'Failed to fetch plan status' };
  return res.json();
}

export async function planPause(sessionId: string, slug: string): Promise<{ success: boolean }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan/${slug}/pause`, { method: 'POST' });
  return res.json();
}

export async function planResume(sessionId: string, slug: string): Promise<{ success: boolean }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan/${slug}/resume`, { method: 'POST' });
  return res.json();
}

export async function planSkipTask(sessionId: string, slug: string, taskId: number): Promise<{ success: boolean }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan/${slug}/skip-task`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id: taskId }),
  });
  return res.json();
}

export async function planAbandon(sessionId: string, slug: string): Promise<{ success: boolean }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan/${slug}/abandon`, { method: 'POST' });
  return res.json();
}

export async function getPlanArtifacts(sessionId: string, slug: string): Promise<{ success: boolean; data?: Record<string, string>; message?: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan/${slug}/artifacts`);
  if (!res.ok) return { success: false, message: 'Failed to fetch plan artifacts' };
  return res.json();
}

export async function getPlanLedger(sessionId: string, slug: string): Promise<{ success: boolean; data?: any[]; message?: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/plan/${slug}/ledger`);
  if (!res.ok) return { success: false, message: 'Failed to fetch plan ledger' };
  return res.json();
}

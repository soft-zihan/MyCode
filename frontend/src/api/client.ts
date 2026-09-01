// API client for MyCode backend

const API_BASE = '/api';

export interface Session {
  id: string;
  startTime: string;
  model: string;
  cwd: string;
  messageCount: number;
  name?: string;
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
  allowed_tools?: string[];
  has_system_prompt: boolean;
}

export interface AgentDetail extends Agent {
  tools_count: number;
  tools: string[];
  system_prompt_preview: string;
  custom_config: any;
}

export interface Endpoint {
  id: string;
  model: string;
  base_url: string;
  has_api_key: boolean;
  protocol: 'openai' | 'anthropic';
}

export interface PrimaryEndpoint {
  model: string;
  base_url: string;
  has_api_key: boolean;
}

export async function fetchSessions(): Promise<Session[]> {
  const res = await fetch(`${API_BASE}/sessions`);
  if (!res.ok) throw new Error('Failed to fetch sessions');
  return res.json();
}

export async function fetchSession(id: string): Promise<SessionDetail> {
  const res = await fetch(`${API_BASE}/sessions/${id}`);
  if (!res.ok) throw new Error('Failed to fetch session');
  return res.json();
}

export async function deleteSession(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete session');
}

export async function generateSessionName(message: string): Promise<string> {
  const res = await fetch(`${API_BASE}/sessions/generate-name`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message })
  });
  if (!res.ok) throw new Error('Failed to generate session name');
  const data = await res.json();
  return data.name;
}

export async function updateSessionName(id: string, name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name })
  });
  if (!res.ok) throw new Error('Failed to update session name');
}

export async function saveSessionMessages(id: string, messages: any[]): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ frontendMessages: messages })
  });
  if (!res.ok) throw new Error('Failed to save session messages');
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
export async function fetchTraceEvents(n: number = 50): Promise<{ enabled: boolean; path: string; events: TraceEvent[] }> {
  const res = await fetch(`${API_BASE}/trace?n=${n}`);
  if (!res.ok) throw new Error('Failed to fetch trace events');
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

export async function fetchWorkspaceFile(path: string): Promise<{ path: string; content: string; size: number }> {
  const res = await fetch(`${API_BASE}/workspace/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error('Failed to fetch file');
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

export async function fetchMcpServers(): Promise<McpServer[]> {
  const res = await fetch(`${API_BASE}/mcp`);
  if (!res.ok) throw new Error('Failed to fetch MCP servers');
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

// ── Session Control ──────────────────────────────────────────────────────────

export async function abortSession(sessionId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/abort`, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to abort session');
}

export async function compactSession(sessionId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/compact`, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to compact session');
}

export async function rewindSession(sessionId: string, turns: number): Promise<{ message: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/rewind`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, turns }),
  });
  if (!res.ok) throw new Error('Failed to rewind session');
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

export async function forkSession(sessionId: string): Promise<{ new_session_id: string; message: string }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/fork`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error('Failed to fork session');
  return res.json();
}

export interface PermissionRequest {
  request_id: string;
  command: string;
  tool_name: string;
  sub_agent_id?: string;
}

export async function respondToPermission(sessionId: string, requestId: string, allowed: boolean): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/permission-response`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, allowed }),
  });
  if (!res.ok) throw new Error('Failed to respond to permission');
}

export async function truncateSession(sessionId: string, keepUserMessages: number): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/truncate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keep_user_messages: keepUserMessages }),
  });
  if (!res.ok) throw new Error('Failed to truncate session');
}

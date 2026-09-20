export type ChatNodeKind = 'user' | 'assistant' | 'thinking' | 'tool-call' | 'sub-agent' | 'error' | 'turn-status' | 'system';

export interface BaseNode {
  key: string;
  kind: ChatNodeKind;
  seq: number;
}

export interface UserNode extends BaseNode {
  kind: 'user';
  content: string;
  contextFiles?: string[];
  agent?: string;
  model?: string;
  timestamp: string;
}

export interface AssistantNode extends BaseNode {
  kind: 'assistant';
  content: string;
  thinking?: string;
  streaming: boolean;
  timestamp: string;
  turn?: number;
  step?: number;
  hasToolCalls?: boolean;
}

export interface ThinkingNode extends BaseNode {
  kind: 'thinking';
  content: string;
  streaming: boolean;
  complete: boolean;
}

export interface ToolCallNode extends BaseNode {
  kind: 'tool-call';
  callId: string;
  name: string;
  input: Record<string, unknown>;
  status: 'pending' | 'success' | 'error' | 'denied';
  result?: string;
  durationMs?: number;
  snapshot?: { file_path: string; old_content: string; new_content: string };
}

export type SubAgentEventItem =
  | { type: 'thinking'; content: string }
  | { type: 'text'; content: string }
  | { type: 'tool_call'; toolCall: ToolCallNode };

export interface SubAgentNode extends BaseNode {
  kind: 'sub-agent';
  agentId: string;
  agentType: string;
  description: string;
  status: 'running' | 'completed' | 'error';
  thinking?: string;
  text?: string;
  toolCalls: ToolCallNode[];
  internalOrder: SubAgentEventItem[];
  tokens?: number;
  durationMs?: number;
}

export interface ErrorNode extends BaseNode {
  kind: 'error';
  message: string;
}

export interface TurnStatusNode extends BaseNode {
  kind: 'turn-status';
  startTime: number;
}

export interface SystemNode extends BaseNode {
  kind: 'system';
  message: string;
  timestamp: string;
}

export type ChatNode = 
  | UserNode 
  | AssistantNode 
  | ThinkingNode 
  | ToolCallNode 
  | SubAgentNode 
  | ErrorNode 
  | TurnStatusNode
  | SystemNode;

export interface ChatSnapshot {
  order: string[];
  nodes: Map<string, ChatNode>;
}

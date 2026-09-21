import { useCallback, useRef, useState } from 'react';

export interface SessionEvent {
  seq: number;
  type: string;
  time: number;
  session_id: string;
  [key: string]: unknown;
}

export interface AgentStats {
  input_tokens: number;
  output_tokens: number;
  cached_tokens?: number;
  context_window: number;
  effective_window?: number;
  last_input_token_count?: number;
  last_total_token_count?: number;
  estimated_context_tokens?: number;
}

export interface ToolCallEvent {
  call_id: string;
  name: string;
  input: Record<string, unknown>;
  status: 'pending' | 'success' | 'error' | 'denied';
  result?: string;
  duration_ms?: number;
  snapshot?: { file_path: string; old_content: string; new_content: string };
  sub_agent_id?: string;
}

export interface SubAgentEvent {
  agent_id: string;
  agent_type: string;
  description: string;
  status: 'running' | 'completed' | 'error';
  summary?: string;
  tokens?: number;
  duration_ms?: number;
  thinking?: string;
  text?: string;
  tool_calls: ToolCallEvent[];
}

export interface ContextInfo {
  used_tokens: number;
  total_tokens: number;
  occupancy_percent: number;
}

export interface PermissionRequest {
  rpc_id: string;
  request_id: string;
  command: string;
  tool_name: string;
  message: string;
  sub_agent_id?: string;
}

export interface AgentEventsState {
  stats: AgentStats | null;
  toolCalls: Map<string, ToolCallEvent>;
  subAgents: Map<string, SubAgentEvent>;
  permissionRequest: PermissionRequest | null;
  isStreaming: boolean;
  lastSeq: number;
}

interface UseAgentEventsReturn {
  state: AgentEventsState;
  handleSessionEvent: (event: SessionEvent) => void;
  handleSSEEvent: (event: MessageEvent) => void;
  resetState: () => void;
  respondPermission: (rpcId: string, allowed: boolean) => Promise<void>;
}

const initialState: AgentEventsState = {
  stats: null,
  toolCalls: new Map(),
  subAgents: new Map(),
  permissionRequest: null,
  isStreaming: false,
  lastSeq: 0,
};

export function useAgentEvents(sessionId: string | null): UseAgentEventsReturn {
  const [state, setState] = useState<AgentEventsState>(initialState);
  const stateRef = useRef(state);
  stateRef.current = state;

  const handleSessionEvent = useCallback((event: SessionEvent) => {
    setState(prev => {
      const next = { ...prev };
      let changed = false;

      next.lastSeq = Math.max(prev.lastSeq, event.seq);
      changed = true;

      switch (event.type) {
        case 'thinking': {
          const content = event.content as string;
          const subAgentId = event.sub_agent_id as string | undefined;
          if (subAgentId && prev.subAgents.has(subAgentId)) {
            const newSubAgents = new Map(prev.subAgents);
            const agent = newSubAgents.get(subAgentId)!;
            newSubAgents.set(subAgentId, {
              ...agent,
              thinking: (agent.thinking || '') + content,
            });
            next.subAgents = newSubAgents;
          }
          break;
        }

        case 'text': {
          const content = event.content as string;
          const subAgentId = event.sub_agent_id as string | undefined;
          if (subAgentId && prev.subAgents.has(subAgentId)) {
            const newSubAgents = new Map(prev.subAgents);
            const agent = newSubAgents.get(subAgentId)!;
            newSubAgents.set(subAgentId, {
              ...agent,
              text: (agent.text || '') + content,
            });
            next.subAgents = newSubAgents;
          }
          break;
        }

        case 'tool_call': {
          const callId = event.call_id as string;
          const name = event.name as string;
          const input = event.input as Record<string, unknown>;
          const subAgentId = event.sub_agent_id as string | undefined;
          
          const toolCall: ToolCallEvent = {
            call_id: callId,
            name,
            input,
            status: 'pending',
            sub_agent_id: subAgentId,
          };

          if (subAgentId && prev.subAgents.has(subAgentId)) {
            const newSubAgents = new Map(prev.subAgents);
            const agent = newSubAgents.get(subAgentId)!;
            newSubAgents.set(subAgentId, {
              ...agent,
              tool_calls: [...agent.tool_calls, toolCall],
            });
            next.subAgents = newSubAgents;
          } else {
            const newToolCalls = new Map(prev.toolCalls);
            newToolCalls.set(callId, toolCall);
            next.toolCalls = newToolCalls;
          }
          break;
        }

        case 'tool_result': {
          const callId = event.call_id as string;
          const result = event.result as string;
          const status = (event.status as string) || 'success';
          const subAgentId = event.sub_agent_id as string | undefined;

          if (subAgentId && prev.subAgents.has(subAgentId)) {
            const newSubAgents = new Map(prev.subAgents);
            const agent = newSubAgents.get(subAgentId)!;
            const toolCalls = [...agent.tool_calls];
            const idx = toolCalls.findIndex(tc => tc.call_id === callId);
            if (idx !== -1) {
              toolCalls[idx] = { ...toolCalls[idx], result, status: status as ToolCallEvent['status'] };
            }
            newSubAgents.set(subAgentId, { ...agent, tool_calls: toolCalls });
            next.subAgents = newSubAgents;
          } else {
            const newToolCalls = new Map(prev.toolCalls);
            if (newToolCalls.has(callId)) {
              const existing = newToolCalls.get(callId)!;
              newToolCalls.set(callId, { ...existing, result, status: status as ToolCallEvent['status'] });
            }
            next.toolCalls = newToolCalls;
          }
          break;
        }

        case 'sub_agent/start': {
          const agentId = event.agent_id as string;
          const agentType = event.agent_type as string;
          const description = event.description as string;
          const newSubAgents = new Map(prev.subAgents);
          newSubAgents.set(agentId, {
            agent_id: agentId,
            agent_type: agentType,
            description,
            status: 'running',
            thinking: '',
            text: '',
            tool_calls: [],
          });
          next.subAgents = newSubAgents;
          break;
        }

        case 'sub_agent/end': {
          const agentId = event.agent_id as string;
          const status = (event.status as string) || 'completed';
          const summary = event.summary as string | undefined;
          const durationMs = event.duration_ms as number | undefined;
          
          const newSubAgents = new Map(prev.subAgents);
          if (newSubAgents.has(agentId)) {
            const existing = newSubAgents.get(agentId)!;
            newSubAgents.set(agentId, {
              ...existing,
              status: status as SubAgentEvent['status'],
              summary,
              duration_ms: durationMs,
            });
          }
          next.subAgents = newSubAgents;
          break;
        }

        case 'stats': {
          next.stats = {
            input_tokens: event.input_tokens as number,
            output_tokens: event.output_tokens as number,
            cached_tokens: event.cached_tokens as number | undefined,
            context_window: event.context_window as number,
            effective_window: event.effective_window as number | undefined,
            last_input_token_count: event.last_input_token_count as number | undefined,
            last_total_token_count: event.last_total_token_count as number | undefined,
            estimated_context_tokens: event.estimated_context_tokens as number | undefined,
          };
          break;
        }

        case 'permission/request': {
          next.permissionRequest = {
            rpc_id: event.rpc_id as string,
            request_id: event.request_id as string,
            command: event.command as string,
            tool_name: event.tool_name as string,
            message: event.message as string,
            sub_agent_id: event.sub_agent_id as string | undefined,
          };
          break;
        }

        case 'turn/start': {
          next.isStreaming = true;
          break;
        }

        case 'turn/end': {
          next.isStreaming = false;
          break;
        }
      }

      return changed ? next : prev;
    });
  }, []);

  const handleSSEEvent = useCallback((event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data);
      handleSessionEvent(data);
    } catch (e) {
      console.error('Failed to parse SSE event:', e);
    }
  }, [handleSessionEvent]);

  const resetState = useCallback(() => {
    setState({
      ...initialState,
      toolCalls: new Map(),
      subAgents: new Map(),
    });
  }, []);

  const respondPermission = useCallback(async (rpcId: string, allowed: boolean) => {
    try {
      await fetch('/api/events/respond', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rpc_id: rpcId, allowed, session_id: sessionId }),
      });
      setState(prev => ({ ...prev, permissionRequest: null }));
    } catch (e) {
      console.error('Failed to respond permission:', e);
    }
  }, [sessionId]);

  return {
    state,
    handleSessionEvent,
    handleSSEEvent,
    resetState,
    respondPermission,
  };
}

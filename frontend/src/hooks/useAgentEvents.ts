import { useCallback, useRef, useState } from 'react';

export interface AgentStats {
  turns: number;
  steps: number;
  llm_ms: number;
  tool_ms: number;
  ttft_ms: number;
  tokens_per_sec: number;
  cache_hit_percent: number;
  input_tokens: number;
  output_tokens: number;
}

export interface ContextInfo {
  used_tokens: number;
  total_tokens: number;
  occupancy_percent: number;
}

export interface ToolCallEvent {
  call_id: string;
  name: string;
  input: Record<string, unknown>;
  status: 'pending' | 'success' | 'error' | 'denied';
  result?: string;
  duration_ms?: number;
  snapshot?: { file_path: string; old_content: string; new_content: string };
}

export interface SubAgentEvent {
  agent_id: string;
  agent_type: string;
  description: string;
  status: 'running' | 'completed' | 'error';
  summary?: string;
  tokens?: number;
  duration_ms?: number;
  // 子智能体的完整运行过程
  thinking?: string;
  text?: string;
  tool_calls: ToolCallEvent[];
}

export interface PermissionRequest {
  request_id: string;
  action: string;
  resource: string;
  message: string;
}

export interface AgentEventsState {
  stats: AgentStats | null;
  context: ContextInfo | null;
  toolCalls: Map<string, ToolCallEvent>;
  subAgents: Map<string, SubAgentEvent>;
  permissionRequest: PermissionRequest | null;
  isStreaming: boolean;
}

interface UseAgentEventsReturn {
  state: AgentEventsState;
  handleSSEEvent: (event: MessageEvent) => void;
  resetState: () => void;
  respondPermission: (requestId: string, allowed: boolean) => Promise<void>;
}

const initialState: AgentEventsState = {
  stats: null,
  context: null,
  toolCalls: new Map(),
  subAgents: new Map(),
  permissionRequest: null,
  isStreaming: false,
};

export function useAgentEvents(sessionId: string | null): UseAgentEventsReturn {
  const [state, setState] = useState<AgentEventsState>(initialState);
  const stateRef = useRef(state);
  stateRef.current = state;

  const handleSSEEvent = useCallback((event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data);
      console.log('[useAgentEvents] SSE event:', data);
      
      setState(prev => {
        const next = { ...prev };
        let changed = false;

        // Thinking event
        if (data.thinking !== undefined) {
          const { sub_agent_id, content } = data.thinking;
          // 如果属于子智能体，更新子智能体的 thinking
          if (sub_agent_id && prev.subAgents.has(sub_agent_id)) {
            const newSubAgents = new Map(prev.subAgents);
            const agent = newSubAgents.get(sub_agent_id)!;
            newSubAgents.set(sub_agent_id, {
              ...agent,
              thinking: (agent.thinking || '') + content,
            });
            next.subAgents = newSubAgents;
            changed = true;
          }
          // 主智能体的 thinking 由 message content parser 处理
          return changed ? next : prev;
        }

        // Text chunk (from sub-agent)
        if (data.text !== undefined) {
          const { sub_agent_id, content } = data.text;
          // 如果属于子智能体，更新子智能体的 text
          if (sub_agent_id && prev.subAgents.has(sub_agent_id)) {
            const newSubAgents = new Map(prev.subAgents);
            const agent = newSubAgents.get(sub_agent_id)!;
            newSubAgents.set(sub_agent_id, {
              ...agent,
              text: (agent.text || '') + content,
            });
            next.subAgents = newSubAgents;
            changed = true;
          }
          // 主智能体的 text 由 message content parser 处理
          return changed ? next : prev;
        }

        // Tool call start
        if (data.tool_call) {
          const { name, input, call_id, sub_agent_id } = data.tool_call;
          const id = call_id || `tc_${Date.now()}`;
          const toolCall: ToolCallEvent = {
            call_id: id,
            name,
            input,
            status: 'pending',
          };
          
          // 如果属于子智能体，存储到子智能体的 tool_calls 中
          if (sub_agent_id && prev.subAgents.has(sub_agent_id)) {
            const newSubAgents = new Map(prev.subAgents);
            const agent = newSubAgents.get(sub_agent_id)!;
            newSubAgents.set(sub_agent_id, {
              ...agent,
              tool_calls: [...agent.tool_calls, toolCall],
            });
            next.subAgents = newSubAgents;
          } else {
            // 否则存储到主工具调用映射中
            const newToolCalls = new Map(prev.toolCalls);
            newToolCalls.set(id, toolCall);
            next.toolCalls = newToolCalls;
          }
          changed = true;
        }

        // Tool result
        if (data.tool_result) {
          const { name, result, status, call_id, snapshot, duration_ms, sub_agent_id } = data.tool_result;
          
          // 如果属于子智能体，更新子智能体的 tool_calls
          if (sub_agent_id && prev.subAgents.has(sub_agent_id)) {
            const newSubAgents = new Map(prev.subAgents);
            const agent = newSubAgents.get(sub_agent_id)!;
            const toolCalls = [...agent.tool_calls];
            
            // Find by call_id or by name (fallback)
            let targetId = call_id;
            if (!targetId) {
              for (const tc of toolCalls) {
                if (tc.name === name && tc.status === 'pending') {
                  targetId = tc.call_id;
                  break;
                }
              }
            }
            
            const idx = toolCalls.findIndex(tc => tc.call_id === targetId);
            if (idx !== -1) {
              toolCalls[idx] = {
                ...toolCalls[idx],
                result,
                status: status || 'success',
                snapshot,
                duration_ms,
              };
            } else {
              // Create new entry if not found
              const newId = call_id || `tc_${Date.now()}`;
              toolCalls.push({
                call_id: newId,
                name,
                input: {},
                status: status || 'success',
                result,
                snapshot,
                duration_ms,
              });
            }
            
            newSubAgents.set(sub_agent_id, {
              ...agent,
              tool_calls: toolCalls,
            });
            next.subAgents = newSubAgents;
          } else {
            // 否则更新主工具调用映射
            const newToolCalls = new Map(prev.toolCalls);
            // Find by call_id or by name (fallback)
            let targetId = call_id;
            if (!targetId) {
              for (const [id, tc] of newToolCalls) {
                if (tc.name === name && tc.status === 'pending') {
                  targetId = id;
                  break;
                }
              }
            }
            if (targetId && newToolCalls.has(targetId)) {
              const existing = newToolCalls.get(targetId)!;
              newToolCalls.set(targetId, {
                ...existing,
                result,
                status: status || 'success',
                snapshot,
                duration_ms,
              });
            } else {
              // Create new entry if not found
              const newId = call_id || `tc_${Date.now()}`;
              newToolCalls.set(newId, {
                call_id: newId,
                name,
                input: {},
                status: status || 'success',
                result,
                snapshot,
                duration_ms,
              });
            }
            next.toolCalls = newToolCalls;
          }
          changed = true;
        }

        // Sub-agent start
        if (data.sub_agent_start) {
          const { agent_type, description, agent_id } = data.sub_agent_start;
          const newSubAgents = new Map(prev.subAgents);
          const id = agent_id || `sa_${Date.now()}`;
          newSubAgents.set(id, {
            agent_id: id,
            agent_type,
            description,
            status: 'running',
            thinking: '',
            text: '',
            tool_calls: [],
          });
          next.subAgents = newSubAgents;
          changed = true;
        }

        // Sub-agent end
        if (data.sub_agent_end) {
          const { agent_type, description, agent_id, summary, tokens, duration_ms } = data.sub_agent_end;
          const newSubAgents = new Map(prev.subAgents);
          // Find by agent_id or agent_type
          let targetId = agent_id;
          if (!targetId) {
            for (const [id, sa] of newSubAgents) {
              if (sa.agent_type === agent_type && sa.status === 'running') {
                targetId = id;
                break;
              }
            }
          }
          if (targetId && newSubAgents.has(targetId)) {
            const existing = newSubAgents.get(targetId)!;
            newSubAgents.set(targetId, {
              ...existing,
              status: 'completed',
              summary,
              tokens,
              duration_ms,
            });
          } else {
            const newId = agent_id || `sa_${Date.now()}`;
            newSubAgents.set(newId, {
              agent_id: newId,
              agent_type,
              description: description || '',
              status: 'completed',
              summary,
              tokens,
              duration_ms,
              thinking: '',
              text: '',
              tool_calls: [],
            });
          }
          next.subAgents = newSubAgents;
          changed = true;
        }

        // Stats update
        if (data.stats && typeof data.stats === 'object') {
          console.log('[useAgentEvents] Stats update:', data.stats);
          next.stats = {
            turns: data.stats.turns ?? 0,
            steps: data.stats.steps ?? 0,
            llm_ms: data.stats.llm_ms ?? 0,
            tool_ms: data.stats.tool_ms ?? 0,
            ttft_ms: data.stats.ttft_ms ?? 0,
            tokens_per_sec: data.stats.tokens_per_sec ?? 0,
            cache_hit_percent: data.stats.cache_hit_percent ?? 0,
            input_tokens: data.stats.input_tokens ?? 0,
            output_tokens: data.stats.output_tokens ?? 0,
          };
          changed = true;
        }

        // Context update
        if (data.context && typeof data.context === 'object') {
          next.context = {
            used_tokens: data.context.used_tokens ?? 0,
            total_tokens: data.context.total_tokens ?? 0,
            occupancy_percent: data.context.occupancy_percent ?? 0,
          };
          changed = true;
        }

        // Permission request
        if (data.permission_request) {
          next.permissionRequest = data.permission_request;
          changed = true;
        }

        // Steering injected
        if (data.steering_injected) {
          // Could show a notification
          return prev;
        }

        // Info message
        if (data.info) {
          // Could show a notification
          return prev;
        }

        // Done
        if (data.done) {
          next.isStreaming = false;
          changed = true;
        }

        // Error
        if (data.error) {
          next.isStreaming = false;
          changed = true;
        }

        return changed ? next : prev;
      });
    } catch (e) {
      console.error('Failed to parse SSE event:', e);
    }
  }, []);

  const resetState = useCallback(() => {
    setState({
      ...initialState,
      toolCalls: new Map(),
      subAgents: new Map(),
    });
  }, []);

  const respondPermission = useCallback(async (requestId: string, allowed: boolean) => {
    try {
      await fetch('/api/permission/respond', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: requestId, allowed, session_id: sessionId }),
      });
      setState(prev => ({ ...prev, permissionRequest: null }));
    } catch (e) {
      console.error('Failed to respond permission:', e);
    }
  }, [sessionId]);

  return {
    state,
    handleSSEEvent,
    resetState,
    respondPermission,
  };
}

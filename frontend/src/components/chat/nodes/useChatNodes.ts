import { useCallback } from 'react';
import { sessionStore, useSessionStore } from '../../../store';
import type { ChatNode, ChatSnapshot, ToolCallNode, SubAgentNode, SubAgentEventItem } from './types';

interface UseChatNodesReturn {
  snapshot: ChatSnapshot;
  handleSSEEvent: (sessionId: string, data: Record<string, unknown>) => void;
  addUserMessage: (sessionId: string, content: string, contextFiles?: string[], agent?: string, model?: string) => void;
  resetNodes: (sessionId: string) => void;
  loadSessionEvents: (sessionId: string, events: Array<Record<string, unknown>>) => void;
  prependSessionEvents: (sessionId: string, events: Array<Record<string, unknown>>) => void;
}

let nodeSeq = 0;
const nextSeq = () => ++nodeSeq;
const nextKey = (prefix: string) => `${prefix}_${nextSeq()}`;

const createSubAgentNode = (agentId: string): SubAgentNode => ({
  key: `subagent_${agentId}`,
  kind: 'sub-agent',
  seq: nextSeq(),
  agentId,
  agentType: 'unknown',
  description: '',
  status: 'running',
  toolCalls: [],
  internalOrder: [],
  startedAt: Date.now(),
});

/** U3a：从 agent 工具的 <subagent session_id="..." state="..."> 标签解析后台状态 */
const SUBAGENT_TAG_RE = /<subagent session_id="([^"]+)" state="(running|backgrounded)">/;

const appendToInternalOrder = (
  existing: SubAgentEventItem[],
  newItem: SubAgentEventItem
): SubAgentEventItem[] => {
  if (newItem.type === 'thinking' || newItem.type === 'text') {
    const last = existing[existing.length - 1];
    if (last && last.type === newItem.type) {
      const merged = { ...last, content: last.content + newItem.content };
      return [...existing.slice(0, -1), merged];
    }
  }
  return [...existing, newItem];
};

function foldEventMessage(type: string, event: Record<string, unknown>): string | null {
  if (type === 'session_folded' && event.trigger === 'manual') {
    return null;
  }
  if (type === 'tool_folded') {
    const abstracts = event.abstracts;
    const n = Array.isArray(abstracts) ? abstracts.length : 0;
    return n > 0 ? `上下文自动压缩：已折叠 ${n} 条较早的工具结果` : '上下文自动压缩：已折叠较早的工具结果';
  }
  return '上下文自动压缩：较早对话已折叠为摘要';
}

export function useChatNodes(): UseChatNodesReturn {
  const snapshot = useSessionStore((s: any) => s.getCurrentSnapshot());

  const handleSSEEvent = useCallback((sessionId: string, data: Record<string, unknown>) => {
    const eventType = data.type as string;
    const store = sessionStore;
    const subAgentInfoMap = store.getSubAgentInfo(sessionId);

    const updateSnap = (updater: (prev: ChatSnapshot) => ChatSnapshot) => {
      store.updateSnapshot(sessionId, updater);
    };

    const getAssistantKey = () => store.getCurrentAssistantKey(sessionId);
    const setAssistantKey = (key: string | null) => store.setCurrentAssistantKey(sessionId, key);

    switch (eventType) {
      case 'thinking': {
        const content = data.content as string;
        const subAgentId = data.sub_agent_id as string | undefined;

        if (subAgentId) {
          updateSnap(prev => {
            const subAgentKey = `subagent_${subAgentId}`;
            let existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
            const newNodes = new Map(prev.nodes);
            
            if (!existing) {
              const info = subAgentInfoMap.get(subAgentId);
              existing = {
                ...createSubAgentNode(subAgentId),
                agentType: info?.agentType || 'unknown',
                description: info?.description || '',
              };
              newNodes.set(subAgentKey, existing);
              return { 
                order: [...prev.order, subAgentKey], 
                nodes: newNodes.set(subAgentKey, { 
                  ...existing, 
                  thinking: content,
                  internalOrder: appendToInternalOrder(existing.internalOrder, { type: 'thinking', content })
                })
              };
            }
            
            newNodes.set(subAgentKey, { 
              ...existing, 
              thinking: (existing.thinking || '') + content,
              internalOrder: appendToInternalOrder(existing.internalOrder, { type: 'thinking', content })
            });
            return { order: prev.order, nodes: newNodes };
          });
        } else {
          const assistantKey = getAssistantKey();
          if (assistantKey) {
            updateSnap(prev => {
              const node = prev.nodes.get(assistantKey);
              if (!node || node.kind !== 'assistant') return prev;
              const newNodes = new Map(prev.nodes);
              newNodes.set(assistantKey, { ...node, thinking: (node.thinking || '') + content, streaming: true });
              return { order: prev.order, nodes: newNodes };
            });
          } else {
            const key = nextKey('assistant');
            setAssistantKey(key);
            updateSnap(prev => {
              const newNodes = new Map(prev.nodes);
              newNodes.set(key, {
                key,
                kind: 'assistant',
                seq: nextSeq(),
                content: '',
                thinking: content,
                streaming: true,
                timestamp: new Date().toISOString(),
                turn: data.turn as number | undefined,
                step: data.step as number | undefined,
              });
              return { order: [...prev.order, key], nodes: newNodes };
            });
          }
        }
        break;
      }

      case 'text': {
        const content = data.content as string;
        const subAgentId = data.sub_agent_id as string | undefined;

        if (subAgentId) {
          updateSnap(prev => {
            const subAgentKey = `subagent_${subAgentId}`;
            let existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
            const newNodes = new Map(prev.nodes);
            
            if (!existing) {
              const info = subAgentInfoMap.get(subAgentId);
              existing = {
                ...createSubAgentNode(subAgentId),
                agentType: info?.agentType || 'unknown',
                description: info?.description || '',
              };
              newNodes.set(subAgentKey, existing);
              return { 
                order: [...prev.order, subAgentKey], 
                nodes: newNodes.set(subAgentKey, { 
                  ...existing, 
                  text: content,
                  internalOrder: appendToInternalOrder(existing.internalOrder, { type: 'text', content })
                })
              };
            }
            
            newNodes.set(subAgentKey, { 
              ...existing, 
              text: (existing.text || '') + content,
              internalOrder: appendToInternalOrder(existing.internalOrder, { type: 'text', content })
            });
            return { order: prev.order, nodes: newNodes };
          });
        } else {
          const assistantKey = getAssistantKey();
          if (assistantKey) {
            updateSnap(prev => {
              const node = prev.nodes.get(assistantKey);
              if (!node || node.kind !== 'assistant') return prev;
              const newNodes = new Map(prev.nodes);
              newNodes.set(assistantKey, { ...node, content: node.content + content, streaming: true });
              return { order: prev.order, nodes: newNodes };
            });
          } else {
            const key = nextKey('assistant');
            setAssistantKey(key);
            updateSnap(prev => {
              const newNodes = new Map(prev.nodes);
              newNodes.set(key, {
                key,
                kind: 'assistant',
                seq: nextSeq(),
                content,
                streaming: true,
                timestamp: new Date().toISOString(),
                turn: data.turn as number | undefined,
                step: data.step as number | undefined,
              });
              return { order: [...prev.order, key], nodes: newNodes };
            });
          }
        }
        break;
      }

      case 'tool_call': {
        const callId = data.call_id as string;
        const name = data.name as string;
        const input = data.input as Record<string, unknown>;
        const subAgentId = data.sub_agent_id as string | undefined;

        if (name === 'agent') break;

        if (subAgentId) {
          updateSnap(prev => {
            const subAgentKey = `subagent_${subAgentId}`;
            let existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
            const newNodes = new Map(prev.nodes);
            
            const toolNode: ToolCallNode = {
              key: `tool_${callId}`,
              kind: 'tool-call',
              seq: nextSeq(),
              callId,
              name,
              input,
              status: 'pending',
            };
            
            if (!existing) {
              const info = subAgentInfoMap.get(subAgentId);
              existing = {
                ...createSubAgentNode(subAgentId),
                agentType: info?.agentType || 'unknown',
                description: info?.description || '',
              };
              newNodes.set(subAgentKey, existing);
              return { 
                order: [...prev.order, subAgentKey], 
                nodes: newNodes.set(subAgentKey, { 
                  ...existing, 
                  toolCalls: [toolNode],
                  internalOrder: appendToInternalOrder(existing.internalOrder, { type: 'tool_call', toolCall: toolNode })
                })
              };
            }
            
            newNodes.set(subAgentKey, { 
              ...existing, 
              toolCalls: [...existing.toolCalls, toolNode],
              internalOrder: appendToInternalOrder(existing.internalOrder, { type: 'tool_call', toolCall: toolNode })
            });
            return { order: prev.order, nodes: newNodes };
          });
        } else {
          setAssistantKey(null);
          updateSnap(prev => {
            const newNodes = new Map(prev.nodes);
            const toolKey = `tool_${callId}`;
            const toolNode: ToolCallNode = {
              key: toolKey,
              kind: 'tool-call',
              seq: nextSeq(),
              callId,
              name,
              input,
              status: 'pending',
            };
            newNodes.set(toolKey, toolNode);
            return { order: [...prev.order, toolKey], nodes: newNodes };
          });
        }
        break;
      }

      case 'tool_result': {
        const callId = data.call_id as string;
        const result = data.result as string;
        const status = (data.status as string) || 'ok';
        const durationMs = data.duration_ms as number | undefined;
        const fileSnapshot = data.snapshot as { file_path: string; old_content: string; new_content: string } | undefined;
        const subAgentId = data.sub_agent_id as string | undefined;

        if ((data.name as string) === 'agent') {
          // U3a：agent 工具不渲染工具卡，但结果标签携带后台状态——
          // state="running"（后台发起）或 "backgrounded"（前台转后台）→ 标记节点
          const m = SUBAGENT_TAG_RE.exec(result || '');
          if (m) {
            const subAgentKey = `subagent_${m[1]}`;
            updateSnap(prev => {
              const node = prev.nodes.get(subAgentKey);
              if (!node || node.kind !== 'sub-agent' || node.backgrounded) return prev;
              const newNodes = new Map(prev.nodes);
              newNodes.set(subAgentKey, { ...node, backgrounded: true });
              return { order: prev.order, nodes: newNodes };
            });
          }
          break;
        }

        const updateTool = (node: ChatNode): ChatNode => {
          if (node.kind !== 'tool-call') return node;
          return { ...node, status: status === 'ok' ? 'success' : status as 'error' | 'denied', result, durationMs, snapshot: fileSnapshot };
        };

        if (subAgentId) {
          updateSnap(prev => {
            const subAgentKey = `subagent_${subAgentId}`;
            const existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
            if (existing) {
              const newNodes = new Map(prev.nodes);
              const updatedToolCalls = existing.toolCalls.map(tc => tc.callId === callId ? updateTool(tc) as ToolCallNode : tc);
              const updatedInternalOrder = existing.internalOrder.map(item => {
                if (item.type === 'tool_call' && item.toolCall.callId === callId) {
                  return { type: 'tool_call' as const, toolCall: updateTool(item.toolCall) as ToolCallNode };
                }
                return item;
              });
              newNodes.set(subAgentKey, { ...existing, toolCalls: updatedToolCalls, internalOrder: updatedInternalOrder });
              return { order: prev.order, nodes: newNodes };
            }
            return prev;
          });
        } else {
          const toolKey = `tool_${callId}`;
          updateSnap(prev => {
            const node = prev.nodes.get(toolKey);
            if (!node) return prev;
            const newNodes = new Map(prev.nodes);
            newNodes.set(toolKey, updateTool(node));
            return { order: prev.order, nodes: newNodes };
          });
        }
        break;
      }

      case 'sub_agent/start': {
        const agentId = (data.agent_id as string) || `sa_${nextSeq()}`;
        const agentType = data.agent_type as string;
        const description = data.description as string;
        const insertAfterKey = data.insert_after_key as string | undefined;
        
        subAgentInfoMap.set(agentId, { agentType, description });
        setAssistantKey(null);
        
        updateSnap(prev => {
          const subAgentKey = `subagent_${agentId}`;
          const existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
          
          if (existing) {
            const newNodes = new Map(prev.nodes);
            newNodes.set(subAgentKey, { ...existing, agentType, description });
            return { order: prev.order, nodes: newNodes };
          }
          
          const subAgentNode: SubAgentNode = {
            key: subAgentKey,
            kind: 'sub-agent',
            seq: nextSeq(),
            agentId,
            agentType,
            description,
            status: 'running',
            toolCalls: [],
            internalOrder: [],
            startedAt: Date.now(),
          };
          const newNodes = new Map(prev.nodes);
          newNodes.set(subAgentKey, subAgentNode);
          
          let newOrder = prev.order;
          if (insertAfterKey) {
            const insertIndex = prev.order.indexOf(insertAfterKey);
            if (insertIndex >= 0) {
              newOrder = [...prev.order.slice(0, insertIndex + 1), subAgentKey, ...prev.order.slice(insertIndex + 1)];
            } else {
              newOrder = [...prev.order, subAgentKey];
            }
          } else {
            newOrder = [...prev.order, subAgentKey];
          }
          
          return { order: newOrder, nodes: newNodes };
        });
        break;
      }

      case 'sub_agent/end': {
        const agentId = data.agent_id as string;
        if (agentId) {
          const subAgentKey = `subagent_${agentId}`;
          const summary = data.summary as string | undefined;
          const durationMs = data.duration_ms as number | undefined;
          const tokens = data.tokens as number | undefined;
          const endStatus = (data.status as string) || 'completed';
          updateSnap(prev => {
            const node = prev.nodes.get(subAgentKey);
            if (!node || node.kind !== 'sub-agent') return prev;
            const newNodes = new Map(prev.nodes);
            newNodes.set(subAgentKey, { 
              ...node, 
              status: endStatus === 'completed' || endStatus === 'budget_exceeded' ? 'completed' : 'error',
              text: summary || node.text,
              durationMs: durationMs ?? node.durationMs,
              tokens: tokens ?? node.tokens,
            } as SubAgentNode);
            return { order: prev.order, nodes: newNodes };
          });
        }
        break;
      }

      case 'subagent/completed': {
        // U3a：后台子代理完成通知（synthetic 事件走统一数据流）→ 完成卡片。
        // 同一 append 已完成后端持久化+投影，前端只负责渲染
        const subId = data.sub_session_id as string;
        if (!subId) break;
        const status = (data.status as string) || 'completed';
        const text = data.text as string | undefined;
        const agentType = (data.agent_type as string) || 'unknown';
        const description = (data.description as string) || '';
        const mappedStatus = status === 'completed' || status === 'budget_exceeded' ? 'completed' : 'error';
        const subAgentKey = `subagent_${subId}`;
        updateSnap(prev => {
          const newNodes = new Map(prev.nodes);
          const existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
          if (existing) {
            newNodes.set(subAgentKey, {
              ...existing,
              status: mappedStatus,
              backgrounded: true,
              completionText: text || existing.completionText,
            });
            return { order: prev.order, nodes: newNodes };
          }
          newNodes.set(subAgentKey, {
            ...createSubAgentNode(subId),
            agentType,
            description,
            status: mappedStatus,
            backgrounded: true,
            completionText: text,
          });
          return { order: [...prev.order, subAgentKey], nodes: newNodes };
        });
        break;
      }

      case 'error': {
        const message = data.message as string;
        updateSnap(prev => {
          const newNodes = new Map(prev.nodes);
          const key = nextKey('error');
          newNodes.set(key, { key, kind: 'error', seq: nextSeq(), message });
          return { order: [...prev.order, key], nodes: newNodes };
        });
        break;
      }

      case 'session/interrupted':
      case 'system': {
        // U6：session/interrupted（启动扫描/shutdown 合成）复用 system 节点渲染
        const message = (data.message as string) || '服务已重启，上一轮执行被中断';
        updateSnap(prev => {
          const newNodes = new Map(prev.nodes);
          const key = nextKey('system');
          newNodes.set(key, { 
            key, 
            kind: 'system', 
            seq: nextSeq(), 
            message,
            timestamp: new Date().toISOString(),
          });
          return { order: [...prev.order, key], nodes: newNodes };
        });
        break;
      }

      case 'tool_folded':
      case 'session_folded': {
        const message = foldEventMessage(eventType, data);
        if (message) {
          updateSnap(prev => {
            const newNodes = new Map(prev.nodes);
            const key = nextKey('system');
            newNodes.set(key, {
              key,
              kind: 'system',
              seq: nextSeq(),
              message,
              timestamp: new Date().toISOString(),
            });
            return { order: [...prev.order, key], nodes: newNodes };
          });
        }
        break;
      }

      case 'turn/end': {
        updateSnap(prev => {
          const newNodes = new Map(prev.nodes);
          
          for (const [key, node] of newNodes) {
            if (node.kind === 'tool-call' && node.status === 'pending') {
              newNodes.set(key, { ...node, status: 'success' });
            }
            if (node.kind === 'assistant' && node.streaming) {
              newNodes.set(key, { ...node, streaming: false });
            }
          }
          
          return { order: prev.order, nodes: newNodes };
        });
        break;
      }
    }
  }, []);

  const addUserMessage = useCallback((sessionId: string, content: string, contextFiles?: string[], agent?: string, model?: string) => {
    const key = nextKey('user');
    const node: ChatNode = {
      key,
      kind: 'user',
      seq: nextSeq(),
      content,
      contextFiles,
      agent,
      model,
      timestamp: new Date().toISOString(),
    };
    sessionStore.updateSnapshot(sessionId, prev => {
      const newNodes = new Map(prev.nodes);
      newNodes.set(key, node);
      return { order: [...prev.order, key], nodes: newNodes };
    });
    sessionStore.setCurrentAssistantKey(sessionId, null);
  }, []);

  const resetNodes = useCallback((sessionId: string) => {
    sessionStore.updateSnapshot(sessionId, () => ({ order: [], nodes: new Map() }));
    sessionStore.setCurrentAssistantKey(sessionId, null);
    sessionStore.getSubAgentInfo(sessionId).clear();
  }, []);

  const loadSessionEvents = useCallback((sessionId: string, events: Array<Record<string, unknown>>) => {
    console.log('[NODES] loadSessionEvents:', { sessionId, totalEvents: events.length });
    resetNodes(sessionId);
    
    const subAgentStartEvents: Array<{ agentId: string; agentType: string; description: string; subSessionId?: string }> = [];
    
    for (const event of events) {
      const type = event.type as string;
      if (type === 'sub_agent/start') {
        subAgentStartEvents.push({
          agentId: event.agent_id as string,
          agentType: event.agent_type as string || 'unknown',
          description: event.description as string || '',
          subSessionId: event.sub_session_id as string,
        });
      }
    }
    
    let subAgentIndex = 0;
    
    for (const event of events) {
      const type = event.type as string;
      
      switch (type) {
        case 'user_message': {
          const content = event.content as string;
          if (content && content.trim()) {
            addUserMessage(sessionId, content);
          }
          break;
        }
        
        case 'assistant_message': {
          const thinking = event.thinking as string;
          const content = event.content as string;
          const toolCalls = event.tool_calls as Array<Record<string, unknown>>;
          
          if (thinking) {
            handleSSEEvent(sessionId, { type: 'thinking', content: thinking });
          }
          if (content && content.trim()) {
            handleSSEEvent(sessionId, { type: 'text', content: content });
          }
          
          if (toolCalls && toolCalls.length > 0) {
            for (const tc of toolCalls) {
              const fn = (tc as any).function || {};
              const callId = (tc as any).id || `tc_${Date.now()}_${Math.random()}`;
              let input: Record<string, unknown> = {};
              try {
                input = typeof fn.arguments === 'string' ? JSON.parse(fn.arguments) : {};
              } catch {}
              
              if (fn.name === 'agent') {
                const subAgentInfo = subAgentStartEvents[subAgentIndex];
                const subAgentId = subAgentInfo?.agentId || `sub_${Date.now()}_${subAgentIndex}`;
                
                handleSSEEvent(sessionId, {
                  type: 'sub_agent/start',
                  agent_type: subAgentInfo?.agentType || 'agent',
                  description: subAgentInfo?.description || (input.description as string) || '',
                  agent_id: subAgentId,
                });
                
                subAgentIndex++;
              } else {
                handleSSEEvent(sessionId, { type: 'tool_call', call_id: callId, name: fn.name || 'unknown', input });
              }
            }
          }
          break;
        }
        
        case 'tool_result_msg': {
          const callId = event.call_id as string;
          const content = event.content as string;
          handleSSEEvent(sessionId, { type: 'tool_result', call_id: callId, name: 'unknown', result: content || '', status: 'ok' });
          break;
        }
        
        case 'sub_agent/start': {
          break;
        }
        
        case 'sub_agent/end': {
          const agentId = event.agent_id as string;
          handleSSEEvent(sessionId, { 
            type: 'sub_agent/end', 
            agent_id: agentId,
            status: event.status,
            summary: event.summary,
            duration_ms: event.duration_ms,
            tokens: event.tokens,
          });
          break;
        }
        
        case 'subagent/completed': {
          // U3a：历史回放同样渲染完成卡片（事件即数据源）
          handleSSEEvent(sessionId, event);
          break;
        }
        
        case 'session/interrupted': {
          // U6：历史回放渲染中断提示
          handleSSEEvent(sessionId, event);
          break;
        }
        
        case 'tool_folded':
        case 'session_folded': {
          handleSSEEvent(sessionId, event);
          break;
        }

        case 'permission/request': {
          // Don't restore permission requests from history - they're only valid for active sessions
          break;
        }
        
        default:
          break;
      }
    }
    
    handleSSEEvent(sessionId, { type: 'turn/end' });
  }, [resetNodes, addUserMessage, handleSSEEvent]);

  const prependSessionEvents = useCallback((sessionId: string, events: Array<Record<string, unknown>>) => {
    console.log('[NODES] prependSessionEvents:', { sessionId, totalEvents: events.length });
    
    // Build new nodes from older events
    const newNodes: ChatNode[] = [];
    const subAgentStartEvents: Array<{ agentId: string; agentType: string; description: string }> = [];
    
    for (const event of events) {
      const type = event.type as string;
      if (type === 'sub_agent/start') {
        subAgentStartEvents.push({
          agentId: event.agent_id as string,
          agentType: event.agent_type as string || 'unknown',
          description: event.description as string || '',
        });
      }
    }
    
    let subAgentIndex = 0;
    
    for (const event of events) {
      const type = event.type as string;
      
      switch (type) {
        case 'user_message': {
          const content = event.content as string;
          if (content && content.trim()) {
            newNodes.push({
              key: nextKey('user'),
              kind: 'user',
              seq: nextSeq(),
              content,
              timestamp: new Date(event.time as number).toISOString(),
            });
          }
          break;
        }
        
        case 'assistant_message': {
          const thinking = event.thinking as string;
          const content = event.content as string;
          const toolCalls = event.tool_calls as Array<Record<string, unknown>>;
          
          // Directly create assistant node (like prependSessionEvents does)
          const assistantKey = nextKey('assistant');
          sessionStore.updateSnapshot(sessionId, prev => {
            const newNodes = new Map(prev.nodes);
            newNodes.set(assistantKey, {
              key: assistantKey,
              kind: 'assistant',
              seq: nextSeq(),
              content: content || '',
              thinking: thinking || undefined,
              streaming: false,
              timestamp: new Date(event.time as number).toISOString(),
              turn: event.turn as number | undefined,
              step: event.step as number | undefined,
              hasToolCalls: toolCalls && toolCalls.length > 0,
            });
            return { order: [...prev.order, assistantKey], nodes: newNodes };
          });
          sessionStore.setCurrentAssistantKey(sessionId, assistantKey);
          
          if (toolCalls && toolCalls.length > 0) {
            for (const tc of toolCalls) {
              const fn = (tc as any).function || {};
              const callId = (tc as any).id || `tc_${Date.now()}_${Math.random()}`;
              let input: Record<string, unknown> = {};
              try {
                input = typeof fn.arguments === 'string' ? JSON.parse(fn.arguments) : {};
              } catch {}
              
              if (fn.name === 'agent') {
                const subAgentInfo = subAgentStartEvents[subAgentIndex];
                const subAgentId = subAgentInfo?.agentId || `sub_${Date.now()}_${subAgentIndex}`;
                
                newNodes.push({
                  key: `subagent_${subAgentId}`,
                  kind: 'sub-agent',
                  seq: nextSeq(),
                  agentId: subAgentId,
                  agentType: subAgentInfo?.agentType || 'agent',
                  description: subAgentInfo?.description || (input.description as string) || '',
                  status: 'completed',
                  toolCalls: [],
                  internalOrder: [],
                });
                
                subAgentIndex++;
              } else {
                newNodes.push({
                  key: `tc_${callId}`,
                  kind: 'tool-call',
                  seq: nextSeq(),
                  callId,
                  name: fn.name || 'unknown',
                  input,
                  status: 'pending',
                });
              }
            }
          }
          break;
        }
        
        case 'tool_folded':
        case 'session_folded': {
          const foldMessage = foldEventMessage(type, event);
          if (foldMessage) {
            newNodes.push({
              key: nextKey('system'),
              kind: 'system',
              seq: nextSeq(),
              message: foldMessage,
              timestamp: new Date(event.time as number).toISOString(),
            });
          }
          break;
        }

        case 'tool_result_msg': {
          // Tool results are already included in assistant messages
          break;
        }
        
        default:
          break;
      }
    }
    
    // Prepend new nodes to existing snapshot
    if (newNodes.length > 0) {
      sessionStore.updateSnapshot(sessionId, prev => {
        const existingNodes = new Map(prev.nodes);
        const existingOrder = [...prev.order];
        
        // Add new nodes
        for (const node of newNodes) {
          existingNodes.set(node.key, node);
        }
        
        // Prepend new node keys to order
        const newKeys = newNodes.map(n => n.key);
        return { order: [...newKeys, ...existingOrder], nodes: existingNodes };
      });
    }
  }, []);

  return { snapshot, handleSSEEvent, addUserMessage, resetNodes, loadSessionEvents, prependSessionEvents };
}

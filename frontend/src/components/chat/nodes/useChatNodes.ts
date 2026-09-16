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
});

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

        if ((data.name as string) === 'agent') break;

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
          updateSnap(prev => {
            const node = prev.nodes.get(subAgentKey);
            if (!node || node.kind !== 'sub-agent') return prev;
            const newNodes = new Map(prev.nodes);
            newNodes.set(subAgentKey, { 
              ...node, 
              status: 'completed',
              text: summary || node.text,
              durationMs: durationMs ?? node.durationMs,
              tokens: tokens ?? node.tokens,
            } as SubAgentNode);
            return { order: prev.order, nodes: newNodes };
          });
        }
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

      case 'system': {
        const message = data.message as string;
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

      case 'turn/end': {
        const assistantKey = getAssistantKey();
        if (assistantKey) {
          updateSnap(prev => {
            const node = prev.nodes.get(assistantKey);
            if (!node || node.kind !== 'assistant') return prev;
            const newNodes = new Map(prev.nodes);
            newNodes.set(assistantKey, { ...node, streaming: false });
            return { order: prev.order, nodes: newNodes };
          });
        }
        break;
      }
    }
  }, []);

  const addUserMessage = useCallback((sessionId: string, content: string, contextFiles?: string[], agent?: string, model?: string, snapshotId?: string, messageId?: string) => {
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
      snapshotId,
      messageId,
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
          const snapshotId = event.snapshot_id as string | undefined;
          const messageId = event.message_id as string | undefined;
          if (content && content.trim()) {
            addUserMessage(sessionId, content, undefined, undefined, undefined, snapshotId, messageId);
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
            summary: event.summary,
            duration_ms: event.duration_ms,
            tokens: event.tokens,
          });
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
          const snapshotId = event.snapshot_id as string | undefined;
          const messageId = event.message_id as string | undefined;
          if (content && content.trim()) {
            newNodes.push({
              key: nextKey('user'),
              kind: 'user',
              seq: nextSeq(),
              content,
              timestamp: new Date(event.time as number).toISOString(),
              snapshotId,
              messageId,
            });
          }
          break;
        }
        
        case 'assistant_message': {
          const thinking = event.thinking as string;
          const content = event.content as string;
          const toolCalls = event.tool_calls as Array<Record<string, unknown>>;
          
          const assistantKey = nextKey('assistant');
          newNodes.push({
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

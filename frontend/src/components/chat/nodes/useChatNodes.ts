import { useCallback, useRef, useState } from 'react';
import type { ChatNode, ChatSnapshot, ToolCallNode, SubAgentNode, SubAgentEventItem } from './types';

interface UseChatNodesReturn {
  snapshot: ChatSnapshot;
  handleSSEEvent: (data: Record<string, unknown>) => void;
  addUserMessage: (content: string, contextFiles?: string[], agent?: string, model?: string) => void;
  resetNodes: () => void;
  loadMessages: (messages: Array<{role: string; content: string; timestamp?: string; contextFiles?: string[]; agent?: string; model?: string}>) => void;
}

const emptySnapshot: ChatSnapshot = { order: [], nodes: new Map() };

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
  const [snapshot, setSnapshot] = useState<ChatSnapshot>(emptySnapshot);
  const currentAssistantKeyRef = useRef<string | null>(null);
  const subAgentInfoRef = useRef<Map<string, { agentType: string; description: string }>>(new Map());

  const updateNode = useCallback((key: string, updater: (node: ChatNode) => ChatNode) => {
    setSnapshot(prev => {
      const node = prev.nodes.get(key);
      if (!node) return prev;
      const newNodes = new Map(prev.nodes);
      newNodes.set(key, updater(node));
      return { order: prev.order, nodes: newNodes };
    });
  }, []);

  const addNode = useCallback((node: ChatNode) => {
    setSnapshot(prev => {
      const newNodes = new Map(prev.nodes);
      newNodes.set(node.key, node);
      return { order: [...prev.order, node.key], nodes: newNodes };
    });
  }, []);

  const addUserMessage = useCallback((content: string, contextFiles?: string[], agent?: string, model?: string) => {
    const node: ChatNode = {
      key: nextKey('user'),
      kind: 'user',
      seq: nextSeq(),
      content,
      contextFiles,
      agent,
      model,
      timestamp: new Date().toISOString(),
    };
    addNode(node);
    currentAssistantKeyRef.current = null;
  }, [addNode]);

  const handleSSEEvent = useCallback((data: Record<string, unknown>) => {
    // Thinking event - update or create assistant node with thinking
    if (data.thinking !== undefined) {
      const thinkingData = data.thinking as { content: string; sub_agent_id?: string };
      const content = thinkingData.content;
      const subAgentId = thinkingData.sub_agent_id;

      if (subAgentId) {
        setSnapshot(prev => {
          const subAgentKey = `subagent_${subAgentId}`;
          let existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
          const newNodes = new Map(prev.nodes);
          
          // Create node if it doesn't exist yet
          if (!existing) {
            const info = subAgentInfoRef.current.get(subAgentId);
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
        // Main agent thinking - update current assistant or create new one
        const assistantKey = currentAssistantKeyRef.current;
        if (assistantKey) {
          updateNode(assistantKey, node => {
            if (node.kind !== 'assistant') return node;
            return { ...node, thinking: (node.thinking || '') + content, streaming: true };
          });
        } else {
          const key = nextKey('assistant');
          currentAssistantKeyRef.current = key;
          addNode({
            key,
            kind: 'assistant',
            seq: nextSeq(),
            content: '',
            thinking: content,
            streaming: true,
            timestamp: new Date().toISOString(),
          });
        }
      }
      return;
    }

    // Text chunk event
    if (data.text !== undefined) {
      const textData = data.text as { content: string; sub_agent_id?: string };
      const content = textData.content;
      const subAgentId = textData.sub_agent_id;

      if (subAgentId) {
        setSnapshot(prev => {
          const subAgentKey = `subagent_${subAgentId}`;
          let existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
          const newNodes = new Map(prev.nodes);
          
          // Create node if it doesn't exist yet
          if (!existing) {
            const info = subAgentInfoRef.current.get(subAgentId);
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
        // Main agent text - update current assistant or create new one
        const assistantKey = currentAssistantKeyRef.current;
        if (assistantKey) {
          updateNode(assistantKey, node => {
            if (node.kind !== 'assistant') return node;
            return { ...node, content: node.content + content, streaming: true };
          });
        } else {
          const key = nextKey('assistant');
          currentAssistantKeyRef.current = key;
          addNode({
            key,
            kind: 'assistant',
            seq: nextSeq(),
            content,
            streaming: true,
            timestamp: new Date().toISOString(),
          });
        }
      }
      return;
    }

    // Tool call event - creates a new node (breaks assistant chain)
    if (data.tool_call) {
      const tc = data.tool_call as { call_id: string; name: string; input: Record<string, unknown>; sub_agent_id?: string };
      const subAgentId = tc.sub_agent_id;

      if (subAgentId) {
        setSnapshot(prev => {
          const subAgentKey = `subagent_${subAgentId}`;
          let existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
          const newNodes = new Map(prev.nodes);
          
          const toolNode: ToolCallNode = {
            key: `tool_${tc.call_id}`,
            kind: 'tool-call',
            seq: nextSeq(),
            callId: tc.call_id,
            name: tc.name,
            input: tc.input,
            status: 'pending',
          };
          
          // Create node if it doesn't exist yet
          if (!existing) {
            const info = subAgentInfoRef.current.get(subAgentId);
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
        // Main agent tool call - create new tool-call node
        // Reset current assistant so next text creates a new assistant node
        currentAssistantKeyRef.current = null;
        
        const toolNode: ToolCallNode = {
          key: `tool_${tc.call_id}`,
          kind: 'tool-call',
          seq: nextSeq(),
          callId: tc.call_id,
          name: tc.name,
          input: tc.input,
          status: 'pending',
        };
        addNode(toolNode);
      }
      return;
    }

    // Tool result event
    if (data.tool_result) {
      const tr = data.tool_result as { call_id: string; name: string; result: string; status: string; duration_ms?: number; snapshot?: { file_path: string; old_content: string; new_content: string }; sub_agent_id?: string };

      const updateTool = (node: ChatNode): ChatNode => {
        if (node.kind !== 'tool-call') return node;
        return {
          ...node,
          status: tr.status === 'ok' ? 'success' : tr.status as 'error' | 'denied',
          result: tr.result,
          durationMs: tr.duration_ms,
          snapshot: tr.snapshot,
        };
      };

      if (tr.sub_agent_id) {
        setSnapshot(prev => {
          const subAgentKey = `subagent_${tr.sub_agent_id}`;
          const existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
          if (existing) {
            const newNodes = new Map(prev.nodes);
            const updatedToolCalls = existing.toolCalls.map(tc => tc.callId === tr.call_id ? updateTool(tc) as ToolCallNode : tc);
            const updatedInternalOrder = existing.internalOrder.map(item => {
              if (item.type === 'tool_call' && item.toolCall.callId === tr.call_id) {
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
        const toolKey = `tool_${tr.call_id}`;
        updateNode(toolKey, updateTool);
      }
      return;
    }

    // Sub-agent start event - store info for later use
    if (data.sub_agent_start) {
      const sa = data.sub_agent_start as { agent_type: string; description: string; agent_id?: string };
      const agentId = sa.agent_id || `sa_${nextSeq()}`;
      
      // Store info for on-demand node creation
      subAgentInfoRef.current.set(agentId, {
        agentType: sa.agent_type,
        description: sa.description,
      });
      
      // Reset current assistant so sub-agent appears as separate block
      currentAssistantKeyRef.current = null;
      
      // Check if node already exists (created on-demand)
      setSnapshot(prev => {
        const subAgentKey = `subagent_${agentId}`;
        const existing = prev.nodes.get(subAgentKey) as SubAgentNode | undefined;
        
        if (existing) {
          // Update existing placeholder node with real info
          const newNodes = new Map(prev.nodes);
          newNodes.set(subAgentKey, {
            ...existing,
            agentType: sa.agent_type,
            description: sa.description,
          });
          return { order: prev.order, nodes: newNodes };
        }
        
        // Create new node
        const subAgentNode: SubAgentNode = {
          key: subAgentKey,
          kind: 'sub-agent',
          seq: nextSeq(),
          agentId,
          agentType: sa.agent_type,
          description: sa.description,
          status: 'running',
          toolCalls: [],
          internalOrder: [],
        };
        const newNodes = new Map(prev.nodes);
        newNodes.set(subAgentKey, subAgentNode);
        return { order: [...prev.order, subAgentKey], nodes: newNodes };
      });
      return;
    }

    // Sub-agent end event
    if (data.sub_agent_end) {
      const sa = data.sub_agent_end as { agent_type: string; agent_id?: string };
      const agentId = sa.agent_id;
      if (agentId) {
        const subAgentKey = `subagent_${agentId}`;
        updateNode(subAgentKey, node => ({
          ...node,
          kind: 'sub-agent',
          status: 'completed',
        } as ChatNode));
      }
      return;
    }

    // Error event
    if (data.error) {
      const errorData = data.error as { message: string };
      addNode({
        key: nextKey('error'),
        kind: 'error',
        seq: nextSeq(),
        message: errorData.message,
      });
      return;
    }

    // Done event - finalize streaming
    if (data.done) {
      const assistantKey = currentAssistantKeyRef.current;
      if (assistantKey) {
        updateNode(assistantKey, node => {
          if (node.kind !== 'assistant') return node;
          return { ...node, streaming: false };
        });
      }
      return;
    }
  }, [addNode, updateNode]);

  const resetNodes = useCallback(() => {
    setSnapshot(emptySnapshot);
    currentAssistantKeyRef.current = null;
    subAgentInfoRef.current.clear();
  }, []);

  const loadMessages = useCallback((messages: Array<{role: string; content: string; timestamp?: string; contextFiles?: string[]; agent?: string; model?: string}>) => {
    resetNodes();
    setSnapshot(prev => {
      const newNodes = new Map(prev.nodes);
      const newOrder = [...prev.order];
      
      for (const msg of messages) {
        if (msg.role === 'user') {
          const key = nextKey('user');
          newNodes.set(key, {
            key,
            kind: 'user',
            seq: nextSeq(),
            content: msg.content,
            contextFiles: msg.contextFiles,
            agent: msg.agent,
            model: msg.model,
            timestamp: msg.timestamp || new Date().toISOString(),
          });
          newOrder.push(key);
        } else if (msg.role === 'assistant') {
          // Parse thinking and content
          let thinking = '';
          let content = msg.content;
          const thinkingMatch = msg.content.match(/<thinking>([\s\S]*?)<\/thinking>/);
          if (thinkingMatch) {
            thinking = thinkingMatch[1].trim();
            content = msg.content.replace(/<thinking>[\s\S]*?<\/thinking>/, '').trim();
          }
          
          const key = nextKey('assistant');
          newNodes.set(key, {
            key,
            kind: 'assistant',
            seq: nextSeq(),
            content,
            thinking: thinking || undefined,
            streaming: false,
            timestamp: msg.timestamp || new Date().toISOString(),
          });
          newOrder.push(key);
        }
      }
      
      return { order: newOrder, nodes: newNodes };
    });
  }, [resetNodes]);

  return { snapshot, handleSSEEvent, addUserMessage, resetNodes, loadMessages };
}

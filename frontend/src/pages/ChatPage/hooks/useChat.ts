import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import {
  fetchConfig, AppConfig,
  updateSessionName,
  compactSession, updatePermissionMode,
  forkSession, respondToPermission, truncateSession
} from '../../../api/client';
import { useChatNodes } from '../../../components/chat/nodes';
import type { UserNode } from '../../../components/chat/nodes/types';
import { sessionStore, wsManager, eventRouter, useSessionStore } from '../../../store';
import type { PermissionRequest, FileSnapshot } from '../../../store/SessionStore';
import { logger } from '../../../utils/logger';

const EMPTY_FILE_SNAPSHOTS: FileSnapshot[] = [];

export function useChat() {
  const [inputValue, setInputValue] = useState('');
  const [contextFiles, setContextFiles] = useState<string[]>([]);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string>('build');
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const currentSessionIdRef = useRef<string | null>(null);
  const [currentProject, setCurrentProject] = useState<string | null>(null);
  const [currentCwd, setCurrentCwd] = useState<string | null>(null);
  const [isWaitingResponse, setIsWaitingResponse] = useState(false);
  const [isLoadingSession, setIsLoadingSession] = useState(false);
  const [fileTreeRefreshTrigger, setFileTreeRefreshTrigger] = useState(0);
  const pendingSessionNameRef = useRef<string | null>(null);
  
  const [permissionMode, setPermissionMode] = useState<'default' | 'acceptEdits' | 'bypassPermissions'>('bypassPermissions');
  const [contextTotal, setContextTotal] = useState(128000);
  
  const [sessionRefreshTrigger, setSessionRefreshTrigger] = useState(0);
  const [pendingSteerMessages, setPendingSteerMessages] = useState<Array<{content: string, contextFiles: string[], model?: string}>>([]);
  const autoSendRef = useRef(false);
  
  const sessionId = currentSessionId ?? sessionStore.getCurrentId();
  const isStreaming = useSessionStore(() => sessionStore.get(sessionId ?? '')?.projections?.running ?? false);
  const pendingPermission = useSessionStore(() => sessionId ? sessionStore.getPendingPermission(sessionId) : undefined);
  const goalState = useSessionStore(() => sessionId ? sessionStore.getGoalState(sessionId) : undefined);
  const fileSnapshots = useSessionStore(() => sessionId ? sessionStore.getFileSnapshots(sessionId) : EMPTY_FILE_SNAPSHOTS);
  const contextUsed = useSessionStore(() => sessionId ? sessionStore.getContextUsed(sessionId) : 0);
  
  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);
  
  const { snapshot: chatSnapshot, handleSSEEvent: handleNodeEvent, addUserMessage, resetNodes, loadSessionEvents, prependSessionEvents } = useChatNodes();
  
  // 从 chatSnapshot 派生 user 节点列表
  const userNodes = useMemo(() => {
    const nodes: UserNode[] = [];
    for (const key of chatSnapshot.order) {
      const node = chatSnapshot.nodes.get(key);
      if (node && node.kind === 'user') {
        nodes.push(node as UserNode);
      }
    }
    return nodes;
  }, [chatSnapshot]);

  // Initialize WebSocket connection and event router
  useEffect(() => {
    wsManager.connect();
    eventRouter.start();
    
    const isWaitingResponseRef = { current: isWaitingResponse };
    isWaitingResponseRef.current = isWaitingResponse;
    
    // Subscribe to WebSocket events
    const unsubscribe = eventRouter.subscribe((event) => {
      const eventType = event.type;
      const eventSessionId = event.session_id;
      
      // Only process events for current session
      // Allow session/created events to set the current session
      if (eventType === 'session/created') {
        const newSessionId = event.session_id;
        logger.info('[WS] session/created:', newSessionId);
        if (newSessionId) {
          wsManager.subscribeSession(newSessionId);
          sessionStore.migrateNodes('__pending__', newSessionId);
          sessionStore.setCurrentId(newSessionId);
          currentSessionIdRef.current = newSessionId;
          setCurrentSessionId(newSessionId);
          setSessionRefreshTrigger(prev => prev + 1);
        }
        return;
      }
      
      if (eventType === 'session/title') {
        const titleSessionId = event.session_id;
        const title = event.title;
        if (titleSessionId && title) {
          sessionStore.updateProjections(titleSessionId, { title });
          updateSessionName(titleSessionId, title).catch(err => {
            console.error('Failed to update session name:', err);
          });
          setSessionRefreshTrigger(prev => prev + 1);
        }
        return;
      }
      
      if (!eventSessionId) return;
      
      logger.debug('[EVENT] Processing event:', eventType, eventSessionId);
      
      const isCurrentSession = eventSessionId === currentSessionIdRef.current;
      
      // 如果是子智能体事件，关联到主 session
      const subAgentId = event.sub_agent_id as string | undefined;
      const targetSessionId = (subAgentId && currentSessionIdRef.current) 
        ? currentSessionIdRef.current 
        : eventSessionId;
      
      if (isCurrentSession && isWaitingResponseRef.current && ['text', 'thinking', 'tool_call'].includes(eventType)) {
        setIsWaitingResponse(false);
      }
      
      if (eventType === 'stats') {
        sessionStore.setContextStats(targetSessionId, event.last_input_token_count || 0, event.context_window || 128000);
      }
      
      if (eventType === 'context/compacted') {
        // 更新 token 计数
        if (event.last_input_token_count !== undefined) {
          sessionStore.setContextStats(targetSessionId, event.last_input_token_count, event.context_window || 128000);
        }
        // 显示压缩提示
        handleNodeEvent(targetSessionId, {
          type: 'system',
          message: event.message || '上下文已压缩',
        });
      }
      
      handleNodeEvent(targetSessionId, event);
      
      if (eventType === 'tool_result' && event.snapshot) {
        const snap = event.snapshot;
        if (snap.old_content !== undefined && snap.new_content !== undefined) {
          sessionStore.addFileSnapshot(eventSessionId, {
            file_path: snap.file_path,
            is_new: snap.is_new,
            old_content: snap.old_content,
            new_content: snap.new_content,
          });
        }
      }
      
      if (eventType === 'tool_result' && ['write_file', 'edit_file', 'run_shell'].includes(event.name)) {
        setFileTreeRefreshTrigger(prev => prev + 1);
      }
      
      if (eventType === 'permission/request') {
        const permReq: PermissionRequest = {
          rpc_id: event.rpc_id,
          request_id: event.request_id || event.rpc_id,
          command: event.command,
          tool_name: event.tool_name,
          message: event.message || '',
          sub_agent_id: event.sub_agent_id,
        };
        sessionStore.setPendingPermission(eventSessionId, permReq);
      }
      
      if (eventType === 'goal/criteria') {
        sessionStore.setGoalState(eventSessionId, {
          active: false,
          goal: event.goal as string,
          criteria: event.criteria as string[],
          iteration: 0,
          maxIterations: 10,
          status: 'idle',
        });
      }
      
      if (eventType === 'goal/start') {
        sessionStore.setGoalState(eventSessionId, {
          active: true,
          goal: event.goal as string,
          criteria: event.criteria as string[],
          iteration: 0,
          maxIterations: 10,
          status: 'running',
        });
      }
      
      if (eventType === 'goal/progress') {
        const current = sessionStore.getGoalState(eventSessionId);
        if (current) {
          sessionStore.setGoalState(eventSessionId, {
            ...current,
            iteration: event.iteration,
            status: event.status || 'running',
          });
        }
      }
      
      if (eventType === 'goal/complete') {
        const current = sessionStore.getGoalState(eventSessionId);
        if (current) {
          sessionStore.setGoalState(eventSessionId, {
            ...current,
            status: event.status,
          });
        }
      }
      
      if (eventType === 'turn/start') {
        sessionStore.updateProjections(eventSessionId, { running: true });
      }
      
      if (eventType === 'turn/end') {
        const doneSessionId = eventSessionId;
        const turnName = event.name as string | undefined;
        logger.info('[WS] turn/end:', { sessionId: doneSessionId, subAgentId: event.sub_agent_id, name: turnName });
        sessionStore.updateProjections(doneSessionId, {
          running: false,
          updatedAt: Date.now(),
        });
        if (turnName) {
          sessionStore.updateProjections(doneSessionId, { title: turnName });
          updateSessionName(doneSessionId, turnName).catch(err => {
            console.error('Failed to update session name:', err);
          });
        }
        if (isCurrentSession) {
          currentSessionIdRef.current = doneSessionId;
          setCurrentSessionId(doneSessionId);
          setSessionRefreshTrigger(prev => prev + 1);
          setIsWaitingResponse(false);
        }
      }
    });
    
    return () => {
      unsubscribe();
      // Don't disconnect WebSocketManager on cleanup - it's a global singleton
      // React.StrictMode will cause this effect to run twice, and disconnecting
      // would reset the connecting flag, causing duplicate connections
    };
  }, [handleNodeEvent]);

  useEffect(() => {
    fetchConfig().then(cfg => {
      setConfig(cfg);
      const endpoints = Object.values(cfg.endpoints);
      if (endpoints.length > 0 && !selectedModel) {
        const firstEndpoint = endpoints[0];
        setSelectedModel(firstEndpoint.model);
        setContextTotal(firstEndpoint.context_window || 128000);
      }
    }).catch(console.error);
    
    const lastCwd = localStorage.getItem('lastCwd');
    if (lastCwd) {
      setCurrentCwd(lastCwd);
      setCurrentProject(lastCwd.split('/').pop() || lastCwd);
    }
    
    // 恢复上次的 session
    const lastSessionId = localStorage.getItem('lastSessionId');
    if (lastSessionId) {
      logger.info('[INIT] restoring session:', lastSessionId);
      handleSessionSelect(lastSessionId);
    }
  }, []);

  useEffect(() => {
    if (config && selectedModel) {
      const endpoint = Object.values(config.endpoints).find(e => e.model === selectedModel);
      if (endpoint?.context_window) {
        setContextTotal(endpoint.context_window);
      }
    }
  }, [config, selectedModel]);

  const handleAddToChat = (path: string) => {
    if (!contextFiles.includes(path)) {
      setContextFiles([...contextFiles, path]);
    }
  };

  const handleRemoveContext = (path: string) => {
    setContextFiles(contextFiles.filter(f => f !== path));
  };

  const handleSessionSelect = async (sessionId: string) => {
    logger.info('[SESSION] handleSessionSelect:', sessionId);
    
    // 保存当前 session 到 localStorage
    localStorage.setItem('lastSessionId', sessionId);
    
    // Switch session in store
    sessionStore.select(sessionId);
    
    // Update ref immediately
    currentSessionIdRef.current = sessionId;
    setCurrentSessionId(sessionId);
    
    // Reset UI state
    resetNodes(sessionId);
    setIsLoadingSession(true);
    pendingSessionNameRef.current = null;
    
    // Load session data with lazy loading
    try {
      // Load session metadata
      const sessionResponse = await fetch(`/api/sessions/${sessionId}`);
      
      // Check if session changed during fetch
      if (currentSessionIdRef.current !== sessionId) {
        logger.info('[SESSION] session changed during metadata fetch, aborting');
        return;
      }
      
      if (sessionResponse.ok) {
        const data = await sessionResponse.json();
        const metadata = data.metadata || {};
        
        // Update projections
        sessionStore.updateProjections(sessionId, {
          title: metadata.name,
          cwd: metadata.cwd,
          updatedAt: Date.now(),
        });
        
        if (metadata.cwd) {
          setCurrentProject(metadata.cwd.split('/').pop() || metadata.cwd);
          setCurrentCwd(metadata.cwd);
          localStorage.setItem('lastCwd', metadata.cwd);
        }
      }
      
      // Load recent events with pagination
      const eventsResponse = await fetch(`/api/sessions/${sessionId}/events?limit=50`);
      
      // Check if session changed during fetch
      if (currentSessionIdRef.current !== sessionId) {
        logger.info('[SESSION] session changed during events fetch, aborting');
        return;
      }
      
      if (eventsResponse.ok) {
        const { events, has_more, base_seq, total_count } = await eventsResponse.json();
        logger.info('[SESSION] loaded:', {
          id: sessionId,
          eventCount: events.length,
          totalCount: total_count,
          hasMore: has_more,
          baseSeq: base_seq,
        });
        
        // Restore context stats from events
        for (let i = events.length - 1; i >= 0; i--) {
          const e = events[i];
          if (e.type === 'stats' && e.last_input_token_count) {
            sessionStore.setContextStats(sessionId, e.last_input_token_count, e.context_window || 128000);
            break;
          }
        }
        
        // Update pagination state
        const lastSeq = events.length > 0 ? Math.max(...events.map((e: any) => e.seq ?? 0)) : -1;
        sessionStore.setPagination(sessionId, has_more, base_seq, lastSeq);
        
        // Load events into nodes
        loadSessionEvents(sessionId, events);
      }
    } catch (err) {
      console.error('Failed to load session:', err);
    } finally {
      // Only reset loading state if this is still the current session
      if (currentSessionIdRef.current === sessionId) {
        setIsLoadingSession(false);
      }
    }
  };

  const handleLoadMoreEvents = async () => {
    const sessionId = currentSessionIdRef.current;
    if (!sessionId) return false;
    
    const state = sessionStore.get(sessionId);
    if (!state || !state.hasMore) return false;
    
    try {
      const response = await fetch(`/api/sessions/${sessionId}/events?before=${state.baseSeq}&limit=50`);
      
      if (!response.ok) {
        console.error(`[SESSION] loadMore failed: ${response.status}`);
        return false;
      }
      
      const { events, has_more, base_seq } = await response.json();
      
      if (events && events.length > 0) {
        logger.info('[SESSION] loaded more events:', { count: events.length, hasMore: has_more });
        
        // Prepend events to existing snapshot
        prependSessionEvents(sessionId, events);
        
        // Update pagination state
        sessionStore.setPagination(sessionId, has_more, base_seq, state.lastSeq);
        return true;
      }
      
      return false;
    } catch (err) {
      console.error('[SESSION] loadMore error:', err);
      return false;
    }
  };

  const handleNewSession = (cwd?: string) => {
    logger.info('[SESSION] handleNewSession, cwd:', cwd);
    const projectCwd = cwd || currentCwd || localStorage.getItem('lastCwd') || '';
    if (!projectCwd) {
      alert('Please select a project first');
      return;
    }
    
    // 清除 lastSessionId，表示新建 session
    localStorage.removeItem('lastSessionId');
    
    // Reset streaming state
    setIsWaitingResponse(false);
    
    // Create new session in store (sets currentSessionId to '__pending__')
    sessionStore.create();
    
    // Get the pending session ID from store
    const pendingId = sessionStore.getCurrentId();
    
    // Update ref and state to match store
    currentSessionIdRef.current = pendingId;
    setCurrentSessionId(pendingId);
    
    // Reset UI state
    setInputValue('');
    setContextFiles([]);
    setCurrentProject(projectCwd.split('/').pop() || projectCwd);
    setCurrentCwd(projectCwd);
    localStorage.setItem('lastCwd', projectCwd);
    pendingSessionNameRef.current = null;
  };

  const handleSendMessage = async () => {
    if (!inputValue.trim()) return;
    if (!currentCwd) {
      alert('Please select a project first');
      return;
    }
    
    // Auto-create pending session if needed
    if (!currentSessionIdRef.current) {
      sessionStore.create();
      const pendingId = sessionStore.getCurrentId();
      currentSessionIdRef.current = pendingId;
      setCurrentSessionId(pendingId);
    }
    
    if (isStreaming) {
      logger.info('[MSG] queued as steer:', inputValue.trim().slice(0, 30));
      setPendingSteerMessages(prev => [...prev, {
        content: inputValue.trim(),
        contextFiles: [...contextFiles],
        model: selectedModel || undefined,
      }]);
      setInputValue('');
      setContextFiles([]);
      return;
    }
    
    const isFirstMessage = userNodes.length === 0;
    const userMessageContent = inputValue.trim();
    logger.info('[MSG] send:', { content: userMessageContent.slice(0, 30), isFirstMessage, sessionId: currentSessionIdRef.current });
    
    // For new sessions, use a pending session ID until the real one is created
    const nodeSessionId = currentSessionIdRef.current || '__pending__';
    addUserMessage(nodeSessionId, userMessageContent, contextFiles.length > 0 ? contextFiles : undefined, undefined, selectedModel || undefined);
    setInputValue('');
    setContextFiles([]);

    if (isFirstMessage && !currentSessionId) {
      pendingSessionNameRef.current = userMessageContent;
    }

    sessionStore.updateProjections(nodeSessionId, { running: true });
    setIsWaitingResponse(true);

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: userMessageContent,
          session_id: currentSessionIdRef.current === '__pending__' ? null : currentSessionIdRef.current,
          context_files: contextFiles.length > 0 ? contextFiles : undefined,
          agent: selectedAgent,
          model: selectedModel || undefined,
          permission_mode: permissionMode,
          cwd: currentCwd!,
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const result = await response.json();
      
      // If new session, update session_id
      if (result.session_id && (!currentSessionIdRef.current || currentSessionIdRef.current === '__pending__')) {
        sessionStore.setCurrentId(result.session_id);
        currentSessionIdRef.current = result.session_id;
        setCurrentSessionId(result.session_id);
      }
      
      // 总是触发侧边栏刷新，确保 session 立刻出现
      setSessionRefreshTrigger(prev => prev + 1);
      
    } catch (error) {
      console.error('Send message error:', error);
      sessionStore.updateProjections(nodeSessionId, { running: false });
      setIsWaitingResponse(false);
    }
  };

  const handleStopStreaming = useCallback(async () => {
    logger.info('[ABORT] handleStopStreaming called, currentSessionId:', currentSessionId, 'currentSessionIdRef:', currentSessionIdRef.current);
    const sessionId = currentSessionId || currentSessionIdRef.current;
    if (sessionId) {
      try {
        logger.info('[ABORT] sending abort via WebSocket:', sessionId);
        wsManager.send({ type: 'abort', session_id: sessionId });
        logger.info('[ABORT] abort sent');
      } catch (err) {
        console.error('Failed to abort session:', err);
      }
    } else {
      logger.info('[ABORT] no session to abort');
    }
    
    if (sessionId) {
      sessionStore.updateProjections(sessionId, { running: false });
    }
    setIsWaitingResponse(false);
  }, [currentSessionId]);

  // Send pending steer messages after streaming ends (one by one)
  useEffect(() => {
    if (!isStreaming && pendingSteerMessages.length > 0) {
      const [steerMsg, ...rest] = pendingSteerMessages;
      setPendingSteerMessages(rest);
      
      // Set input value and context files
      setInputValue(steerMsg.content);
      setContextFiles(steerMsg.contextFiles);
      if (steerMsg.model) {
        setSelectedModel(steerMsg.model);
      }
      
      // Mark for auto-send after state updates
      autoSendRef.current = true;
    }
  }, [isStreaming, pendingSteerMessages]);
  
  // Check for auto-send when input value changes
  useEffect(() => {
    if (autoSendRef.current && inputValue && !isStreaming) {
      autoSendRef.current = false;
      // Use setTimeout to ensure state is updated
      setTimeout(() => {
        handleSendMessage();
      }, 0);
    }
  }, [inputValue, isStreaming]);

  const [isCompacting, setIsCompacting] = useState(false);
  
  const handleCompactSession = useCallback(async () => {
    if (!currentSessionId || isCompacting) return;
    try {
      setIsCompacting(true);
      const result = await compactSession(currentSessionId);
      if (result.success) {
        alert('上下文压缩成功');
      } else {
        alert('压缩失败: ' + result.message);
      }
    } catch (err) {
      console.error('Failed to compact session:', err);
      alert('上下文压缩失败');
    } finally {
      setIsCompacting(false);
    }
  }, [currentSessionId, isCompacting]);

  const handleCyclePermissionMode = useCallback(async () => {
    const modes: Array<'default' | 'acceptEdits' | 'bypassPermissions'> = ['default', 'acceptEdits', 'bypassPermissions'];
    const currentIndex = modes.indexOf(permissionMode);
    const nextMode = modes[(currentIndex + 1) % modes.length];
    setPermissionMode(nextMode);
    
    if (currentSessionId) {
      try {
        await updatePermissionMode(currentSessionId, nextMode);
      } catch (err) {
        console.error('Failed to update permission mode:', err);
      }
    }
  }, [currentSessionId, permissionMode]);

  const handleForkSession = useCallback(async () => {
    if (!currentSessionId) return;
    try {
      const result = await forkSession(currentSessionId);
      if (result.new_session_id) {
        sessionStorage.removeItem('sessions');
        setSessionRefreshTrigger(prev => prev + 1);
        await handleSessionSelect(result.new_session_id);
      }
    } catch (err) {
      console.error('Failed to fork session:', err);
    }
  }, [currentSessionId, handleSessionSelect]);

  const handleForkAtPoint = useCallback(async (userMessageIndex: number) => {
    if (!currentSessionId) return;
    try {
      logger.info('[FORK] userMessageIndex:', userMessageIndex);
      logger.info('[FORK] userNodes:', userNodes.map((n, i) => ({ index: i, key: n.key, content: n.content.slice(0, 30) })));
      
      // userMessageIndex is the index of the user message before the click point
      // fork keeps messages before this index (not including this message)
      const keep_user_messages = userMessageIndex;
      logger.info('[FORK] keep_user_messages:', keep_user_messages);
      
      // Use atomic fork API (fork + truncate in one step)
      const result = await forkSession(currentSessionId, { keep_user_messages });
      logger.info('[FORK] forkSession result:', result);
      
      if (result.new_session_id) {
        sessionStorage.removeItem('sessions');
        setSessionRefreshTrigger(prev => prev + 1);
        await handleSessionSelect(result.new_session_id);
      }
    } catch (err) {
      console.error('Failed to fork at point:', err);
    }
  }, [currentSessionId, userNodes, handleSessionSelect]);

  const handleEditMessage = useCallback(async (node: UserNode, restoreFiles: boolean) => {
    logger.info('[EDIT] node:', { key: node.key, content: node.content.slice(0, 30) });
    logger.info('[EDIT] userNodes:', userNodes.map((n, i) => ({ index: i, key: n.key, content: n.content.slice(0, 30) })));
    
    setInputValue(node.content);
    setContextFiles(node.contextFiles || []);
    setSelectedModel(node.model || '');
    
    const nodeIndex = userNodes.findIndex(n => n.key === node.key);
    logger.info('[EDIT] nodeIndex:', nodeIndex);
    
    if (currentSessionId) {
      try {
        // Always use truncate, keeping messages before nodeIndex
        logger.info('[EDIT] truncate to:', nodeIndex, 'restoreFiles:', restoreFiles);
        await truncateSession(currentSessionId, nodeIndex);
        
        // If file restore is needed, call rewind (but this may not be accurate)
        if (restoreFiles) {
          logger.info('[EDIT] restoreFiles requested, but using truncate only');
        }
        
        // Reload session after truncate to rebuild all nodes
        await handleSessionSelect(currentSessionId);
      } catch (err) {
        console.error('Failed to edit session:', err);
      }
    }
  }, [userNodes, currentSessionId, handleSessionSelect]);

  const handlePermissionApprove = useCallback(async () => {
    if (!pendingPermission || !currentSessionId) return;
    try {
      await respondToPermission(currentSessionId, pendingPermission.request_id, true);
      sessionStore.setPendingPermission(currentSessionId, undefined);
    } catch (err) {
      console.error('Failed to approve permission:', err);
    }
  }, [pendingPermission, currentSessionId]);

  const handlePermissionDeny = useCallback(async () => {
    if (!pendingPermission || !currentSessionId) return;
    try {
      await respondToPermission(currentSessionId, pendingPermission.request_id, false);
      sessionStore.setPendingPermission(currentSessionId, undefined);
    } catch (err) {
      console.error('Failed to deny permission:', err);
    }
  }, [pendingPermission, currentSessionId]);

  const handleAcceptFile = useCallback((filePath: string) => {
    if (!currentSessionId) return;
    const snaps = sessionStore.getFileSnapshots(currentSessionId).filter(s => s.file_path !== filePath);
    sessionStore.setFileSnapshots(currentSessionId, snaps);
  }, [currentSessionId]);

  const handleRejectFile = useCallback(async (filePath: string) => {
    if (!currentSessionId) return;
    const snap = fileSnapshots.find(s => s.file_path === filePath);
    if (!snap) return;
    try {
      const res = await fetch('/api/revert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: filePath, old_content: snap.old_content }),
      });
      if (res.ok) {
        const snaps = sessionStore.getFileSnapshots(currentSessionId).filter(s => s.file_path !== filePath);
        sessionStore.setFileSnapshots(currentSessionId, snaps);
        setFileTreeRefreshTrigger(prev => prev + 1);
      }
    } catch (err) {
      console.error('Failed to revert file:', err);
    }
  }, [fileSnapshots, currentSessionId]);

  const handleAcceptAll = useCallback(() => {
    if (!currentSessionId) return;
    sessionStore.setFileSnapshots(currentSessionId, []);
  }, [currentSessionId]);

  const handleConfirmGoal = useCallback(async () => {
    if (!currentSessionId) return;
    // Send /goal confirm to start the goal loop
    setInputValue('/goal confirm');
    // Trigger send after state update
    setTimeout(() => {
      handleSendMessage();
    }, 0);
  }, [currentSessionId]);

  const handleCancelGoal = useCallback(() => {
    if (!currentSessionId) return;
    sessionStore.setGoalState(currentSessionId, undefined);
  }, [currentSessionId]);

  return {
    // State
    inputValue,
    setInputValue,
    contextFiles,
    config,
    selectedAgent,
    setSelectedAgent,
    selectedModel,
    setSelectedModel,
    currentSessionId,
    currentProject,
    currentCwd,
    isStreaming,
    isLoadingSession,
    isWaitingResponse,
    isCompacting,
    fileSnapshots,
    fileTreeRefreshTrigger,
    permissionMode,
    contextUsed,
    contextTotal,
    pendingPermission,
    sessionRefreshTrigger,
    chatSnapshot,
    pendingSteerMessages,
    setPendingSteerMessages,
    goalState,
    
    // Handlers
    handleAddToChat,
    handleRemoveContext,
    handleSessionSelect,
    handleNewSession,
    handleSendMessage,
    handleStopStreaming,
    handleCompactSession,
    handleCyclePermissionMode,
    handleForkSession,
    handleForkAtPoint,
    handleEditMessage,
    handlePermissionApprove,
    handlePermissionDeny,
    handleAcceptFile,
    handleRejectFile,
    handleAcceptAll,
    handleNodeEvent,
    handleConfirmGoal,
    handleCancelGoal,
    handleLoadMoreEvents,
  };
}

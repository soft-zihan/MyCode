import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  fetchConfig, AppConfig,
  compactSession, updatePermissionMode,
  forkSession, respondToPermission, respondToQuestion,
  stageRewind, commitRewind,
  fetchSessionSummary, steerSession,
  fetchTasks as fetchTasksApi,
  DEFAULT_CONTEXT_WINDOW,
} from '../../../api/client';
import type { RewindPlan } from '../../../api/client';
import { useChatNodes } from '../../../components/chat/nodes';
import type { UserNode } from '../../../components/chat/nodes/types';
import { sessionStore, wsManager, eventRouter, useSessionStore } from '../../../store';
import type { PermissionRequest, QuestionRequest, TaskItem, FileSnapshot } from '../../../store/SessionStore';
import { logger } from '../../../utils/logger';

const EMPTY_FILE_SNAPSHOTS: FileSnapshot[] = [];
const EMPTY_TASKS: TaskItem[] = [];
const EMPTY_STATS = { inputTokens: 0, outputTokens: 0, cachedTokens: 0 };

const CHARS_PER_TOKEN = 4;

function computeBreakdownFromStats(event: Record<string, any>): Record<string, any> | null {
  const actualInputTokens = event.input_tokens || 0;
  const systemChars = event.system_chars || 0;
  const userChars = event.user_chars || 0;
  const assistantChars = event.assistant_chars || 0;
  const toolResultChars = event.tool_result_chars || 0;
  
  if (systemChars === 0 && toolResultChars === 0 && userChars === 0 && assistantChars === 0) {
    return null;
  }
  
  const systemBaseChars = event.system_base_chars || 0;
  const systemClaudeMdChars = event.system_claude_md_chars || 0;
  const systemSkillsChars = event.system_skills_chars || 0;
  const systemWikiChars = event.system_wiki_chars || 0;
  const systemWorkspaceChars = event.system_workspace_chars || 0;
  const planModeChars = event.plan_mode_chars || 0;
  
  const totalChars = systemChars + userChars + assistantChars + toolResultChars;
  
  let userTokens: number, assistantTokens: number, toolTokens: number;
  let basePromptTokens: number, claudeMdTokens: number, skillsTokens: number;
  let wikiTokens: number, planModeTokens: number;
  
  if (actualInputTokens > 0 && totalChars > 0) {
    const scale = actualInputTokens / (totalChars / CHARS_PER_TOKEN);
    userTokens = Math.round((userChars / CHARS_PER_TOKEN) * scale);
    assistantTokens = Math.round((assistantChars / CHARS_PER_TOKEN) * scale);
    toolTokens = Math.round((toolResultChars / CHARS_PER_TOKEN) * scale);
    basePromptTokens = Math.round(((systemBaseChars + systemWorkspaceChars) / CHARS_PER_TOKEN) * scale);
    claudeMdTokens = Math.round((systemClaudeMdChars / CHARS_PER_TOKEN) * scale);
    skillsTokens = Math.round((systemSkillsChars / CHARS_PER_TOKEN) * scale);
    wikiTokens = Math.round((systemWikiChars / CHARS_PER_TOKEN) * scale);
    planModeTokens = Math.round((planModeChars / CHARS_PER_TOKEN) * scale);
  } else {
    userTokens = Math.round(userChars / CHARS_PER_TOKEN);
    assistantTokens = Math.round(assistantChars / CHARS_PER_TOKEN);
    toolTokens = Math.round(toolResultChars / CHARS_PER_TOKEN);
    basePromptTokens = Math.round((systemBaseChars + systemWorkspaceChars) / CHARS_PER_TOKEN);
    claudeMdTokens = Math.round(systemClaudeMdChars / CHARS_PER_TOKEN);
    skillsTokens = Math.round(systemSkillsChars / CHARS_PER_TOKEN);
    wikiTokens = Math.round(systemWikiChars / CHARS_PER_TOKEN);
    planModeTokens = Math.round(planModeChars / CHARS_PER_TOKEN);
  }
  
  const messagesTokens = userTokens + assistantTokens + toolTokens;
  
  const toolResultByNameChars: Record<string, number> = event.tool_result_by_name || {};
  const toolResultByName: Record<string, number> = {};
  if (toolResultByNameChars && toolTokens > 0) {
    const totalToolChars = Object.values(toolResultByNameChars).reduce((sum, c) => sum + c, 0);
    if (totalToolChars > 0) {
      for (const [toolName, chars] of Object.entries(toolResultByNameChars)) {
        toolResultByName[toolName] = Math.round(toolTokens * (chars as number / totalToolChars));
      }
    }
  }
  
  return {
    base_prompt_tokens: basePromptTokens,
    claude_md_tokens: claudeMdTokens,
    skills_tokens: skillsTokens,
    wiki_tokens: wikiTokens,
    tools_tokens: toolTokens,
    messages_tokens: messagesTokens,
    user_tokens: userTokens,
    assistant_tokens: assistantTokens,
    tool_tokens: toolTokens,
    tool_result_by_name: toolResultByName,
    total_tokens: actualInputTokens || Math.round(totalChars / CHARS_PER_TOKEN),
    is_plan_mode: event.is_plan_mode || false,
    plan_mode_tokens: planModeTokens,
  };
}

export function useChat() {
  const [searchParams] = useSearchParams();
  const [inputValue, setInputValue] = useState('');
  const [contextFiles, setContextFiles] = useState<string[]>([]);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const currentSessionIdRef = useRef<string | null>(null);
  const [currentProject, setCurrentProject] = useState<string | null>(null);
  const [currentCwd, setCurrentCwd] = useState<string | null>(null);
  const [isWaitingResponse, setIsWaitingResponse] = useState(false);
  const [isLoadingSession, setIsLoadingSession] = useState(false);
  const [fileTreeRefreshTrigger, setFileTreeRefreshTrigger] = useState(0);
  const pendingSessionNameRef = useRef<string | null>(null);
  
  const [permissionMode, setPermissionMode] = useState<'default' | 'acceptEdits' | 'plan' | 'bypassPermissions'>('bypassPermissions');
  
  const [sessionRefreshTrigger, setSessionRefreshTrigger] = useState(0);
  const [pendingSteerMessages, setPendingSteerMessages] = useState<Array<{content: string, contextFiles: string[], model?: string}>>([]);
  const autoSendRef = useRef(false);
  
  const sessionId = currentSessionId ?? sessionStore.getCurrentId();
  const isStreaming = useSessionStore(() => sessionStore.get(sessionId ?? '')?.projections?.running ?? false);
  const hasMoreHistory = useSessionStore(() => sessionId ? sessionStore.get(sessionId)?.hasMore ?? false : false);
  const pendingPermission = useSessionStore(() => sessionId ? sessionStore.getPendingPermission(sessionId) : undefined);
  const pendingQuestion = useSessionStore(() => sessionId ? sessionStore.getPendingQuestion(sessionId) : undefined);
  const tasks = useSessionStore(() => sessionId ? sessionStore.getTasks(sessionId) : EMPTY_TASKS);
  const taskFocusId = useSessionStore(() => sessionId ? sessionStore.getTaskFocus(sessionId) : null);
  const planSlug = useSessionStore(() => sessionId ? sessionStore.getPlanSlug(sessionId) : undefined);
  const fileSnapshots = useSessionStore(() => sessionId ? sessionStore.getFileSnapshots(sessionId) : EMPTY_FILE_SNAPSHOTS);
  const contextUsed = useSessionStore(() => sessionId ? sessionStore.getContextUsed(sessionId) : 0);
  const contextTotal = useSessionStore(() => sessionId ? sessionStore.getContextTotal(sessionId) : DEFAULT_CONTEXT_WINDOW);
  const sessionStats = useSessionStore(() => sessionId ? sessionStore.getDetailedStats(sessionId) : EMPTY_STATS);
  
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
        }
        return;
      }
      
      if (eventType === 'session/title') {
        const titleSessionId = event.session_id;
        const title = event.title;
        if (titleSessionId && title) {
          sessionStore.updateProjections(titleSessionId, { title });
        }
        return;
      }
      
      if (eventType === 'session/plan_linked') {
        const planSlug = event.plan_slug;
        if (planSlug && eventSessionId) {
          sessionStore.updateProjections(eventSessionId, { plan_slug: planSlug });
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
        const contextUsed = event.estimated_context_tokens ?? 0;
        const contextTotal = event.effective_window ?? event.context_window ?? DEFAULT_CONTEXT_WINDOW;
        sessionStore.setContextStats(targetSessionId, contextUsed, contextTotal);
        sessionStore.setDetailedStats(
          targetSessionId,
          event.total_input_tokens || 0,
          event.total_output_tokens || 0,
          event.total_cached_tokens || 0
        );
        // 计算实时 breakdown
        const breakdown = computeBreakdownFromStats(event);
        if (breakdown) {
          sessionStore.setBreakdown(targetSessionId, breakdown);
        }
      }
      if (eventType === 'context/compacted') {
        const contextUsed = event.estimated_context_tokens ?? 0;
        const contextTotal = event.effective_window ?? event.context_window ?? DEFAULT_CONTEXT_WINDOW;
        sessionStore.setContextStats(targetSessionId, contextUsed, contextTotal);
        handleNodeEvent(targetSessionId, {
          type: 'system',
          message: event.message || '上下文已压缩',
        });
      }
      
      handleNodeEvent(targetSessionId, event);
      
      if (eventType === 'tool_result' && event.snapshot) {
        const snap = event.snapshot;
        if (snap.old_content !== undefined && snap.new_content !== undefined) {
          const acceptedKey = `acceptedChanges:${eventSessionId}`;
          const accepted: string[] = JSON.parse(localStorage.getItem(acceptedKey) || '[]');
          const wasAccepted = accepted.includes(snap.file_path);
          if (wasAccepted) {
            accepted.splice(accepted.indexOf(snap.file_path), 1);
            localStorage.setItem(acceptedKey, JSON.stringify(accepted));
          }
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
      
      if (eventType === 'question/request') {
        const questionReq: QuestionRequest = {
          request_id: event.request_id,
          question: event.question,
          options: event.options,
          context: event.context,
        };
        sessionStore.setPendingQuestion(eventSessionId, questionReq);
      }
      
      if (eventType === 'question/resolved') {
        sessionStore.setPendingQuestion(eventSessionId, undefined);
      }
      
      if (eventType === 'task_list/updated') {
        fetchTasks(eventSessionId);
      }
      
      if (eventType === 'plan/updated') {
        sessionStore.bumpPlanRevision(eventSessionId);
      }
      
      if (eventType === 'permission/mode_changed') {
        const mode = event.mode as 'default' | 'acceptEdits' | 'plan' | 'bypassPermissions' | undefined;
        if (mode) {
          sessionStore.setPermissionMode(eventSessionId, mode);
          if (eventSessionId === currentSessionIdRef.current) {
            setPermissionMode(mode);
          }
        }
      }
      
      if (eventType === 'turn/start') {
        sessionStore.updateProjections(eventSessionId, { running: true });
      }
      
      if (eventType === 'turn/end') {
        const doneSessionId = eventSessionId;
        logger.info('[WS] turn/end:', { sessionId: doneSessionId, subAgentId: event.sub_agent_id });
        sessionStore.updateProjections(doneSessionId, {
          running: false,
          updatedAt: Date.now(),
        });
        if (isCurrentSession) {
          currentSessionIdRef.current = doneSessionId;
          setCurrentSessionId(doneSessionId);
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
      }
    }).catch(console.error);
    
    const lastCwd = localStorage.getItem('lastCwd');
    if (lastCwd) {
      setCurrentCwd(lastCwd);
      setCurrentProject(lastCwd.split('/').pop() || lastCwd);
    }
    
    const requestedSessionId = searchParams.get('session');
    const initialSessionId = requestedSessionId || localStorage.getItem('lastSessionId');
    if (initialSessionId) {
      logger.info('[INIT] restoring session:', initialSessionId);
      fetchSessionSummary(initialSessionId)
        .then(() => handleSessionSelect(initialSessionId))
        .catch((err: unknown) => {
          if ((err as { status?: number } | undefined)?.status === 404) {
            logger.info('[INIT] stale session removed:', initialSessionId);
            if (!requestedSessionId) {
              localStorage.removeItem('lastSessionId');
            }
          } else {
            handleSessionSelect(initialSessionId);
          }
        });
    }
  }, []);

  useEffect(() => {
    if (config && selectedModel && sessionId) {
      const endpoint = Object.values(config.endpoints).find(e => e.model === selectedModel);
      if (endpoint?.context_window) {
        sessionStore.setContextTotal(sessionId, endpoint.context_window);
      }
    }
  }, [config, selectedModel, sessionId]);

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
    
    localStorage.setItem('lastSessionId', sessionId);
    
    sessionStore.select(sessionId);
    
    currentSessionIdRef.current = sessionId;
    setCurrentSessionId(sessionId);
    
    resetNodes(sessionId);
    setIsLoadingSession(true);
    pendingSessionNameRef.current = null;
    
    try {
      const [summaryResult, eventsResult, fileChangesResult] = await Promise.allSettled([
        fetchSessionSummary(sessionId),
        fetch(`/api/sessions/${sessionId}/events?limit=50`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
        fetch(`/api/sessions/${sessionId}/file-changes`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      ]);
      
      if (currentSessionIdRef.current !== sessionId) {
        logger.info('[SESSION] session changed during fetch, aborting');
        return;
      }
      
      if (summaryResult.status === 'fulfilled') {
        const summary = summaryResult.value;
        const metadata = summary.metadata || {};
        const projections = summary.projections || {};
        const stats = summary.stats || {};
        const breakdown = summary.breakdown || null;
        
        sessionStore.updateProjections(sessionId, {
          title: projections.title || metadata.name,
          cwd: metadata.cwd,
          updatedAt: Date.now(),
          running: projections.running ?? false,
          plan_slug: projections.plan_slug,
        });
        
        if (metadata.cwd) {
          setCurrentProject(metadata.cwd.split('/').pop() || metadata.cwd);
          setCurrentCwd(metadata.cwd);
          localStorage.setItem('lastCwd', metadata.cwd);
        }
        
        const contextUsed = stats.estimated_context_tokens || 0;
        const contextTotal = stats.effective_window || stats.context_window || DEFAULT_CONTEXT_WINDOW;
        sessionStore.setContextStats(sessionId, contextUsed, contextTotal);
        if (stats.total_input_tokens || stats.total_output_tokens) {
          sessionStore.setDetailedStats(sessionId, stats.total_input_tokens || 0, stats.total_output_tokens || 0, stats.total_cached_tokens || 0);
        }
        
        if (breakdown) {
          sessionStore.setBreakdown(sessionId, breakdown);
        }
        
        if (summary.permission_mode) {
          sessionStore.setPermissionMode(sessionId, summary.permission_mode);
          setPermissionMode(summary.permission_mode as 'default' | 'acceptEdits' | 'plan' | 'bypassPermissions');
        }
      }
      
      if (eventsResult.status === 'fulfilled') {
        const { events, has_more, base_seq, total_count } = eventsResult.value;
        logger.info('[SESSION] loaded:', {
          id: sessionId,
          eventCount: events.length,
          totalCount: total_count,
          hasMore: has_more,
          baseSeq: base_seq,
        });
        
        const lastSeq = events.length > 0 ? Math.max(...events.map((e: any) => e.seq ?? 0)) : -1;
        sessionStore.setPagination(sessionId, has_more, base_seq, lastSeq);
        loadSessionEvents(sessionId, events);
      }
      
      if (fileChangesResult.status === 'fulfilled') {
        const { files } = fileChangesResult.value;
        if (files && files.length > 0) {
          const acceptedKey = `acceptedChanges:${sessionId}`;
          const accepted: string[] = JSON.parse(localStorage.getItem(acceptedKey) || '[]');
          const filtered = files.filter((f: any) => !accepted.includes(f.file_path));
          sessionStore.setFileSnapshots(sessionId, filtered);
        }
      }
    } catch (err) {
      console.error('Failed to load session:', err);
    } finally {
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
      // U1: 运行中插话走 steer 端点（后端 MessageQueue 在下一 step 边界注入当前轮），
      // 不再本地排队等 turn 结束重发。仅当服务端报"不活跃"（竞态：turn 恰好结束）
      // 才退回重发队列，作为新 turn 发送。
      const steerContent = inputValue.trim();
      const steerCtx = [...contextFiles];
      logger.info('[MSG] steer via API:', steerContent.slice(0, 30));
      setInputValue('');
      setContextFiles([]);
      // 乐观 UI：后端将落盘 user_message 事件，但 WS 侧只更新投影不回显气泡
      addUserMessage(currentSessionIdRef.current!, steerContent, steerCtx.length > 0 ? steerCtx : undefined, undefined, selectedModel || undefined);
      const fallbackToResend = () => {
        setPendingSteerMessages(prev => [...prev, {
          content: steerContent,
          contextFiles: steerCtx,
          model: selectedModel || undefined,
        }]);
      };
      try {
        const result = await steerSession(currentSessionIdRef.current!, steerContent, steerCtx.length > 0 ? steerCtx : undefined);
        if (!result.success) {
          logger.info('[MSG] steer rejected, fallback to resend:', result.message);
          fallbackToResend();
        }
      } catch (err) {
        logger.error('[MSG] steer failed, fallback to resend:', err);
        fallbackToResend();
      }
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
  const handleSendMessageRef = useRef(handleSendMessage);
  handleSendMessageRef.current = handleSendMessage;
  
  useEffect(() => {
    if (autoSendRef.current && inputValue && !isStreaming) {
      autoSendRef.current = false;
      setTimeout(() => {
        handleSendMessageRef.current();
      }, 0);
    }
  }, [inputValue, isStreaming]);

  const [isCompacting, setIsCompacting] = useState(false);
  
  const handleCompactSession = useCallback(async () => {
    if (!currentSessionId || isCompacting) return;
    try {
      setIsCompacting(true);
      const result = await compactSession(currentSessionId);
      if (result.success && result.folded === false) {
        alert('当前上下文较小，暂无需压缩');
      } else if (result.success) {
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
    const modes: Array<'default' | 'acceptEdits' | 'plan' | 'bypassPermissions'> = ['default', 'acceptEdits', 'plan', 'bypassPermissions'];
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

  const handleSetPermissionMode = useCallback(async (mode: 'default' | 'acceptEdits' | 'plan' | 'bypassPermissions') => {
    setPermissionMode(mode);
    if (currentSessionId) {
      try {
        await updatePermissionMode(currentSessionId, mode);
      } catch (err) {
        console.error('Failed to update permission mode:', err);
      }
    }
  }, [currentSessionId]);

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

  // ── 统一回退（对话 + 文件原子回退） ──

  const [pendingRewind, setPendingRewind] = useState<{ plan: RewindPlan; mode: 'rewind' | 'edit' } | null>(null);

  const handleRewindRequest = useCallback(async (userMessageIndex: number) => {
    if (!currentSessionId) return;
    try {
      const plan = await stageRewind(currentSessionId, { keepUserMessages: userMessageIndex });
      setPendingRewind({ plan, mode: 'rewind' });
    } catch (err) {
      console.error('Failed to stage rewind:', err);
    }
  }, [currentSessionId]);

  const handleEditMessage = useCallback(async (node: UserNode) => {
    setInputValue(node.content);
    setContextFiles(node.contextFiles || []);
    setSelectedModel(node.model || '');

    const nodeIndex = userNodes.findIndex(n => n.key === node.key);
    if (!currentSessionId || nodeIndex < 0) return;
    try {
      // 编辑 = 回退到该消息之前 + 填充输入框；对话与文件原子回退
      const plan = await stageRewind(currentSessionId, { keepUserMessages: nodeIndex });
      if (plan.file_changes.length === 0) {
        await commitRewind(currentSessionId, plan.plan_id);
        await handleSessionSelect(currentSessionId);
      } else {
        // 有文件变更：交给 RewindDialog 预览确认
        setPendingRewind({ plan, mode: 'edit' });
      }
    } catch (err) {
      console.error('Failed to rewind for edit:', err);
    }
  }, [userNodes, currentSessionId, handleSessionSelect]);

  const handleRewindCommitted = useCallback(async () => {
    setPendingRewind(null);
    if (currentSessionId) {
      await handleSessionSelect(currentSessionId);
    }
  }, [currentSessionId, handleSessionSelect]);

  const handleRewindClose = useCallback(() => {
    setPendingRewind(null);
  }, []);

  const handlePermissionApprove = useCallback(async (choice?: string) => {
    if (!pendingPermission || !currentSessionId) return;
    try {
      await respondToPermission(currentSessionId, pendingPermission.request_id, true, undefined, choice);
      sessionStore.setPendingPermission(currentSessionId, undefined);
    } catch (err) {
      console.error('Failed to approve permission:', err);
    }
  }, [pendingPermission, currentSessionId]);

  const handlePermissionDeny = useCallback(async (feedback?: string) => {
    if (!pendingPermission || !currentSessionId) return;
    try {
      await respondToPermission(currentSessionId, pendingPermission.request_id, false, feedback);
    } catch (err) {
      console.error('Failed to deny permission:', err);
    } finally {
      // Always clear pending permission and mark tool call as denied
      sessionStore.setPendingPermission(currentSessionId, undefined);
      sessionStore.updateSnapshot(currentSessionId, prev => {
        const newNodes = new Map(prev.nodes);
        for (const [key, node] of newNodes) {
          if (node.kind === 'tool-call' && node.status === 'pending') {
            newNodes.set(key, { ...node, status: 'denied' });
          }
        }
        return { order: prev.order, nodes: newNodes };
      });
    }
  }, [pendingPermission, currentSessionId]);

  const fetchTasks = useCallback(async (sessionId: string) => {
    try {
      // 走 client.ts 那个照生成契约写的方法（类型来自 paths[...]），不再手写 fetch + 手解 JSON。
      const data = await fetchTasksApi(sessionId);
      // 后端读端点的 `status` 刻意是宽松的 `str`（校验失败会把整个面板打成 500），
      // 生成类型因此是 `string`。这里显式收窄成面板要的 `TaskStatus`：**不在五值词表里的
      // 原样保留**，面板把它渲染成「未知状态」。悄悄映射成 pending 会让后端加第六种状态时
      // 前端一声不响地显示错的东西。
      sessionStore.setTasks(sessionId, (data.tasks ?? []) as TaskItem[], data.focus_id ?? null);
    } catch (err) {
      // 失败**不清空**已有清单：保留旧数据 + 可观测，好过面板静默变空白。
      // 状态码必须进日志——`_taskError` 在有 detail 时消息里不带状态码，
      // 于是「路由 500（响应形状损坏）」与「404/422」在控制台里长得一样，互相遮掩。
      const status = (err as Error & { status?: number })?.status;
      console.error(
        `[TASK] fetchTasks failed (${status != null ? `HTTP ${status}` : 'network error'}), 保留现有清单:`,
        err,
      );
    }
  }, []);

  /** 面板写成功之后的回灌入口（R3：写端点不广播 task_list/updated，发起方就是面板自己）。
   *  必须走后端 GET 而不是本地乐观拼接——`focus_id` 只有后端 `find_focus` 算得准。 */
  const refreshTasks = useCallback(() => {
    if (!currentSessionId) return;
    void fetchTasks(currentSessionId);
  }, [currentSessionId, fetchTasks]);

  useEffect(() => {
    if (currentSessionId) {
      fetchTasks(currentSessionId);
    }
  }, [currentSessionId, fetchTasks]);

  const handleQuestionRespond = useCallback(async (answer: string) => {
    if (!pendingQuestion || !currentSessionId) return;
    try {
      await respondToQuestion(currentSessionId, pendingQuestion.request_id, answer);
    } catch (err) {
      console.error('Failed to respond to question:', err);
    } finally {
      sessionStore.setPendingQuestion(currentSessionId, undefined);
    }
  }, [pendingQuestion, currentSessionId]);

  const markFileAccepted = useCallback((sessionId: string, filePath: string) => {
    const key = `acceptedChanges:${sessionId}`;
    const accepted: string[] = JSON.parse(localStorage.getItem(key) || '[]');
    if (!accepted.includes(filePath)) {
      accepted.push(filePath);
      localStorage.setItem(key, JSON.stringify(accepted));
    }
  }, []);

  const handleAcceptFile = useCallback((filePath: string) => {
    if (!currentSessionId) return;
    markFileAccepted(currentSessionId, filePath);
    const snaps = sessionStore.getFileSnapshots(currentSessionId).filter(s => s.file_path !== filePath);
    sessionStore.setFileSnapshots(currentSessionId, snaps);
  }, [currentSessionId, markFileAccepted]);

  const handleRejectFile = useCallback(async (filePath: string) => {
    if (!currentSessionId) return;
    const snap = fileSnapshots.find(s => s.file_path === filePath);
    if (!snap) return;
    try {
      const res = await fetch('/api/revert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: currentSessionId, file_path: filePath, old_content: snap.old_content }),
      });
      if (res.ok) {
        markFileAccepted(currentSessionId, filePath);
        const snaps = sessionStore.getFileSnapshots(currentSessionId).filter(s => s.file_path !== filePath);
        sessionStore.setFileSnapshots(currentSessionId, snaps);
        setFileTreeRefreshTrigger(prev => prev + 1);
      }
    } catch (err) {
      console.error('Failed to revert file:', err);
    }
  }, [fileSnapshots, currentSessionId, markFileAccepted]);

  const handleAcceptAll = useCallback(() => {
    if (!currentSessionId) return;
    const currentSnaps = sessionStore.getFileSnapshots(currentSessionId);
    const key = `acceptedChanges:${currentSessionId}`;
    const accepted: string[] = JSON.parse(localStorage.getItem(key) || '[]');
    for (const snap of currentSnaps) {
      if (!accepted.includes(snap.file_path)) accepted.push(snap.file_path);
    }
    localStorage.setItem(key, JSON.stringify(accepted));
    sessionStore.setFileSnapshots(currentSessionId, []);
  }, [currentSessionId]);

  return {
    // State
    inputValue,
    setInputValue,
    contextFiles,
    config,
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
    sessionStats,
    pendingPermission,
    pendingQuestion,
    tasks,
    taskFocusId,
    sessionRefreshTrigger,
    chatSnapshot,
    pendingSteerMessages,
    setPendingSteerMessages,
    planSlug,
    
    // Handlers
    handleAddToChat,
    handleRemoveContext,
    handleSessionSelect,
    handleNewSession,
    handleSendMessage,
    handleStopStreaming,
    handleCompactSession,
    handleCyclePermissionMode,
    handleSetPermissionMode,
    handleForkSession,
    handleForkAtPoint,
    handleEditMessage,
    pendingRewind,
    handleRewindRequest,
    handleRewindCommitted,
    handleRewindClose,
    handlePermissionApprove,
    handlePermissionDeny,
    handleQuestionRespond,
    refreshTasks,
    handleAcceptFile,
    handleRejectFile,
    handleAcceptAll,
    handleNodeEvent,
    handleLoadMoreEvents,
    hasMoreHistory,
  };
}

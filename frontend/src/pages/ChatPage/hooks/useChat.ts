import { useState, useEffect, useRef, useCallback } from 'react';
import {
  fetchConfig, AppConfig,
  generateSessionName, updateSessionName,
  compactSession, updatePermissionMode,
  forkSession, respondToPermission, truncateSession, rewindSession, PermissionRequest
} from '../../../api/client';
import { FileSnapshot } from '../../../components/ReviewPanel';
import { useChatNodes } from '../../../components/chat/nodes';
import { ChatMessage } from '../types';

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [contextFiles, setContextFiles] = useState<string[]>([]);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string>('build');
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const currentSessionIdRef = useRef<string | null>(null);
  const [currentProject, setCurrentProject] = useState<string | null>(null);
  const [currentCwd, setCurrentCwd] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isWaitingResponse, setIsWaitingResponse] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  const [fileSnapshots, setFileSnapshots] = useState<FileSnapshot[]>([]);
  const [fileTreeRefreshTrigger, setFileTreeRefreshTrigger] = useState(0);
  const pendingSessionNameRef = useRef<string | null>(null);
  
  const [yoloMode, setYoloMode] = useState(true);
  const [contextUsed, setContextUsed] = useState(0);
  const [contextTotal, setContextTotal] = useState(128000);
  
  const [pendingPermission, setPendingPermission] = useState<PermissionRequest | null>(null);
  const [sessionRefreshTrigger, setSessionRefreshTrigger] = useState(0);
  const [pendingSteerMessage, setPendingSteerMessage] = useState<{content: string, contextFiles: string[], model?: string} | null>(null);
  const autoSendRef = useRef(false);
  
  // Goal mode state
  const [goalState, setGoalState] = useState<{
    active: boolean;
    goal: string;
    criteria: string[];
    iteration: number;
    maxIterations: number;
    status: 'idle' | 'running' | 'achieved' | 'budget_exhausted' | 'aborted';
  } | null>(null);
  
  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);
  
  const { snapshot: chatSnapshot, handleSSEEvent: handleNodeEvent, addUserMessage, resetNodes, loadMessages, loadOpenAIMessages, loadTraceEvents } = useChatNodes();

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
  }, []);

  useEffect(() => {
    if (config && selectedModel) {
      const endpoint = Object.values(config.endpoints).find(e => e.model === selectedModel);
      if (endpoint?.context_window) {
        setContextTotal(endpoint.context_window);
      }
    }
  }, [config, selectedModel]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  const handleAddToChat = (path: string) => {
    if (!contextFiles.includes(path)) {
      setContextFiles([...contextFiles, path]);
    }
  };

  const handleRemoveContext = (path: string) => {
    setContextFiles(contextFiles.filter(f => f !== path));
  };

  const handleSessionSelect = async (sessionId: string) => {
    resetNodes();
    pendingSessionNameRef.current = null;
    setPendingPermission(null);
    setFileSnapshots([]);
    setCurrentSessionId(sessionId);
    
    try {
      // Fetch session data and trace events in parallel
      const [sessionResponse, traceResponse] = await Promise.all([
        fetch(`/api/sessions/${sessionId}`),
        fetch(`/api/trace/${sessionId}`),
      ]);
      
      if (sessionResponse.ok) {
        const data = await sessionResponse.json();
        const rawMessages = data.openaiMessages || data.messages || [];
        
        const loadedMessages: ChatMessage[] = [];
        for (const msg of rawMessages) {
          if (msg.role === 'user') {
            const content = typeof msg.content === 'string' 
              ? msg.content 
              : Array.isArray(msg.content)
                ? msg.content.filter((b: any) => b.type === 'text').map((b: any) => b.text).join('\n')
                : '';
            const filteredContent = content.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '').trim();
            if (filteredContent) {
              loadedMessages.push({
                role: 'user',
                content: filteredContent,
                timestamp: new Date().toISOString(),
              });
            }
          } else if (msg.role === 'assistant') {
            let content = '';
            if (typeof msg.content === 'string' && msg.content) {
              content = msg.content;
            }
            if (msg.thinking) {
              content = `<thinking>${msg.thinking}</thinking>\n\n${content}`;
            }
            if (content.trim()) {
              loadedMessages.push({
                role: 'assistant',
                content: content.trim(),
                timestamp: new Date().toISOString(),
              });
            }
          }
        }
        setMessages(loadedMessages);
        
        // Get trace events first for sub-agent mapping
        let traceEvents: Array<Record<string, unknown>> | undefined;
        if (traceResponse.ok) {
          const traceData = await traceResponse.json();
          if (traceData.events && traceData.events.length > 0) {
            traceEvents = traceData.events;
          }
        }
        
        // Load messages with trace context for proper sub-agent ordering
        loadOpenAIMessages(rawMessages, traceEvents);
        
        // Load trace events to reconstruct sub-agent content
        if (traceEvents) {
          loadTraceEvents(traceEvents);
        }
        
        if (data.metadata?.cwd) {
          setCurrentProject(data.metadata.cwd.split('/').pop() || data.metadata.cwd);
          setCurrentCwd(data.metadata.cwd);
          localStorage.setItem('lastCwd', data.metadata.cwd);
        }
      }
    } catch (err) {
      console.error('Failed to load session:', err);
    }
  };

  const handleNewSession = (cwd?: string) => {
    const projectCwd = cwd || currentCwd || localStorage.getItem('lastCwd') || '';
    if (!projectCwd) {
      alert('Please select a project first');
      return;
    }
    setCurrentSessionId(null);
    setMessages([]);
    setInputValue('');
    setContextFiles([]);
    setCurrentProject(projectCwd.split('/').pop() || projectCwd);
    setCurrentCwd(projectCwd);
    localStorage.setItem('lastCwd', projectCwd);
    setPendingPermission(null);
    setFileSnapshots([]);
    pendingSessionNameRef.current = null;
    resetNodes();
  };

  const handleSendMessage = async () => {
    if (!inputValue.trim()) return;
    if (!currentCwd) {
      alert('Please select a project first');
      return;
    }
    
    // If currently streaming, queue as steer message
    if (isStreaming) {
      setPendingSteerMessage({
        content: inputValue.trim(),
        contextFiles: [...contextFiles],
        model: selectedModel || undefined,
      });
      setInputValue('');
      setContextFiles([]);
      return;
    }
    
    const isFirstMessage = messages.length === 0;
    const userMessageContent = inputValue.trim();
    
    const userMessage: ChatMessage = {
      role: 'user',
      content: userMessageContent,
      timestamp: new Date().toISOString(),
      contextFiles: [...contextFiles],
      model: selectedModel || undefined,
    };
    
    setMessages(prev => [...prev, userMessage]);
    addUserMessage(userMessageContent, contextFiles.length > 0 ? contextFiles : undefined, undefined, selectedModel || undefined);
    setInputValue('');
    setContextFiles([]);

    if (isFirstMessage && !currentSessionId) {
      pendingSessionNameRef.current = userMessageContent;
    }

    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    setIsStreaming(true);
    setIsWaitingResponse(true);

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: userMessage.content,
          session_id: currentSessionId,
          context_files: userMessage.contextFiles,
          agent: selectedAgent,
          model: userMessage.model,
          permission_mode: yoloMode ? 'bypassPermissions' : 'default',
          cwd: currentCwd!,
        }),
        signal: abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      if (!response.body) {
        throw new Error('No response body');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split('\n');

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            try {
              const parsed = JSON.parse(data);

              if (isWaitingResponse && (parsed.text || parsed.thinking || parsed.tool_call)) {
                setIsWaitingResponse(false);
              }

              if (parsed.stats) {
                setContextUsed(parsed.stats.last_input_token_count || 0);
                setContextTotal(parsed.stats.context_window || 128000);
              }

              handleNodeEvent(parsed);
              
              if (parsed.tool_result?.snapshot) {
                const snap = parsed.tool_result.snapshot;
                if (snap.old_content !== undefined && snap.new_content !== undefined) {
                  setFileSnapshots(prev => {
                    const filtered = prev.filter(s => s.file_path !== snap.file_path);
                    return [...filtered, {
                      file_path: snap.file_path,
                      is_new: snap.is_new,
                      old_content: snap.old_content,
                      new_content: snap.new_content,
                    }];
                  });
                }
              }
              
              if (parsed.tool_result && ['write_file', 'edit_file', 'run_shell'].includes(parsed.tool_result.name)) {
                setFileTreeRefreshTrigger(prev => prev + 1);
              }
              
              if (parsed.permission_request) {
                const permReq = parsed.permission_request as PermissionRequest;
                setPendingPermission(permReq);
              }
              
              // Goal mode events
              if (parsed.goal_criteria) {
                // Show criteria for confirmation
                const { goal, criteria } = parsed.goal_criteria;
                setGoalState({
                  active: false, // Not started yet, waiting for confirmation
                  goal,
                  criteria,
                  iteration: 0,
                  maxIterations: 10,
                  status: 'idle',
                });
              }
              
              if (parsed.goal_start) {
                const { goal, criteria } = parsed.goal_start;
                setGoalState({
                  active: true,
                  goal,
                  criteria,
                  iteration: 0,
                  maxIterations: 10,
                  status: 'running',
                });
              }
              
              if (parsed.goal_progress) {
                setGoalState(prev => prev ? {
                  ...prev,
                  iteration: parsed.goal_progress.iteration,
                  status: parsed.goal_progress.status || 'running',
                } : null);
              }
              
              if (parsed.goal_complete) {
                setGoalState(prev => prev ? {
                  ...prev,
                  status: parsed.goal_complete.status,
                } : null);
              }
              
              if (parsed.done) {
                const doneSessionId = parsed.session_id || currentSessionId;
                if (doneSessionId) {
                  setCurrentSessionId(doneSessionId);
                  sessionStorage.removeItem('sessions');
                  setSessionRefreshTrigger(prev => prev + 1);
                  
                  if (pendingSessionNameRef.current) {
                    const userMessage = pendingSessionNameRef.current;
                    pendingSessionNameRef.current = null;
                    
                    generateSessionName(userMessage)
                      .then(async name => {
                        try {
                          await updateSessionName(doneSessionId, name);
                          sessionStorage.removeItem('sessions');
                          setSessionRefreshTrigger(prev => prev + 1);
                        } catch (err) {
                          console.error('Failed to update session name:', err);
                        }
                      })
                      .catch(err => console.error('Failed to generate session name:', err));
                  }
                }
              }
            } catch {
              // Ignore parse errors
            }
          }
        }
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        console.log('Stream aborted by user');
      } else {
        console.error('Streaming error:', error);
      }
    } finally {
      setIsStreaming(false);
      setIsWaitingResponse(false);
      abortControllerRef.current = null;
    }
  };

  const handleStopStreaming = useCallback(async () => {
    // Send abort to backend but don't abort the fetch - wait for "done" event
    if (currentSessionId) {
      try {
        await fetch(`/api/sessions/${currentSessionId}/abort`, { method: 'POST' });
      } catch (err) {
        console.error('Failed to abort session:', err);
      }
    }
    // Don't set isStreaming to false here - wait for "done" event from backend
  }, [currentSessionId]);

  // Send pending steer message after streaming ends
  useEffect(() => {
    if (!isStreaming && pendingSteerMessage) {
      const steerMsg = pendingSteerMessage;
      setPendingSteerMessage(null);
      
      // Set input value and context files
      setInputValue(steerMsg.content);
      setContextFiles(steerMsg.contextFiles);
      if (steerMsg.model) {
        setSelectedModel(steerMsg.model);
      }
      
      // Mark for auto-send after state updates
      autoSendRef.current = true;
    }
  }, [isStreaming, pendingSteerMessage]);
  
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

  const handleCompactSession = useCallback(async () => {
    if (!currentSessionId) return;
    try {
      await compactSession(currentSessionId);
    } catch (err) {
      console.error('Failed to compact session:', err);
    }
  }, [currentSessionId]);

  const handleToggleYoloMode = useCallback(async () => {
    const newMode = !yoloMode;
    setYoloMode(newMode);
    
    if (currentSessionId) {
      try {
        await updatePermissionMode(currentSessionId, newMode ? 'bypassPermissions' : 'default');
      } catch (err) {
        console.error('Failed to update permission mode:', err);
      }
    }
  }, [currentSessionId, yoloMode]);

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

  const handleForkAtPoint = useCallback(async (nodeIndex: number) => {
    if (!currentSessionId) return;
    try {
      let userMessageCount = 0;
      for (let i = 0; i < nodeIndex; i++) {
        if (messages[i]?.role === 'user') {
          userMessageCount++;
        }
      }
      
      const result = await forkSession(currentSessionId);
      if (result.new_session_id) {
        await truncateSession(result.new_session_id, userMessageCount);
        sessionStorage.removeItem('sessions');
        setSessionRefreshTrigger(prev => prev + 1);
        await handleSessionSelect(result.new_session_id);
      }
    } catch (err) {
      console.error('Failed to fork at point:', err);
    }
  }, [currentSessionId, messages, handleSessionSelect]);

  const handleEditMessage = useCallback(async (index: number) => {
    const msg = messages[index];
    if (msg.role !== 'user') return;
    
    // Count user messages before this one to determine how many turns to rewind
    let userMessagesBefore = 0;
    for (let i = 0; i < index; i++) {
      if (messages[i].role === 'user') {
        userMessagesBefore++;
      }
    }
    
    setInputValue(msg.content);
    setContextFiles(msg.contextFiles || []);
    setSelectedModel(msg.model || '');
    
    if (currentSessionId) {
      try {
        // Calculate turns to rewind: current turns - target turns
        // Each user message represents one turn
        const currentTurns = messages.filter(m => m.role === 'user').length;
        const targetTurns = userMessagesBefore;
        const turnsToRewind = currentTurns - targetTurns;
        
        if (turnsToRewind > 0) {
          // Use rewind to restore file snapshots
          await rewindSession(currentSessionId, turnsToRewind);
        } else {
          // Fallback to truncate if no turns to rewind
          await truncateSession(currentSessionId, userMessagesBefore);
        }
      } catch (err) {
        console.error('Failed to rewind session:', err);
        // Fallback to truncate on error
        try {
          await truncateSession(currentSessionId, userMessagesBefore);
        } catch (e) {
          console.error('Failed to truncate session:', e);
        }
      }
    }
    
    const remainingMessages = messages.slice(0, index);
    setMessages(remainingMessages);
    loadMessages(remainingMessages);
  }, [messages, loadMessages, currentSessionId]);

  const handlePermissionApprove = useCallback(async () => {
    if (!pendingPermission || !currentSessionId) return;
    try {
      await respondToPermission(currentSessionId, pendingPermission.request_id, true);
      setPendingPermission(null);
    } catch (err) {
      console.error('Failed to approve permission:', err);
    }
  }, [pendingPermission, currentSessionId]);

  const handlePermissionDeny = useCallback(async () => {
    if (!pendingPermission || !currentSessionId) return;
    try {
      await respondToPermission(currentSessionId, pendingPermission.request_id, false);
      setPendingPermission(null);
    } catch (err) {
      console.error('Failed to deny permission:', err);
    }
  }, [pendingPermission, currentSessionId]);

  const handleAcceptFile = useCallback((filePath: string) => {
    setFileSnapshots(prev => prev.filter(s => s.file_path !== filePath));
  }, []);

  const handleRejectFile = useCallback(async (filePath: string) => {
    const snap = fileSnapshots.find(s => s.file_path === filePath);
    if (!snap) return;
    try {
      const res = await fetch('/api/revert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: filePath, old_content: snap.old_content }),
      });
      if (res.ok) {
        setFileSnapshots(prev => prev.filter(s => s.file_path !== filePath));
        setFileTreeRefreshTrigger(prev => prev + 1);
      }
    } catch (err) {
      console.error('Failed to revert file:', err);
    }
  }, [fileSnapshots]);

  const handleAcceptAll = useCallback(() => {
    setFileSnapshots([]);
  }, []);

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
    setGoalState(null);
  }, []);

  return {
    // State
    messages,
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
    isWaitingResponse,
    fileSnapshots,
    fileTreeRefreshTrigger,
    yoloMode,
    contextUsed,
    contextTotal,
    pendingPermission,
    sessionRefreshTrigger,
    chatSnapshot,
    pendingSteerMessage,
    setPendingSteerMessage,
    goalState,
    
    // Handlers
    handleAddToChat,
    handleRemoveContext,
    handleSessionSelect,
    handleNewSession,
    handleSendMessage,
    handleStopStreaming,
    handleCompactSession,
    handleToggleYoloMode,
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
  };
}

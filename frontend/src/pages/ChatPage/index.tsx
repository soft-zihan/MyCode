import { useState, useCallback, useEffect } from 'react';
import { 
  MessageSquare, PanelRight, Send, Square, Zap, File, X, Package, Cpu, Sliders, Layers, BookOpen, Target
} from 'lucide-react';
import { ReviewPanel } from '../../components/ReviewPanel';
import { DiffViewer } from '../../components/DiffViewer';
import { ChatView } from '../../components/chat/nodes';
import { FileTree } from './components/FileTree';
import { FileViewer } from './components/FileViewer';
import { SessionsPanel } from './components/SessionsPanel';
import { ContextPanel } from '../../components/agent/ContextPanel';
import { DeliverablesPanel } from '../../components/agent/DeliverablesPanel';
import { QuestionDialog } from '../../components/agent/QuestionDialog';
import { PlanApprovalDialog } from '../../components/agent/PlanApprovalDialog';
import { WikiPanel } from '../../components/agent/WikiPanel';
import { PlanControlPanel } from '../../components/agent/PlanControlPanel';
import { TodoListPanel } from '../../components/chat/TodoListPanel';
import { PageLayout } from '../../components/PageLayout';
import { useChat } from './hooks/useChat';
import { Composer } from '../../components/chat/Composer';
import { PermissionSelect } from '../../components/chat/PermissionSelect';
import { fetchWorkspaceTree, WorkspaceNode } from '../../api/client';

const formatTokens = (tokens: number): string => {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
  return `${tokens}`;
};

const RIGHT_SIDEBAR_MIN = 200;
const RIGHT_SIDEBAR_MAX = 600;
const RIGHT_SIDEBAR_DEFAULT = 288;
const RIGHT_SIDEBAR_WIDTH_KEY = 'right-sidebar-width';

export default function ChatPage() {
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [changesSelectedFile, setChangesSelectedFile] = useState<string | null>(null);
  const [rightSidebarOpen, setRightSidebarOpen] = useState(true);
  const [rightTab, setRightTab] = useState<'files' | 'control'>('files');
  const [controlSubTab, setControlSubTab] = useState<'context' | 'wiki' | 'plan'>('context');
  const [changesExpanded, setChangesExpanded] = useState(false);
  const [fileList, setFileList] = useState<string[]>([]);
  const [rightSidebarWidth, setRightSidebarWidth] = useState(() => {
    const saved = localStorage.getItem(RIGHT_SIDEBAR_WIDTH_KEY);
    return saved ? parseInt(saved, 10) : RIGHT_SIDEBAR_DEFAULT;
  });
  const [isDraggingRight, setIsDraggingRight] = useState(false);

  useEffect(() => {
    localStorage.setItem(RIGHT_SIDEBAR_WIDTH_KEY, String(rightSidebarWidth));
  }, [rightSidebarWidth]);

  const handleRightDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDraggingRight(true);
  }, []);

  useEffect(() => {
    if (!isDraggingRight) return;

    const handleMouseMove = (e: MouseEvent) => {
      const newWidth = window.innerWidth - e.clientX;
      setRightSidebarWidth(Math.max(RIGHT_SIDEBAR_MIN, Math.min(RIGHT_SIDEBAR_MAX, newWidth)));
    };

    const handleMouseUp = () => {
      setIsDraggingRight(false);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isDraggingRight]);

  const {
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
    sessionRefreshTrigger,
    permissionMode,
    contextUsed,
    contextTotal,
    sessionStats,
    pendingPermission,
    pendingQuestion,
    todos,
    chatSnapshot,
    pendingSteerMessages,
    setPendingSteerMessages,
    goalState,
    planSlug,
    handleAddToChat,
    handleRemoveContext,
    handleSessionSelect,
    handleNewSession,
    handleSendMessage,
    handleStopStreaming,
    handleCompactSession,
    handleSetPermissionMode,
    handleForkAtPoint,
    handleEditMessage,
    handlePermissionApprove,
    handlePermissionDeny,
    handleQuestionRespond,
    handleAcceptFile,
    handleRejectFile,
    handleAcceptAll,
    handleConfirmGoal,
    handleCancelGoal,
  } = useChat();

  // Fetch file list for @ mentions
  useEffect(() => {
    const fetchFiles = async () => {
      try {
        const tree = await fetchWorkspaceTree(currentCwd || undefined);
        const files: string[] = [];
        const flatten = (node: WorkspaceNode, path: string) => {
          if (node.type === 'file') {
            files.push(path);
          } else if (node.type === 'directory' && node.children) {
            for (const child of node.children) {
              flatten(child, path ? `${path}/${child.name}` : child.name);
            }
          }
        };
        flatten(tree, '');
        setFileList(files);
      } catch (e) {
        // Ignore errors
      }
    };
    if (currentCwd) {
      fetchFiles();
    }
  }, [currentCwd, fileTreeRefreshTrigger]);

  const handleOpenFile = useCallback((filePath: string) => {
    setSelectedFile(filePath);
  }, []);

  const sidebarContent = (
    <SessionsPanel
      onSessionSelect={handleSessionSelect}
      onNewSession={handleNewSession}
      currentSessionId={currentSessionId}
      refreshTrigger={sessionRefreshTrigger}
    />
  );

  return (
    <PageLayout sidebarContent={sidebarContent}>
      <div className="h-full flex">
        {/* Main Chat Area */}
        <div className="flex-1 flex flex-col bg-white min-w-0">
          {/* Chat Header */}
          <div className="px-3 md:px-4 py-3 border-b border-gray-200 bg-white flex items-center justify-between">
            <div className="flex items-center min-w-0 flex-1">
              <MessageSquare className="w-5 h-5 mr-2 text-blue-500 flex-shrink-0" />
              <h1 className="text-base md:text-lg font-semibold text-gray-900 truncate">Chat</h1>
              {currentProject && (
                <span className="ml-2 px-2 py-0.5 bg-blue-50 text-blue-700 text-xs font-medium rounded border border-blue-200 truncate max-w-[120px] md:max-w-none">
                  {currentProject}
                </span>
              )}
              {currentSessionId && (
                <span className="ml-2 px-2 py-0.5 bg-gray-50 text-gray-500 text-xs rounded border border-gray-200 hidden md:inline">
                  #{currentSessionId}
                </span>
              )}
            </div>
            
            {/* Session Controls */}
            <div className="flex items-center gap-4 mr-2">
              {/* Token Stats - only show when there's actual data */}
              {sessionStats.inputTokens > 0 && (
                <div className="flex items-center gap-1.5 text-xs text-gray-500">
                  <Cpu className="w-3.5 h-3.5" />
                  <span>{formatTokens(sessionStats.inputTokens)} in</span>
                  <span className="text-gray-300">/</span>
                  <span>{formatTokens(sessionStats.outputTokens || 0)} out</span>
                  {sessionStats.cachedTokens && sessionStats.cachedTokens > 0 && (
                    <>
                      <span className="text-gray-300">|</span>
                      <span className="text-green-600">
                        {Math.round((sessionStats.cachedTokens / sessionStats.inputTokens) * 100)}% cache
                      </span>
                    </>
                  )}
                </div>
              )}
              {/* Context Progress - only show when there's a session */}
              {currentSessionId && contextTotal > 0 && (
                <div className="flex items-center gap-2">
                  <div className="w-24 h-2 bg-gray-200 rounded-full overflow-hidden">
                    <div 
                      className={`h-full transition-all ${
                        (contextUsed / contextTotal) >= 0.9 ? 'bg-red-500' :
                        (contextUsed / contextTotal) >= 0.7 ? 'bg-yellow-500' :
                        'bg-green-500'
                      }`}
                      style={{ width: `${Math.min((contextUsed / contextTotal) * 100, 100)}%` }}
                    />
                  </div>
                  <span className="text-xs text-gray-500">
                    {Math.round(contextUsed / 1000)}k / {Math.round(contextTotal / 1000)}k
                  </span>
                  <button
                    onClick={handleCompactSession}
                    disabled={!currentSessionId || isStreaming || isCompacting}
                    className={`p-1 rounded transition-colors ${
                      !currentSessionId || isStreaming || isCompacting
                        ? 'text-gray-300 cursor-not-allowed'
                        : 'text-gray-600 hover:text-blue-600 hover:bg-blue-50'
                    }`}
                    title={!currentSessionId ? 'Start a session first' : isStreaming ? 'Wait for response to complete' : isCompacting ? 'Compressing...' : '压缩上下文'}
                  >
                    <Zap className={`w-3.5 h-3.5 ${isCompacting ? 'animate-pulse' : ''}`} />
                  </button>
                </div>
              )}
            </div>
            
            <button
              onClick={() => setRightSidebarOpen(!rightSidebarOpen)}
              className={`p-2 rounded hover:bg-gray-100 transition-colors flex-shrink-0 ${
                rightSidebarOpen ? 'text-blue-600' : 'text-gray-500'
              }`}
              title={rightSidebarOpen ? 'Close sidebar' : 'Open sidebar'}
            >
              <PanelRight className="w-5 h-5" />
            </button>
          </div>

          {/* Plan Mode Guidance */}
          {selectedAgent === 'plan' && (
            <div className="px-4 py-2 bg-blue-50 border-b border-blue-200">
              <p className="text-xs text-blue-700">
                <span className="font-medium">📋 计划模式：</span>
                AI 将只读取文件并制定计划。计划完成后，您可以预览、编辑或提出修改意见，批准后才会执行。
              </p>
            </div>
          )}

          {/* Messages */}
          <ChatView
            snapshot={chatSnapshot}
            sessionId={currentSessionId || undefined}
            isStreaming={isStreaming}
            isLoadingSession={isLoadingSession}
            isWaitingResponse={isWaitingResponse}
            onEditMessage={handleEditMessage}
            onFileClick={(path: string) => {
              setSelectedFile(path);
            }}
            onFork={handleForkAtPoint}
          />

          {/* Review Panel */}
          <ReviewPanel
            snapshots={fileSnapshots}
            onAccept={handleAcceptFile}
            onReject={handleRejectFile}
            onAcceptAll={handleAcceptAll}
            onOpenFile={handleOpenFile}
          />

          {/* Permission Request Dialog */}
          {pendingPermission && pendingPermission.tool_name === 'exit_plan_mode' && currentSessionId && (
            <PlanApprovalDialog
              request={pendingPermission}
              sessionId={currentSessionId}
              onApprove={handlePermissionApprove}
              onDeny={() => {
                handlePermissionDeny();
              }}
            />
          )}

          {/* Other Permission Request Dialog */}
          {pendingPermission && pendingPermission.tool_name !== 'exit_plan_mode' && (
            <div className="border-t px-4 py-3 border-yellow-300 bg-yellow-50">
              <div className="flex items-start gap-3">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-sm font-medium text-yellow-800">
                      ⚠️ 需要授权
                    </span>
                    <span className="text-xs px-1.5 py-0.5 rounded bg-yellow-200 text-yellow-800">
                      {pendingPermission.tool_name}
                    </span>
                  </div>
                  <pre className="text-xs text-gray-700 bg-white border border-yellow-200 rounded p-2 overflow-x-auto overflow-y-auto whitespace-pre-wrap font-mono max-h-32">
                    {pendingPermission.command}
                  </pre>
                </div>
                <div className="flex gap-2 flex-shrink-0">
                  <button
                    onClick={handlePermissionDeny}
                    className="px-3 py-1.5 text-sm font-medium text-red-700 bg-white border border-red-300 rounded hover:bg-red-50 transition-colors">
                    拒绝
                  </button>
                  <button
                    onClick={handlePermissionApprove}
                    className="px-3 py-1.5 text-sm font-medium text-white bg-green-600 border border-green-700 rounded hover:bg-green-700 transition-colors">
                    批准
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Question Dialog */}
          {pendingQuestion && (
            <QuestionDialog
              request={pendingQuestion}
              onRespond={handleQuestionRespond}
            />
          )}

          {/* TodoList Panel */}
          {todos && todos.length > 0 && (
            <TodoListPanel todos={todos} />
          )}

          {/* Goal Mode UI */}
          {goalState && (
            <div className={`border-t px-4 py-3 ${
              goalState.status === 'achieved' ? 'border-green-300 bg-green-50' :
              goalState.status === 'aborted' ? 'border-red-300 bg-red-50' :
              goalState.status === 'budget_exhausted' ? 'border-orange-300 bg-orange-50' :
              'border-purple-300 bg-purple-50'
            }`}>
              <div className="flex items-start gap-3">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-2">
                    <span className="text-sm font-medium text-purple-800">
                      {goalState.active ? '🎯 Goal Mode' : '📋 Confirm Goal'}
                    </span>
                    {goalState.active && (
                      <span className="text-xs px-1.5 py-0.5 bg-purple-200 text-purple-800 rounded">
                        Iteration {goalState.iteration}/{goalState.maxIterations}
                      </span>
                    )}
                    {goalState.status === 'achieved' && (
                      <span className="text-xs px-1.5 py-0.5 bg-green-200 text-green-800 rounded">✓ Achieved</span>
                    )}
                    {goalState.status === 'aborted' && (
                      <span className="text-xs px-1.5 py-0.5 bg-red-200 text-red-800 rounded">✗ Aborted</span>
                    )}
                    {goalState.status === 'budget_exhausted' && (
                      <span className="text-xs px-1.5 py-0.5 bg-orange-200 text-orange-800 rounded">Budget Exhausted</span>
                    )}
                  </div>
                  <div className="text-sm text-gray-700 mb-2">
                    <strong>Goal:</strong> {goalState.goal}
                  </div>
                  <div className="text-xs text-gray-600">
                    <strong>Success Criteria:</strong>
                    <ol className="list-decimal list-inside mt-1 space-y-0.5">
                      {goalState.criteria.map((c, i) => (
                        <li key={i}>{c}</li>
                      ))}
                    </ol>
                  </div>
                </div>
                <div className="flex gap-2 flex-shrink-0">
                  {!goalState.active ? (
                    <>
                      <button
                        onClick={handleCancelGoal}
                        className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded hover:bg-gray-50 transition-colors"
                      >
                        Cancel
                      </button>
                      <button
                        onClick={handleConfirmGoal}
                        className="px-3 py-1.5 text-sm font-medium text-white bg-purple-600 border border-purple-700 rounded hover:bg-purple-700 transition-colors"
                      >
                        Start Goal
                      </button>
                    </>
                  ) : goalState.status === 'running' ? (
                    <button
                      onClick={handleStopStreaming}
                      className="px-3 py-1.5 text-sm font-medium text-red-700 bg-white border border-red-300 rounded hover:bg-red-50 transition-colors"
                    >
                      Stop
                    </button>
                  ) : (
                    <button
                      onClick={handleCancelGoal}
                      className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded hover:bg-gray-50 transition-colors"
                    >
                      Dismiss
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* Input Area */}
          <div className="border-t border-gray-200 bg-white px-4 py-3">
            {/* Pending Steer Messages Indicator - 在输入框上方 */}
            {pendingSteerMessages.length > 0 && (
              <div className="mb-2 px-3 py-2 bg-gradient-to-r from-amber-50 to-orange-50 border border-amber-200 rounded-lg">
                <div className="flex items-start gap-2">
                  <div className="flex-shrink-0 mt-0.5">
                    <div className="w-1.5 h-1.5 bg-amber-500 rounded-full animate-pulse" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs font-medium text-amber-900">Queued Messages ({pendingSteerMessages.length})</span>
                      <span className="text-xs text-amber-600">Will send after current response</span>
                    </div>
                    <div className="space-y-1">
                      {pendingSteerMessages.map((msg, i) => (
                        <div key={i} className="flex items-center gap-1 text-sm text-gray-700 bg-white/60 px-2 py-1 rounded border border-amber-100 group">
                          <div className="flex-1 min-w-0 truncate">
                            {msg.content}
                            {msg.contextFiles && msg.contextFiles.length > 0 && (
                              <span className="ml-2 inline-flex items-center gap-0.5">
                                <File className="w-3 h-3 text-blue-500" />
                                <span className="text-xs text-blue-600">{msg.contextFiles.length}</span>
                              </span>
                            )}
                          </div>
                          <button
                            onClick={() => setPendingSteerMessages(prev => prev.filter((_, idx) => idx !== i))}
                            className="flex-shrink-0 p-0.5 text-amber-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                            title="Remove this message"
                          >
                            <X className="w-3 h-3" />
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                  <button
                    onClick={() => setPendingSteerMessages([])}
                    className="flex-shrink-0 p-1 text-amber-600 hover:text-amber-800 hover:bg-amber-100 rounded transition-colors"
                    title="Cancel all queued messages"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            )}
            {/* Context Files - 在输入框上方 */}
            {contextFiles.length > 0 && (
              <div className="flex items-center gap-2 flex-wrap mb-2 px-2 py-1.5 bg-blue-50 rounded">
                <span className="text-xs font-medium text-blue-700">Context:</span>
                {contextFiles.map(file => (
                  <span
                    key={file}
                    className="inline-flex items-center gap-1 px-2 py-0.5 bg-white border border-blue-200 rounded text-xs text-blue-700"
                  >
                    <File className="w-3 h-3" />
                    {file.split('/').pop()}
                    <button
                      onClick={() => handleRemoveContext(file)}
                      className="hover:text-red-500"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="flex items-end gap-3 relative">
              <div className="flex-1">
                <Composer
                  value={inputValue}
                  onChange={setInputValue}
                  onSubmit={handleSendMessage}
                  files={fileList}
                />
              </div>
              {isStreaming ? (
                <button
                  onClick={handleStopStreaming}
                  className="p-2 bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors"
                  title="Stop generation"
                >
                  <Square className="w-5 h-5" />
                </button>
              ) : (
                <button
                  onClick={handleSendMessage}
                  disabled={!inputValue.trim()}
                  className="p-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  <Send className="w-5 h-5" />
                </button>
              )}
            </div>
            {/* Agent and Model Selection */}
            <div className="flex items-center gap-2 mt-2">
              <select
                value={selectedAgent}
                onChange={(e) => setSelectedAgent(e.target.value)}
                className={`px-2 py-1 border rounded text-xs focus:outline-none focus:ring-1 max-w-[120px] truncate ${
                  selectedAgent === 'plan'
                    ? 'border-blue-400 bg-blue-50 text-blue-700 focus:ring-blue-500'
                    : 'border-gray-300 focus:ring-blue-500'
                }`}
                title={selectedAgent === 'plan'
                  ? 'Plan 模式：AI 只读，制定计划后由你审批'
                  : 'Build 模式：AI 可以直接执行操作'}
              >
                <option value="build">build</option>
                <option value="plan">plan</option>
              </select>
              {selectedAgent === 'plan' && (
                <span className="text-xs text-blue-600 bg-blue-100 px-2 py-0.5 rounded">
                  📋 计划模式
                </span>
              )}
              <select
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
                className="px-2 py-1 border border-gray-300 rounded text-xs focus:outline-none focus:ring-1 focus:ring-blue-500 max-w-[220px] truncate"
                title={selectedModel}
              >
                {config && Object.entries(config.endpoints).map(([id, endpoint]) => {
                  const provider = endpoint.provider_name || new URL(endpoint.base_url).hostname;
                  const displayName = `${endpoint.model} (${provider})`;
                  return (
                    <option key={id} value={endpoint.model}>
                      {displayName}
                    </option>
                  );
                })}
              </select>
              {/* Permission Mode Toggle */}
              <PermissionSelect
                mode={permissionMode as 'default' | 'acceptEdits' | 'bypassPermissions'}
                onChange={handleSetPermissionMode}
              />
            </div>
          </div>
        </div>

        {/* Right Sidebar - Stacked collapsible panels */}
        {rightSidebarOpen && (
          <div className="relative border-l border-gray-200 bg-white flex flex-col" style={{ width: `${rightSidebarWidth}px` }}>
            {/* Drag handle */}
            <div
              className="absolute top-0 -left-1 w-2 h-full cursor-col-resize group z-10"
              onMouseDown={handleRightDragStart}
            >
              <div className="absolute inset-y-0 right-0 w-0.5 bg-transparent group-hover:bg-blue-400 group-active:bg-blue-500 transition-colors" />
            </div>
            {/* Tab buttons */}
            <div className="flex border-b border-gray-200 flex-shrink-0">
              <button
                onClick={() => setRightTab('files')}
                className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
                  rightTab === 'files' ? 'text-blue-600 border-b-2 border-blue-600' : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                <File className="w-3 h-3 inline mr-1" />
                Files
                {fileSnapshots.length > 0 && (
                  <span className="ml-1 px-1 py-0.5 bg-blue-500 text-white text-[9px] rounded-full">
                    {fileSnapshots.length}
                  </span>
                )}
              </button>
              <button
                onClick={() => setRightTab('control')}
                className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
                  rightTab === 'control' ? 'text-blue-600 border-b-2 border-blue-600' : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                <Sliders className="w-3 h-3 inline mr-1" />
                Control
              </button>
            </div>

            {/* Tab content */}
            <div className="flex-1 overflow-hidden flex flex-col">
              {rightTab === 'control' ? (
                <div className="flex-1 overflow-hidden flex flex-col">
                  {/* Control sub-tabs */}
                  <div className="flex border-b border-gray-200 bg-gray-50">
                    <button
                      onClick={() => setControlSubTab('context')}
                      className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
                        controlSubTab === 'context' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
                      }`}
                    >
                      <Layers className="w-3 h-3 inline mr-1" />
                      Context
                    </button>
                    <button
                      onClick={() => setControlSubTab('wiki')}
                      className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
                        controlSubTab === 'wiki' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
                      }`}
                    >
                      <BookOpen className="w-3 h-3 inline mr-1" />
                      Wiki
                    </button>
                    <button
                      onClick={() => setControlSubTab('plan')}
                      className={`flex-1 px-2 py-1.5 text-xs font-medium transition-colors ${
                        controlSubTab === 'plan' ? 'text-blue-600 border-b-2 border-blue-600 bg-white' : 'text-gray-500 hover:text-gray-700'
                      }`}
                    >
                      <Target className="w-3 h-3 inline mr-1" />
                      Plan
                    </button>
                  </div>
                  {/* Sub-tab content */}
                  <div className="flex-1 overflow-auto">
                    {controlSubTab === 'context' ? (
                      <ContextPanel sessionId={currentSessionId} />
                    ) : controlSubTab === 'wiki' ? (
                      <WikiPanel 
                        cwd={currentCwd}
                        onFileSelect={setSelectedFile}
                        selectedFile={selectedFile}
                      />
                    ) : (
                      <PlanControlPanel
                        sessionId={currentSessionId || ''}
                        planSlug={planSlug || ''}
                      />
                    )}
                  </div>
                </div>
              ) : (
                <div className="flex-1 overflow-auto flex flex-col">
                  <FileTree
                    key={currentSessionId || 'default'}
                    onFileSelect={setSelectedFile}
                    onAddToChat={handleAddToChat}
                    selectedFile={selectedFile}
                    cwd={currentCwd}
                    visible={true}
                    refreshTrigger={fileTreeRefreshTrigger}
                  />
                  {fileSnapshots.length > 0 && (
                    <div className="border-t border-gray-200">
                      <button
                        onClick={() => setChangesExpanded(!changesExpanded)}
                        className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-gray-700 bg-gray-50 hover:bg-gray-100"
                      >
                        <Package className="w-3 h-3" />
                        Changes ({fileSnapshots.length})
                        <span className="ml-auto text-gray-400">
                          {changesExpanded ? '▼' : '▶'}
                        </span>
                      </button>
                      {changesExpanded && (
                        <DeliverablesPanel
                          snapshots={fileSnapshots}
                          onOpenFile={setChangesSelectedFile}
                        />
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}

        {/* File Viewer Overlay */}
        {(() => {
          // Changes file takes priority for diff view
          if (changesSelectedFile) {
            const snapshot = fileSnapshots.find(s => s.file_path === changesSelectedFile);
            if (snapshot) {
              return (
                <div className="w-1/2 border-l border-gray-200">
                  <div className="h-full flex flex-col bg-white">
                    <div className="flex items-center justify-between px-4 py-2 border-b border-gray-200 bg-gray-50">
                      <div className="flex items-center gap-2">
                        <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                          snapshot.is_new ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'
                        }`}>
                          {snapshot.is_new ? 'NEW' : 'EDIT'}
                        </span>
                        <span className="text-sm font-medium text-gray-700">{changesSelectedFile.split('/').pop()}</span>
                        <span className="text-xs text-gray-400">{changesSelectedFile}</span>
                      </div>
                      <button
                        onClick={() => setChangesSelectedFile(null)}
                        className="p-1 hover:bg-gray-200 rounded"
                      >
                        <X className="w-4 h-4" />
                      </button>
                    </div>
                    <div className="flex-1 overflow-hidden p-3">
                      <DiffViewer
                        oldContent={snapshot.old_content}
                        newContent={snapshot.new_content}
                        maxHeight={800}
                      />
                    </div>
                  </div>
                </div>
              );
            }
          }
          
          // Files panel shows file content (editable)
          if (!selectedFile) return null;
          
          // .mycode/ 下的文件可编辑
          const isEditable = selectedFile.includes('.mycode/');
          
          return (
            <div className="w-1/2 border-l border-gray-200">
              <FileViewer
                filePath={selectedFile}
                cwd={currentCwd || undefined}
                onClose={() => setSelectedFile(null)}
                editable={isEditable}
              />
            </div>
          );
        })()}
      </div>
    </PageLayout>
  );
}

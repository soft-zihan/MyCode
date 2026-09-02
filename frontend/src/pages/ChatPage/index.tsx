import { useState, useCallback } from 'react';
import { 
  MessageSquare, PanelRight, Send, Square, Zap, File, X, FolderTree
} from 'lucide-react';
import { ReviewPanel } from '../../components/ReviewPanel';
import { DiffViewer } from '../../components/DiffViewer';
import { ChatView } from '../../components/chat/nodes';
import { FileTree } from './components/FileTree';
import { FileViewer } from './components/FileViewer';
import { SessionsPanel } from './components/SessionsPanel';
import { useChat } from './hooks/useChat';

export default function ChatPage() {
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [sidebarTab, setSidebarTab] = useState<'files' | 'sessions'>('files');

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
    handleAddToChat,
    handleRemoveContext,
    handleSessionSelect,
    handleNewSession,
    handleSendMessage,
    handleStopStreaming,
    handleCompactSession,
    handleToggleYoloMode,
    handleForkAtPoint,
    handleEditMessage,
    handlePermissionApprove,
    handlePermissionDeny,
    handleAcceptFile,
    handleRejectFile,
    handleAcceptAll,
    handleConfirmGoal,
    handleCancelGoal,
  } = useChat();

  const handleOpenFile = useCallback((filePath: string) => {
    setSelectedFile(filePath);
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  return (
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
          <div className="flex items-center gap-2 mr-2">
            <div className="flex items-center gap-2">
              <div className="w-32 h-2 bg-gray-200 rounded-full overflow-hidden">
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
                disabled={!currentSessionId || isStreaming}
                className={`p-1 rounded transition-colors ${
                  !currentSessionId || isStreaming
                    ? 'text-gray-300 cursor-not-allowed'
                    : 'text-gray-600 hover:text-blue-600 hover:bg-blue-50'
                }`}
                title={!currentSessionId ? 'Start a session first' : isStreaming ? 'Wait for response to complete' : '压缩上下文'}
              >
                <Zap className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className={`p-2 rounded hover:bg-gray-100 transition-colors flex-shrink-0 ${
              sidebarOpen ? 'text-blue-600' : 'text-gray-500'
            }`}
            title={sidebarOpen ? 'Close sidebar' : 'Open sidebar'}
          >
            <PanelRight className="w-5 h-5" />
          </button>
        </div>

        {/* Messages */}
        <ChatView
          snapshot={chatSnapshot}
          isStreaming={isStreaming}
          isWaitingResponse={isWaitingResponse}
          onEditMessage={handleEditMessage}
          onFileClick={(path: string) => {
            setSelectedFile(path);
            setSidebarTab('files');
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
        {pendingPermission && (
          <div className="border-t border-yellow-300 bg-yellow-50 px-4 py-3">
            <div className="flex items-start gap-3">
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-yellow-800">⚠️ 需要授权</span>
                  <span className="text-xs px-1.5 py-0.5 bg-yellow-200 text-yellow-800 rounded">
                    {pendingPermission.tool_name}
                  </span>
                </div>
                <pre className="text-xs text-gray-700 bg-white border border-yellow-200 rounded p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap font-mono">
                  {pendingPermission.command}
                </pre>
              </div>
              <div className="flex gap-2 flex-shrink-0">
                <button
                  onClick={handlePermissionDeny}
                  className="px-3 py-1.5 text-sm font-medium text-red-700 bg-white border border-red-300 rounded hover:bg-red-50 transition-colors"
                >
                  拒绝
                </button>
                <button
                  onClick={handlePermissionApprove}
                  className="px-3 py-1.5 text-sm font-medium text-white bg-green-600 border border-green-700 rounded hover:bg-green-700 transition-colors"
                >
                  批准
                </button>
              </div>
            </div>
          </div>
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
          <div className="flex items-end gap-3">
            <textarea
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type a message... (Shift+Enter for newline)"
              className="flex-1 resize-none border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              rows={2}
            />
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
          {/* Pending Steer Message Indicator */}
          {pendingSteerMessage && (
            <div className="mt-2 px-3 py-2 bg-gradient-to-r from-amber-50 to-orange-50 border border-amber-200 rounded-lg">
              <div className="flex items-start gap-2">
                <div className="flex-shrink-0 mt-0.5">
                  <div className="w-1.5 h-1.5 bg-amber-500 rounded-full animate-pulse" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs font-medium text-amber-900">Queued Steer Message</span>
                    <span className="text-xs text-amber-600">Will send after current response</span>
                  </div>
                  <div className="text-sm text-gray-700 bg-white/60 px-2 py-1 rounded border border-amber-100 truncate">
                    {pendingSteerMessage.content}
                  </div>
                </div>
                <button
                  onClick={() => setPendingSteerMessage(null)}
                  className="flex-shrink-0 p-1 text-amber-600 hover:text-amber-800 hover:bg-amber-100 rounded transition-colors"
                  title="Cancel steer message"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          )}
          {/* Context Files */}
          {contextFiles.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap mt-2 px-2 py-1.5 bg-blue-50 rounded">
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
          {/* Agent and Model Selection */}
          <div className="flex items-center gap-2 mt-2">
            <select
              value={selectedAgent}
              onChange={(e) => setSelectedAgent(e.target.value)}
              className="px-2 py-1 border border-gray-300 rounded text-xs focus:outline-none focus:ring-1 focus:ring-blue-500 max-w-[120px] truncate"
              title={selectedAgent}
            >
              <option value="build">build</option>
              <option value="plan">plan</option>
            </select>
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
            {/* YOLO Mode Toggle */}
            <button
              onClick={handleToggleYoloMode}
              className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
                yoloMode
                  ? 'text-green-600 bg-green-50 border border-green-200 hover:bg-green-100'
                  : 'text-gray-600 bg-white border border-gray-200 hover:bg-gray-50'
              }`}
              title={yoloMode ? 'YOLO模式：自动允许所有操作' : '默认模式：需要确认'}
            >
              {yoloMode ? 'YOLO' : 'Default'}
            </button>
          </div>
        </div>
      </div>

      {/* Right Sidebar */}
      {sidebarOpen && (
        <div className="w-72 border-l border-gray-200 bg-white flex flex-col">
          {/* Sidebar Tabs */}
          <div className="flex border-b border-gray-200">
            <button
              className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
                sidebarTab === 'files'
                  ? 'text-blue-600 border-b-2 border-blue-600 bg-blue-50'
                  : 'text-gray-500 hover:text-gray-700'
              }`}
              onClick={() => setSidebarTab('files')}
            >
              <FolderTree className="w-3 h-3 inline mr-1" />
              Files
            </button>
            <button
              className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
                sidebarTab === 'sessions'
                  ? 'text-blue-600 border-b-2 border-blue-600 bg-blue-50'
                  : 'text-gray-500 hover:text-gray-700'
              }`}
              onClick={() => setSidebarTab('sessions')}
            >
              <MessageSquare className="w-3 h-3 inline mr-1" />
              Sessions
            </button>
          </div>

          {/* Sidebar Content */}
          <div className="flex-1 overflow-hidden relative">
            <div className={`absolute inset-0 ${sidebarTab === 'files' ? 'visible' : 'invisible hidden'}`}>
              <FileTree
                onFileSelect={setSelectedFile}
                onAddToChat={handleAddToChat}
                selectedFile={selectedFile}
                cwd={currentCwd}
                visible={sidebarTab === 'files'}
                refreshTrigger={fileTreeRefreshTrigger}
              />
            </div>
            <div className={`absolute inset-0 ${sidebarTab === 'sessions' ? 'visible' : 'invisible hidden'}`}>
              <SessionsPanel
                onSessionSelect={handleSessionSelect}
                onNewSession={handleNewSession}
                currentSessionId={currentSessionId}
                visible={sidebarTab === 'sessions'}
                refreshTrigger={sessionRefreshTrigger}
              />
            </div>
          </div>
        </div>
      )}

      {/* File Viewer Overlay */}
      {selectedFile && (() => {
        const snapshot = fileSnapshots.find(s => s.file_path === selectedFile);
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
                    <span className="text-sm font-medium text-gray-700">{selectedFile.split('/').pop()}</span>
                    <span className="text-xs text-gray-400">{selectedFile}</span>
                  </div>
                  <button
                    onClick={() => setSelectedFile(null)}
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
        return (
          <div className="w-1/2 border-l border-gray-200">
            <FileViewer
              filePath={selectedFile}
              onClose={() => setSelectedFile(null)}
            />
          </div>
        );
      })()}
    </div>
  );
}

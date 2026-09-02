import { WorkspaceNode, Session, AppConfig, PermissionRequest } from '../../api/client';
import { FileSnapshot } from '../../components/ReviewPanel';

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  contextFiles?: string[];
  agent?: string;
  model?: string;
  isEditing?: boolean;
}

export interface ChatState {
  selectedFile: string | null;
  sidebarOpen: boolean;
  sidebarTab: 'files' | 'sessions';
  messages: ChatMessage[];
  inputValue: string;
  contextFiles: string[];
  config: AppConfig | null;
  selectedAgent: string;
  selectedModel: string;
  currentSessionId: string | null;
  currentProject: string | null;
  currentCwd: string | null;
  isStreaming: boolean;
  isWaitingResponse: boolean;
  fileSnapshots: FileSnapshot[];
  fileTreeRefreshTrigger: number;
  yoloMode: boolean;
  contextUsed: number;
  contextTotal: number;
  pendingPermission: PermissionRequest | null;
  sessionRefreshTrigger: number;
}

export type { WorkspaceNode, Session, AppConfig, PermissionRequest, FileSnapshot };

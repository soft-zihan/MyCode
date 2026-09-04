import { useSyncExternalStore, useCallback, useRef } from 'react';
import type { ChatSnapshot } from '../components/chat/nodes/types';

export interface PermissionRequest {
  rpc_id: string;
  request_id: string;
  command: string;
  tool_name: string;
  message?: string;
  sub_agent_id?: string;
}

export interface GoalState {
  active: boolean;
  goal: string;
  criteria: string[];
  iteration: number;
  maxIterations: number;
  status: string;
}

export interface FileSnapshot {
  file_path: string;
  is_new: boolean;
  old_content?: string;
  new_content: string;
}

export interface SessionState {
  sessionId: string | null;
  snapshot: ChatSnapshot;
  projections: {
    title?: string;
    updatedAt?: number;
    running?: boolean;
    lastMessage?: string;
    cwd?: string;
  };
  streamState: {
    active: boolean;
  };
  currentAssistantKey: string | null;
  subAgentInfo: Map<string, { agentType: string; description: string }>;
  hasMore: boolean;
  baseSeq: number;
  lastSeq: number;
  pendingPermission?: PermissionRequest;
  goalState?: GoalState;
  fileSnapshots: FileSnapshot[];
  contextUsed: number;
  contextTotal: number;
}

export function createEmptySessionState(sessionId: string | null = null): SessionState {
  return {
    sessionId,
    snapshot: { order: [], nodes: new Map() },
    projections: {},
    streamState: { active: false },
    currentAssistantKey: null,
    subAgentInfo: new Map(),
    hasMore: false,
    baseSeq: 0,
    lastSeq: -1,
    fileSnapshots: [],
    contextUsed: 0,
    contextTotal: 128000,
  };
}

const emptySnapshot: ChatSnapshot = { order: [], nodes: new Map() };
const EMPTY_ARRAY: FileSnapshot[] = [];

class SessionStore {
  private sessions = new Map<string, SessionState>();
  private currentSessionId: string | null = null;
  private version = 0;
  private listeners = new Set<() => void>();

  private notify(): void {
    this.version++;
    for (const listener of this.listeners) {
      listener();
    }
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  getOrCreate(sessionId: string): SessionState {
    let state = this.sessions.get(sessionId);
    if (!state) {
      state = createEmptySessionState(sessionId);
      this.sessions.set(sessionId, state);
    }
    return state;
  }

  get(sessionId: string): SessionState | undefined {
    return this.sessions.get(sessionId);
  }

  select(sessionId: string): SessionState {
    this.currentSessionId = sessionId;
    this.getOrCreate(sessionId);
    this.notify();
    return this.sessions.get(sessionId)!;
  }

  getCurrentId(): string | null {
    return this.currentSessionId;
  }

  getCurrent(): SessionState | null {
    if (!this.currentSessionId) return null;
    return this.sessions.get(this.currentSessionId) || null;
  }

  getSnapshot(sessionId: string): ChatSnapshot {
    return this.sessions.get(sessionId)?.snapshot ?? emptySnapshot;
  }

  getCurrentSnapshot(): ChatSnapshot {
    if (!this.currentSessionId) return emptySnapshot;
    return this.getSnapshot(this.currentSessionId);
  }

  updateSnapshot(sessionId: string, updater: (s: ChatSnapshot) => ChatSnapshot): void {
    const state = this.getOrCreate(sessionId);
    state.snapshot = updater(state.snapshot);
    this.notify();
  }

  getCurrentAssistantKey(sessionId: string): string | null {
    return this.sessions.get(sessionId)?.currentAssistantKey ?? null;
  }

  setCurrentAssistantKey(sessionId: string, key: string | null): void {
    const state = this.getOrCreate(sessionId);
    state.currentAssistantKey = key;
  }

  getSubAgentInfo(sessionId: string): Map<string, { agentType: string; description: string }> {
    return this.getOrCreate(sessionId).subAgentInfo;
  }

  create(): SessionState {
    // Create a pending session for new conversations
    const pendingId = '__pending__';
    const state = createEmptySessionState(pendingId);
    this.sessions.set(pendingId, state);
    this.currentSessionId = pendingId;
    this.notify();
    return state;
  }

  setCurrentId(sessionId: string): void {
    this.currentSessionId = sessionId;
    const state = this.sessions.get(sessionId);
    if (state) {
      state.sessionId = sessionId;
    }
    this.notify();
  }

  updateProjections(sessionId: string, projections: Partial<SessionState['projections']>): void {
    const state = this.getOrCreate(sessionId);
    state.projections = { ...state.projections, ...projections };
    this.notify();
  }

  getAllProjections(): Map<string, SessionState['projections']> {
    const result = new Map<string, SessionState['projections']>();
    for (const [id, state] of this.sessions) {
      result.set(id, state.projections);
    }
    return result;
  }

  destroy(sessionId: string): void {
    this.sessions.delete(sessionId);
    if (this.currentSessionId === sessionId) {
      this.currentSessionId = null;
    }
    this.notify();
  }

  migrateNodes(fromSessionId: string, toSessionId: string): void {
    const fromState = this.sessions.get(fromSessionId);
    if (!fromState) return;
    
    const toState = this.getOrCreate(toSessionId);
    toState.snapshot = fromState.snapshot;
    toState.currentAssistantKey = fromState.currentAssistantKey;
    toState.subAgentInfo = new Map(fromState.subAgentInfo);
    toState.hasMore = fromState.hasMore;
    toState.baseSeq = fromState.baseSeq;
    toState.lastSeq = fromState.lastSeq;
    
    this.sessions.delete(fromSessionId);
    this.notify();
  }

  setPagination(sessionId: string, hasMore: boolean, baseSeq: number, lastSeq: number): void {
    const state = this.getOrCreate(sessionId);
    state.hasMore = hasMore;
    state.baseSeq = baseSeq;
    state.lastSeq = lastSeq;
    this.notify();
  }

  updateLastSeq(sessionId: string, seq: number): void {
    const state = this.sessions.get(sessionId);
    if (state && seq > state.lastSeq) {
      state.lastSeq = seq;
    }
  }

  getPendingPermission(sessionId: string): PermissionRequest | undefined {
    return this.sessions.get(sessionId)?.pendingPermission;
  }

  setPendingPermission(sessionId: string, perm: PermissionRequest | undefined): void {
    const state = this.getOrCreate(sessionId);
    state.pendingPermission = perm;
    this.notify();
  }

  getGoalState(sessionId: string): GoalState | undefined {
    return this.sessions.get(sessionId)?.goalState;
  }

  setGoalState(sessionId: string, state: GoalState | undefined): void {
    const sessionState = this.getOrCreate(sessionId);
    sessionState.goalState = state;
    this.notify();
  }

  getFileSnapshots(sessionId: string): FileSnapshot[] {
    return this.sessions.get(sessionId)?.fileSnapshots ?? EMPTY_ARRAY;
  }

  setFileSnapshots(sessionId: string, snaps: FileSnapshot[]): void {
    const state = this.sessions.get(sessionId);
    if (state && state.fileSnapshots === snaps) return;
    const s = this.getOrCreate(sessionId);
    s.fileSnapshots = snaps;
    this.notify();
  }

  addFileSnapshot(sessionId: string, snap: FileSnapshot): void {
    const state = this.getOrCreate(sessionId);
    const filtered = state.fileSnapshots.filter(s => s.file_path !== snap.file_path);
    state.fileSnapshots = [...filtered, snap];
    this.notify();
  }

  getContextUsed(sessionId: string): number {
    return this.sessions.get(sessionId)?.contextUsed ?? 0;
  }

  getContextTotal(sessionId: string): number {
    return this.sessions.get(sessionId)?.contextTotal ?? 128000;
  }

  setContextStats(sessionId: string, used: number, total: number): void {
    const state = this.getOrCreate(sessionId);
    if (state.contextUsed === used && state.contextTotal === total) return;
    state.contextUsed = used;
    state.contextTotal = total;
    this.notify();
  }

  getVersion(): number {
    return this.version;
  }
}

export const sessionStore = new SessionStore();

export function useSessionStore<T>(selector: (store: SessionStore) => T): T {
  const selectorRef = useRef(selector);
  selectorRef.current = selector;
  
  const getSnapshot = useCallback(() => selectorRef.current(sessionStore), []);
  
  return useSyncExternalStore(
    (cb) => sessionStore.subscribe(cb),
    getSnapshot,
    getSnapshot,
  );
}

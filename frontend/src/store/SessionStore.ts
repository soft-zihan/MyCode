import { useSyncExternalStore, useCallback, useRef } from 'react';
import type { ChatSnapshot } from '../components/chat/nodes/types';
import { DEFAULT_CONTEXT_WINDOW } from '../api/client';

export interface PermissionRequest {
  rpc_id: string;
  request_id: string;
  command: string;
  tool_name: string;
  message?: string;
  sub_agent_id?: string;
}

export interface QuestionRequest {
  request_id: string;
  question: string;
  options?: string[];
  context?: string;
}

export interface TodoItem {
  id: number;
  content: string;
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled';
  priority: 'high' | 'medium' | 'low';
  created_at: string;
  updated_at: string;
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
    plan_slug?: string;
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
  pendingQuestion?: QuestionRequest;
  todos?: TodoItem[];
  fileSnapshots: FileSnapshot[];
  contextUsed: number;
  contextTotal: number;
  statsInputTokens: number;
  statsOutputTokens: number;
  statsCachedTokens: number;
  breakdown: Record<string, any> | null;
  permissionMode: string;
  _statsCache?: { inputTokens: number; outputTokens: number; cachedTokens: number };
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
    contextTotal: DEFAULT_CONTEXT_WINDOW,
    statsInputTokens: 0,
    statsOutputTokens: 0,
    statsCachedTokens: 0,
    breakdown: null,
    permissionMode: 'default',
  };
}

const emptySnapshot: ChatSnapshot = { order: [], nodes: new Map() };
const EMPTY_ARRAY: FileSnapshot[] = [];
const EMPTY_TODO_ARRAY: TodoItem[] = [];
const EMPTY_STATS = { inputTokens: 0, outputTokens: 0, cachedTokens: 0 };

class SessionStore {
  private sessions = new Map<string, SessionState>();
  private currentSessionId: string | null = null;
  private version = 0;
  private _projectionsVersion = 0;
  private _projectionsSnapshot: Map<string, SessionState['projections']> | null = null;
  private listeners = new Set<() => void>();
  private planRevisions = new Map<string, number>();

  private notify(): void {
    this.version++;
    this._projectionsSnapshot = null;
    for (const listener of this.listeners) {
      listener();
    }
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  getPlanRevision(sessionId: string): number {
    return this.planRevisions.get(sessionId) ?? 0;
  }

  bumpPlanRevision(sessionId: string): void {
    this.planRevisions.set(sessionId, (this.planRevisions.get(sessionId) ?? 0) + 1);
    this.notify();
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
    let changed = false;
    for (const [k, v] of Object.entries(projections)) {
      if ((state.projections as any)[k] !== v) { changed = true; break; }
    }
    if (!changed) return;
    state.projections = { ...state.projections, ...projections };
    this._projectionsVersion++;
    this.notify();
  }

  getAllProjections(): Map<string, SessionState['projections']> {
    if (this._projectionsSnapshot) return this._projectionsSnapshot;
    const result = new Map<string, SessionState['projections']>();
    for (const [id, state] of this.sessions) {
      result.set(id, state.projections);
    }
    this._projectionsSnapshot = result;
    return result;
  }

  getProjectionsVersion(): number {
    return this._projectionsVersion;
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

  getPendingQuestion(sessionId: string): QuestionRequest | undefined {
    return this.sessions.get(sessionId)?.pendingQuestion;
  }

  setPendingQuestion(sessionId: string, question: QuestionRequest | undefined): void {
    const state = this.getOrCreate(sessionId);
    state.pendingQuestion = question;
    this.notify();
  }

  getTodos(sessionId: string): TodoItem[] {
    return this.sessions.get(sessionId)?.todos ?? EMPTY_TODO_ARRAY;
  }

  setTodos(sessionId: string, todos: TodoItem[]): void {
    const state = this.getOrCreate(sessionId);
    state.todos = todos;
    this.notify();
  }

  getPlanSlug(sessionId: string): string | undefined {
    return this.sessions.get(sessionId)?.projections?.plan_slug;
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
    return this.sessions.get(sessionId)?.contextTotal ?? DEFAULT_CONTEXT_WINDOW;
  }

  setContextTotal(sessionId: string, total: number): void {
    const state = this.getOrCreate(sessionId);
    if (state.contextTotal === total) return;
    state.contextTotal = total;
    this.notify();
  }

  setContextStats(sessionId: string, used: number, total: number): void {
    const state = this.getOrCreate(sessionId);
    if (state.contextUsed === used && state.contextTotal === total) return;
    state.contextUsed = used;
    state.contextTotal = total;
    this.notify();
  }

  setDetailedStats(sessionId: string, inputTokens: number, outputTokens: number, cachedTokens: number): void {
    const state = this.getOrCreate(sessionId);
    if (state.statsInputTokens === inputTokens && 
        state.statsOutputTokens === outputTokens && 
        state.statsCachedTokens === cachedTokens) return;
    state.statsInputTokens = inputTokens;
    state.statsOutputTokens = outputTokens;
    state.statsCachedTokens = cachedTokens;
    state._statsCache = { inputTokens, outputTokens, cachedTokens };
    this.notify();
  }

  setBreakdown(sessionId: string, breakdown: Record<string, any> | null): void {
    const state = this.getOrCreate(sessionId);
    state.breakdown = breakdown;
    this.notify();
  }

  getBreakdown(sessionId: string): Record<string, any> | null {
    return this.sessions.get(sessionId)?.breakdown ?? null;
  }

  setPermissionMode(sessionId: string, mode: string): void {
    const state = this.getOrCreate(sessionId);
    if (state.permissionMode === mode) return;
    state.permissionMode = mode;
    this.notify();
  }

  getPermissionMode(sessionId: string): string {
    return this.sessions.get(sessionId)?.permissionMode ?? 'default';
  }

  getDetailedStats(sessionId: string): { inputTokens: number; outputTokens: number; cachedTokens: number } {
    const state = this.sessions.get(sessionId);
    if (!state) return EMPTY_STATS;
    if (!state._statsCache) {
      state._statsCache = {
        inputTokens: state.statsInputTokens,
        outputTokens: state.statsOutputTokens,
        cachedTokens: state.statsCachedTokens,
      };
    }
    return state._statsCache;
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

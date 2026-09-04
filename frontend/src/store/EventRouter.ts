/**
 * Event Router - Routes WebSocket events to the correct SessionState.
 * 
 * Receives events from WebSocketManager and routes them to the appropriate
 * session's snapshot based on session_id.
 * 
 * Features:
 * - Routes events to correct session by session_id
 * - Updates session projections (title, updatedAt, running, etc.)
 * - Gap detection and repair for WebSocket reconnection
 */

import { wsManager } from './WebSocketManager';
import { sessionStore } from './SessionStore';

type EventListener = (event: any) => void;

class EventRouter {
  private listeners = new Set<EventListener>();
  private unsubscribe: (() => void) | null = null;
  private repairingGaps = new Set<string>();
  
  /**
   * Start listening to WebSocket events.
   */
  start(): void {
    if (this.unsubscribe) {
      return;
    }
    
    this.unsubscribe = wsManager.subscribe((event) => {
      this.routeEvent(event);
    });
  }
  
  /**
   * Stop listening to WebSocket events.
   */
  stop(): void {
    if (this.unsubscribe) {
      this.unsubscribe();
      this.unsubscribe = null;
    }
  }
  
  /**
   * Route event to the correct session.
   */
  private routeEvent(event: any): void {
    const sessionId = event.session_id;
    if (!sessionId) {
      return;
    }
    
    const state = sessionStore.getOrCreate(sessionId);
    
    // Deduplication: check if we've already processed this event
    if (event.seq !== undefined && event.seq <= state.lastSeq) {
      // Already processed this event, skip
      return;
    }
    
    // Gap detection: check if event.seq > lastSeq + 1
    if (event.seq !== undefined && state.lastSeq >= 0) {
      const expectedSeq = state.lastSeq + 1;
      if (event.seq > expectedSeq && !this.repairingGaps.has(sessionId)) {
        console.warn(`[EventRouter] gap detected: expected ${expectedSeq}, got ${event.seq}`);
        this.repairGap(sessionId, expectedSeq, event.seq);
      }
    }
    
    // Update lastSeq
    if (event.seq !== undefined) {
      sessionStore.updateLastSeq(sessionId, event.seq);
    }
    
    // Update session projections
    this.updateProjections(sessionId, event);
    
    // Notify listeners
    for (const listener of this.listeners) {
      try {
        listener(event);
      } catch (err) {
        console.error('[EventRouter] listener error:', err);
      }
    }
  }
  
  /**
   * Update session projections based on event.
   */
  private updateProjections(sessionId: string, event: any): void {
    const eventType = event.type;
    
    if (eventType === 'session/created') {
      sessionStore.updateProjections(sessionId, {
        cwd: event.cwd,
        running: true,
        updatedAt: Date.now(),
      });
    } else if (eventType === 'session/title') {
      sessionStore.updateProjections(sessionId, {
        title: event.title,
      });
    } else if (eventType === 'turn/start') {
      sessionStore.updateProjections(sessionId, {
        running: true,
      });
    } else if (eventType === 'turn/end') {
      sessionStore.updateProjections(sessionId, {
        running: false,
        updatedAt: Date.now(),
        title: event.name,
      });
    } else if (eventType === 'user_message') {
      sessionStore.updateProjections(sessionId, {
        updatedAt: Date.now(),
      });
    }
  }
  
  /**
   * Repair gap by fetching missing events from server.
   */
  private async repairGap(sessionId: string, fromSeq: number, toSeq: number): Promise<void> {
    if (this.repairingGaps.has(sessionId)) {
      return;
    }
    
    this.repairingGaps.add(sessionId);
    
    try {
      console.log(`[EventRouter] repairing gap: from ${fromSeq} to ${toSeq}`);
      const response = await fetch(`/api/sessions/${sessionId}/events?from_seq=${fromSeq}&to_seq=${toSeq}`);
      
      if (!response.ok) {
        console.error(`[EventRouter] gap repair failed: ${response.status}`);
        return;
      }
      
      const { events } = await response.json();
      
      if (events && events.length > 0) {
        console.log(`[EventRouter] repaired ${events.length} missing events`);
        for (const event of events) {
          for (const listener of this.listeners) {
            try {
              listener(event);
            } catch (err) {
              console.error('[EventRouter] listener error during repair:', err);
            }
          }
        }
      }
    } catch (err) {
      console.error('[EventRouter] gap repair error:', err);
    } finally {
      this.repairingGaps.delete(sessionId);
    }
  }
  
  /**
   * Load more historical events for a session (lazy loading).
   */
  async loadMoreEvents(sessionId: string, limit: number = 50): Promise<boolean> {
    const state = sessionStore.get(sessionId);
    if (!state || !state.hasMore) {
      return false;
    }
    
    try {
      const response = await fetch(`/api/sessions/${sessionId}/events?before=${state.baseSeq}&limit=${limit}`);
      
      if (!response.ok) {
        console.error(`[EventRouter] loadMore failed: ${response.status}`);
        return false;
      }
      
      const { events, has_more, base_seq } = await response.json();
      
      if (events && events.length > 0) {
        // Notify listeners to prepend events
        for (const event of events) {
          for (const listener of this.listeners) {
            try {
              listener({ ...event, _prepend: true });
            } catch (err) {
              console.error('[EventRouter] listener error during loadMore:', err);
            }
          }
        }
        
        // Update pagination state
        sessionStore.setPagination(sessionId, has_more, base_seq, state.lastSeq);
        return true;
      }
      
      return false;
    } catch (err) {
      console.error('[EventRouter] loadMore error:', err);
      return false;
    }
  }
  
  /**
   * Subscribe to all events.
   * Returns unsubscribe function.
   */
  subscribe(listener: EventListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }
}

// Global singleton
export const eventRouter = new EventRouter();

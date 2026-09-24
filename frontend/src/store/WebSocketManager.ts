/**
 * WebSocket Manager - Single connection multiplexing all session events.
 * 
 * Replaces SSE with a single WebSocket connection that receives events
 * from all sessions. Events are routed by session_id to the correct
 * SessionState in SessionStore.
 */

import { logger } from '../utils/logger';
import { getToken, notifyAuthRequired } from '../api/auth';

type EventListener = (event: any) => void;

class WebSocketManager {
  private ws: WebSocket | null = null;
  private reconnectTimer: number | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 10;
  private listeners = new Set<EventListener>();
  private messageQueue: any[] = [];
  private shouldReconnect = true;
  
  /**
   * Connect to WebSocket server.
   * Idempotent: calling multiple times has no effect.
   */
  connect(): void {
    // U13 修复：原 `if (this.started) return` 使 onclose→scheduleReconnect→connect
    // 永远空转（started 已置 true），断线重连机制实际失效。幂等性由 readyState
    // 检查保证即可；stop() 通过 shouldReconnect=false 阻断自动重连。
    if (this.ws?.readyState === WebSocket.OPEN || this.ws?.readyState === WebSocket.CONNECTING) {
      return;
    }
    
    this.shouldReconnect = true;
    // U13：token 鉴权启用时 WS 握手带 query token（浏览器 WS 无法设 header）
    const token = getToken();
    const wsUrl = `ws://${window.location.hostname}:5555/ws/events${
      token ? `?token=${encodeURIComponent(token)}` : ''
    }`;
    logger.info('[WS] connecting to', wsUrl);
    
    this.ws = new WebSocket(wsUrl);
    
    this.ws.onopen = () => {
      logger.info('[WS] connected');
      this.reconnectAttempts = 0;
      
      // Flush message queue
      while (this.messageQueue.length > 0) {
        const msg = this.messageQueue.shift();
        this.send(msg);
      }
    };
    
    this.ws.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        logger.debug('[WS] received event:', event.type, event.session_id);
        // Notify all listeners
        for (const listener of this.listeners) {
          try {
            listener(event);
          } catch (err) {
            logger.error('[WS] listener error:', err);
          }
        }
      } catch (err) {
        logger.error('[WS] parse error:', err);
      }
    };
    
    this.ws.onclose = (ev) => {
      logger.warn('[WS] disconnected', ev.code);
      this.ws = null;
      // U13：4401 = token 鉴权拒绝——重连无意义，弹 AuthGate 让用户输入 token
      if (ev.code === 4401) {
        notifyAuthRequired();
        return;
      }
      // 只有在显式 stop 后才不重连
      if (this.shouldReconnect) {
        this.scheduleReconnect();
      }
    };
    
    this.ws.onerror = (e) => {
      logger.error('[WS] error:', e);
    };
  }
  
  /**
   * Schedule reconnection with exponential backoff.
   */
  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) {
      return;
    }
    
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      logger.error('[WS] max reconnect attempts reached');
      return;
    }
    
    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
    logger.info(`[WS] reconnecting in ${delay}ms (attempt ${this.reconnectAttempts + 1})`);
    
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.reconnectAttempts++;
      this.connect();
    }, delay);
  }
  
  /**
   * Send command to server.
   */
  send(command: { type: string; session_id?: string; payload?: any }): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(command));
    } else {
      // Queue message for later
      this.messageQueue.push(command);
    }
  }
  
  /**
   * Subscribe to a specific session.
   */
  subscribeSession(sessionId: string): void {
    this.send({ type: 'subscribe', session_id: sessionId });
  }
  
  /**
   * Unsubscribe from a specific session.
   */
  unsubscribeSession(sessionId: string): void {
    this.send({ type: 'unsubscribe', session_id: sessionId });
  }
  
  /**
   * Subscribe to events.
   * Returns unsubscribe function.
   */
  subscribe(listener: EventListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }
  
  /**
   * Disconnect from server.
   */
  disconnect(): void {
    this.shouldReconnect = false;
    
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    
    this.listeners.clear();
    this.messageQueue = [];
  }
  
  /**
   * Stop and cleanup. Alias for disconnect.
   */
  stop(): void {
    this.disconnect();
  }
  
  /**
   * Check if connected.
   */
  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

// Global singleton
export const wsManager = new WebSocketManager();

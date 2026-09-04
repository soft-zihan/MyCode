/**
 * 活跃的 SSE 流状态
 */
export interface ActiveStream {
  sessionId: string;
  abortController: AbortController;
  startTime: number;
}

/**
 * StreamManager - 管理活跃的 SSE 流
 * 
 * 核心设计：
 * 1. 每个流绑定 session_id，事件过滤简单
 * 2. 自动取消旧流，避免竞态
 * 3. 集中管理流状态
 * 
 * 参考 deepseek-harness 的双通道事件流设计：
 * - Mux 流：session 特定事件
 * - Host 流：全局事件
 * 
 * 我们简化为单通道，但通过 StreamManager 实现流隔离。
 */
class StreamManager {
  private activeStream: ActiveStream | null = null;
  private listeners = new Set<() => void>();
  
  /**
   * 开始新流（自动取消旧流）
   * 
   * @param sessionId 流对应的 session ID
   * @returns AbortController 用于取消流
   */
  start(sessionId: string): AbortController {
    // 如果有活跃流，先取消
    if (this.activeStream) {
      console.log('[StreamManager] cancelling previous stream for session:', this.activeStream.sessionId);
      this.activeStream.abortController.abort();
    }
    
    const abortController = new AbortController();
    this.activeStream = {
      sessionId,
      abortController,
      startTime: Date.now(),
    };
    
    console.log('[StreamManager] started stream for session:', sessionId);
    this.notify();
    
    return abortController;
  }
  
  /**
   * 结束流
   */
  end(sessionId: string): void {
    if (this.activeStream?.sessionId === sessionId) {
      console.log('[StreamManager] ended stream for session:', sessionId);
      this.activeStream = null;
      this.notify();
    }
  }
  
  /**
   * 检查流是否属于当前 session
   */
  isCurrent(sessionId: string): boolean {
    return this.activeStream?.sessionId === sessionId;
  }
  
  /**
   * 获取当前活跃的流
   */
  getActive(): ActiveStream | null {
    return this.activeStream;
  }
  
  /**
   * 取消当前流
   */
  cancel(): void {
    if (this.activeStream) {
      console.log('[StreamManager] cancelling stream for session:', this.activeStream.sessionId);
      this.activeStream.abortController.abort();
      this.activeStream = null;
      this.notify();
    }
  }
  
  /**
   * 检查是否有活跃的流
   */
  isActive(): boolean {
    return this.activeStream !== null;
  }
  
  /**
   * 订阅状态变化
   */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  
  /**
   * 通知所有监听者
   */
  private notify(): void {
    for (const listener of this.listeners) {
      listener();
    }
  }
}

// 全局单例
export const streamManager = new StreamManager();

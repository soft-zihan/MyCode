/**
 * 前端日志工具
 * 将日志发送到后端保存到文件
 */

type LogLevel = 'debug' | 'info' | 'warn' | 'error';

class Logger {
  private enabled = true;

  debug(...args: any[]): void {
    if (this.enabled) {
      console.debug('[DEBUG]', ...args);
      this.sendToBackend('debug', args);
    }
  }

  info(...args: any[]): void {
    if (this.enabled) {
      console.info('[INFO]', ...args);
      this.sendToBackend('info', args);
    }
  }

  warn(...args: any[]): void {
    if (this.enabled) {
      console.warn('[WARN]', ...args);
      this.sendToBackend('warn', args);
    }
  }

  error(...args: any[]): void {
    if (this.enabled) {
      console.error('[ERROR]', ...args);
      this.sendToBackend('error', args);
    }
  }

  private sendToBackend(level: LogLevel, args: any[]): void {
    const message = args.map(arg => {
      if (typeof arg === 'object') {
        try {
          return JSON.stringify(arg);
        } catch {
          return String(arg);
        }
      }
      return String(arg);
    }).join(' ');

    // 异步发送，不阻塞主流程
    fetch('/api/frontend-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, level }),
    }).catch(() => {
      // 忽略发送失败
    });
  }

  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }
}

export const logger = new Logger();

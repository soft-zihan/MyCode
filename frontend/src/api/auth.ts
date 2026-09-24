/**
 * U13 远程访问安全：可选 token 鉴权的前端侧。
 *
 * - token 存 localStorage（MYCODE_AUTH_TOKEN 启用后用户输入一次）。
 * - installAuthFetch() 在入口全局包装 window.fetch：同源 /api 请求自动带
 *   Authorization: Bearer；收到 401 广播 'mycode:auth-required' 事件（AuthGate 弹输入框）。
 * - WS 握手无法带 header → WebSocketManager 以 query token 连接；服务端 4401 关闭。
 */

const TOKEN_KEY = 'mycode_auth_token';
export const AUTH_REQUIRED_EVENT = 'mycode:auth-required';

export function getToken(): string | null {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

export function notifyAuthRequired(): void {
  window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
}

function isApiUrl(url: string): boolean {
  return url.startsWith('/api') || url.startsWith(`${window.location.origin}/api`);
}

let installed = false;

export function installAuthFetch(): void {
  if (installed) return;
  installed = true;

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url;
    const token = getToken();
    if (token && isApiUrl(url)) {
      const headers = new Headers(init?.headers ?? (typeof input !== 'string' && !(input instanceof URL) ? input.headers : undefined));
      if (!headers.has('Authorization')) {
        headers.set('Authorization', `Bearer ${token}`);
      }
      init = { ...init, headers };
    }
    const response = await originalFetch(input, init);
    if (response.status === 401 && isApiUrl(url)) {
      notifyAuthRequired();
    }
    return response;
  };
}

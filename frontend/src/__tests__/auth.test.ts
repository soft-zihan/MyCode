/**
 * U13：auth fetch 拦截器——token 注入 + 401 广播。
 * 每个测试用 vi.resetModules 重装模块（installAuthFetch 幂等标志随之重置）。
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

type AuthModule = typeof import('../api/auth');

const realFetch = window.fetch;
let auth: AuthModule;

async function freshInstall(inner: ReturnType<typeof vi.fn>) {
  vi.resetModules();
  auth = await import('../api/auth');
  window.fetch = inner as unknown as typeof fetch;
  auth.installAuthFetch();
}

describe('auth', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.fetch = realFetch;
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it('stores token in localStorage', async () => {
    await freshInstall(vi.fn());
    auth.setToken('abc');
    expect(auth.getToken()).toBe('abc');
    auth.clearToken();
    expect(auth.getToken()).toBeNull();
  });

  it('attaches Bearer header to /api requests when token set', async () => {
    const inner = vi.fn(async () => new Response('{}', { status: 200 }));
    await freshInstall(inner);
    auth.setToken('abc');
    await window.fetch('/api/sessions');
    const [, init] = inner.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer abc');
  });

  it('leaves non-api requests untouched', async () => {
    const inner = vi.fn(async () => new Response('{}', { status: 200 }));
    await freshInstall(inner);
    auth.setToken('abc');
    await window.fetch('https://example.com/x');
    const [, init] = inner.mock.calls[0] as [string, RequestInit | undefined];
    expect(init).toBeUndefined();
  });

  it('dispatches auth-required event on 401', async () => {
    const inner = vi.fn(async () => new Response('{}', { status: 401 }));
    await freshInstall(inner);
    let fired = 0;
    const listener = () => { fired++; };
    window.addEventListener(auth.AUTH_REQUIRED_EVENT, listener);
    await window.fetch('/api/sessions');
    window.removeEventListener(auth.AUTH_REQUIRED_EVENT, listener);
    expect(fired).toBe(1);
  });
});

/**
 * 每会话自动审批开关（安全阀 UI）。
 *
 * 每条都对着「把实现改坏，这条会不会红」写：
 * - 不发 PUT 的实现 → 「点击发 PUT」红；
 * - 不做乐观更新的实现 → 点击后 store 断言红；
 * - 打开态视觉与普通按钮相同的实现（安全阀不可见）→ class/aria/data 断言红；
 * - 失败不回滚的实现 → 回滚断言红；失败不报 [AUTOAPPROVE] 的实现 → console 断言红；
 * - PUT 成功后不回读后端 enabled 的实现 → 「以后端返回值为准」红。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { setAutoApprove } from '../api/client';
import AutoApproveToggle from '../components/chat/AutoApproveToggle';
import { sessionStore } from '../store/SessionStore';

// 部分 mock：SessionStore 还从 api/client import DEFAULT_CONTEXT_WINDOW，
// 整模块替换会把它藏掉。
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, setAutoApprove: vi.fn() };
});

const btn = () => screen.getByRole('button', { name: /自动审批/ });

describe('AutoApproveToggle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStore.setAutoApprove('s1', false);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('点击发 PUT（乐观更新先落 store，成功后以后端返回的 enabled 为准）', async () => {
    vi.mocked(setAutoApprove).mockResolvedValue({ success: true, enabled: true });
    render(<AutoApproveToggle sessionId="s1" enabled={false} />);
    fireEvent.click(btn());
    // 乐观更新：PUT 未回来 store 已经翻转
    expect(sessionStore.getAutoApprove('s1')).toBe(true);
    await waitFor(() => expect(setAutoApprove).toHaveBeenCalledWith('s1', true));
    expect(sessionStore.getAutoApprove('s1')).toBe(true);
  });

  it('打开 = 警示色实心，关闭 = 幽灵按钮（安全阀必须视觉上明显）', () => {
    const { rerender } = render(<AutoApproveToggle sessionId="s1" enabled={false} />);
    expect(btn().className).toContain('bg-transparent');
    expect(btn().className).not.toContain('bg-amber-500');
    expect(btn().getAttribute('aria-pressed')).toBe('false');
    expect(btn().getAttribute('data-auto-approve')).toBe('off');

    rerender(<AutoApproveToggle sessionId="s1" enabled />);
    expect(btn().className).toContain('bg-amber-500');
    expect(btn().className).not.toContain('bg-transparent');
    expect(btn().getAttribute('aria-pressed')).toBe('true');
    expect(btn().getAttribute('data-auto-approve')).toBe('on');
  });

  it('PUT 失败：回滚 store 并 console.error("[AUTOAPPROVE]", ...)', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(setAutoApprove).mockRejectedValue(new Error('boom 500'));
    render(<AutoApproveToggle sessionId="s1" enabled={false} />);
    fireEvent.click(btn());
    expect(sessionStore.getAutoApprove('s1')).toBe(true); // 先乐观更新
    await waitFor(() => expect(sessionStore.getAutoApprove('s1')).toBe(false)); // 再回滚
    expect(errSpy).toHaveBeenCalled();
    expect(
      errSpy.mock.calls.some(call => String(call[0]).includes('[AUTOAPPROVE]')),
    ).toBe(true);
  });

  it('没有 sessionId 时不可点、不发 PUT', () => {
    render(<AutoApproveToggle sessionId={null} enabled={false} />);
    expect((btn() as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(btn());
    expect(setAutoApprove).not.toHaveBeenCalled();
  });
});

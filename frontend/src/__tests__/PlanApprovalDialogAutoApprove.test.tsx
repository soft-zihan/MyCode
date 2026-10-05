/**
 * 缺陷 2：审批对话框打开时，自动审批开关必须在对话框内可达。
 *
 * 遮罩（fixed inset-0 bg-black/20 z-40，onClick=拒绝）拦截指针事件，composer 里的
 * 开关在对话框打开期间点不到；而这个开关的全部使用场景恰恰是「人要走开了，让后面的
 * 确认自动过」——产生这个念头的时刻屏幕上就摆着这个对话框。修法是在 footer 里复用
 * 同一个 AutoApproveToggle 组件、绑定同一个 SessionStore 每会话槽位。
 *
 * 每条都对着「把实现改坏，这条会不会红」写：
 * - footer 里没有开关的实现（只在 composer）→ dialogToggle() 抛错，三条全红；
 * - 对话框里另存一份本地 useState（不订阅/不写 store）→「composer 跟着变」红；
 * - 不发 PUT 的实现 →「发出 PUT」红；
 * - title 没写清「当前这个审批也会立即被自动批准」→ 关键子串断言红
 *   （只断言子串不断言整句，改标点不红）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor, within, act } from '@testing-library/react';
import { setAutoApprove, getPlanDraftArtifacts } from '../api/client';
import { PlanApprovalDialog } from '../components/agent/PlanApprovalDialog';
import AutoApproveToggle from '../components/chat/AutoApproveToggle';
import { sessionStore, useSessionStore } from '../store/SessionStore';
import type { PermissionRequest } from '../store/SessionStore';

// 部分 mock：SessionStore 还从 api/client import DEFAULT_CONTEXT_WINDOW，
// 整模块替换会把它藏掉（同 AutoApproveToggle.test.tsx 的做法）。
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, setAutoApprove: vi.fn(), getPlanDraftArtifacts: vi.fn() };
});

const request: PermissionRequest = {
  rpc_id: 'r1',
  request_id: 'r1',
  // 非阻塞审批（spec §D3/D4）：command 直接承载计划全文，工具名是哨兵值 plan_approval
  command: '## 计划\n- [ ] 1. 做一件事',
  tool_name: 'plan_approval',
};

/** 复刻生产接线：composer（ChatPage）里的开关与对话框里的开关订阅同一个 store 槽位。 */
function Harness() {
  const autoApprove = useSessionStore(() => sessionStore.getAutoApprove('s1'));
  return (
    <>
      <PlanApprovalDialog request={request} sessionId="s1" onApprove={vi.fn()} onDeny={vi.fn()} />
      <div data-testid="composer-slot">
        <AutoApproveToggle sessionId="s1" enabled={autoApprove} />
      </div>
    </>
  );
}

const composerToggle = () =>
  within(screen.getByTestId('composer-slot')).getByRole('button', { name: /自动审批/ });

const dialogToggle = () => {
  const composer = composerToggle();
  const btn = screen
    .getAllByRole('button', { name: /自动审批/ })
    .find((b) => b !== composer);
  if (!btn) throw new Error('对话框 footer 里没有自动审批开关');
  return btn;
};

describe('PlanApprovalDialog 自动审批开关', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStore.setAutoApprove('s1', false);
    vi.mocked(getPlanDraftArtifacts).mockResolvedValue({ success: false, message: 'none' });
    vi.mocked(setAutoApprove).mockResolvedValue({ success: true, enabled: true });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('footer 里有开关，aria-pressed 反映会话级状态（store 翻转即跟着变）', () => {
    render(<Harness />);
    expect(dialogToggle().getAttribute('aria-pressed')).toBe('false');
    // 同一份状态：别处写 store（composer 翻转 / 切会话读回后端值），对话框里跟着变
    act(() => {
      sessionStore.setAutoApprove('s1', true);
    });
    expect(dialogToggle().getAttribute('aria-pressed')).toBe('true');
  });

  it('在对话框里翻转开关 → 发出 PUT → composer 里那个开关的 aria-pressed 也变', async () => {
    render(<Harness />);
    fireEvent.click(dialogToggle());
    await waitFor(() => expect(setAutoApprove).toHaveBeenCalledWith('s1', true));
    expect(dialogToggle().getAttribute('aria-pressed')).toBe('true');
    // 钉住「同一份状态，不是两份各自维护」：对话框里若另存本地副本，这条红
    expect(composerToggle().getAttribute('aria-pressed')).toBe('true');
  });

  it('title 写清「打开后当前这个审批也会立即被自动批准」', () => {
    render(<Harness />);
    const title = dialogToggle().getAttribute('title') || '';
    expect(title).toContain('当前这个审批');
    expect(title).toContain('立即被自动批准');
  });
});

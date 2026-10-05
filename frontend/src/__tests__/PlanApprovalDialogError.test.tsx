/**
 * spec §十.6 的前端半边：后端报告「这次应答没有落地」时，对话框必须把原因显示出来。
 *
 * 后端那一半（重启后从盘上重建待审批、重建失败回明确错误）由
 * tests/unit/test_plan_approval_rebuild.py 钉住；这里钉的是**显示**这一环。
 * 缺陷形状是「UI 显示成功、实际无效、零可观测」，所以只显示还不够——
 * useChat 在 success === false 时**不关**对话框（那条接线由 ChatPage 的
 * handlePermissionApprove 负责，本文件钉的是它把消息交给谁、以及那个消息看不看得见）。
 *
 * 每条都对着「把实现改坏，这条会不会红」写：
 * - 组件删掉 error prop / 不渲染 → 「显示后端原因」红；
 * - 没有错误时也渲染那一行（例如渲染成空 <p> 或占位文案）→ 「无错误时不渲染」红；
 * - 把错误只渲染在 preview 页脚里 → 「feedback 视图下也看得见」红
 *   （提交意见失败时视图停在 feedback，渲染在页脚就等于没说）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { getPlanDraftArtifacts } from '../api/client';
import { PlanApprovalDialog } from '../components/agent/PlanApprovalDialog';
import type { PermissionRequest } from '../store/SessionStore';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, getPlanDraftArtifacts: vi.fn(), setAutoApprove: vi.fn() };
});

const request: PermissionRequest = {
  rpc_id: 'r1',
  request_id: 'r1',
  command: '# 计划\n\n## 任务清单\n- [ ] 1. 做一件事',
  tool_name: 'plan_approval',
};

// 后端重建失败时回的那句话（plan_approval.rebuild_pending_plan_approval）
const BACKEND_MESSAGE = '计划没有通过格式校验，审批未落地（plan.md 可能在重启前后被改动过）。';

describe('PlanApprovalDialog 审批失败原因', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getPlanDraftArtifacts).mockResolvedValue({ success: false, message: 'none' });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('把后端报告的原因显示出来', () => {
    render(
      <PlanApprovalDialog
        request={request}
        sessionId="s1"
        error={BACKEND_MESSAGE}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    expect(screen.getByText(BACKEND_MESSAGE)).toBeTruthy();
  });

  it('没有错误时不渲染那一行', () => {
    render(
      <PlanApprovalDialog request={request} sessionId="s1" onApprove={vi.fn()} onDeny={vi.fn()} />,
    );
    expect(screen.queryByText(/审批未落地/)).toBeNull();
  });

  it('切到「修改意见」视图后仍然看得见（错误行不在 preview 页脚里）', () => {
    render(
      <PlanApprovalDialog
        request={request}
        sessionId="s1"
        error={BACKEND_MESSAGE}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /修改意见/ }));
    expect(screen.getByText(BACKEND_MESSAGE)).toBeTruthy();
  });
});

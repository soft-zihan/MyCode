/**
 * TaskListPanel（Plan 3b B2）：面板从只读状态显示器变成 plan 的执行界面。
 *
 * 每条测试都是对着「把实现改坏，这条会不会红」写的，钉住点在各自的注释里：
 * M7 摘要口径、焦点不由前端推导、PATCH 只带被改的字段、值没变不发请求、
 * 移动的 after_id 是 number 且边界 disabled、delete 的 200+success:false 不算成功、
 * 渐进披露（detail 只在展开态出现且有滚动上限）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react';
import { createTask, deleteTask, updateTask } from '../api/client';
import TaskListPanel from '../components/chat/TaskListPanel';
import type { TaskItem } from '../store/SessionStore';

vi.mock('../api/client', () => ({
  fetchTasks: vi.fn(),
  createTask: vi.fn(),
  updateTask: vi.fn(),
  deleteTask: vi.fn(),
}));

const VOCAB = ['pending', 'in_progress', 'completed', 'skipped', 'failed'];

const makeTask = (over: Partial<TaskItem> & { id: number }): TaskItem => ({
  content: `任务${over.id}`,
  status: 'pending',
  created_at: '2026-01-01T00:00:00+00:00',
  updated_at: '2026-01-02T00:00:00+00:00',
  detail: `详情${over.id}`,
  acceptance: `验收${over.id}`,
  error: '',
  ...over,
});

const rowOf = (container: HTMLElement, id: number): HTMLElement => {
  const el = container.querySelector(`[data-task-id="${id}"]`);
  if (!el) throw new Error(`测试找不到 task ${id} 对应的行`);
  return el as HTMLElement;
};

const summaryOf = (container: HTMLElement): string => {
  const el = container.querySelector('[data-task-summary]');
  if (!el) throw new Error('测试找不到摘要元素 [data-task-summary]');
  return el.textContent ?? '';
};

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

let onChanged: ReturnType<typeof vi.fn>;

const renderPanel = (tasks: TaskItem[], focusId: number | null = null) =>
  render(<TaskListPanel tasks={tasks} focusId={focusId} sessionId="s1" onChanged={onChanged} />);

describe('TaskListPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    onChanged = vi.fn();
    vi.mocked(updateTask).mockResolvedValue({} as never);
    vi.mocked(createTask).mockResolvedValue({} as never);
    vi.mocked(deleteTask).mockResolvedValue({ success: true, message: 'ok' });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('M7：failed / skipped 在摘要里可区分，未知状态不被当成永久已结束', () => {
    const tasks = [
      makeTask({ id: 1, status: 'completed' }),
      makeTask({ id: 2, status: 'failed', error: 'boom' }),
      makeTask({ id: 3, status: 'skipped' }),
      // 后端读端点的 status 刻意是宽松的 str，所以第六种状态运行时可达（R2 的代价面）
      makeTask({ id: 4, status: 'reviewing' as unknown as TaskItem['status'] }),
    ];
    const { container } = renderPanel(tasks);
    const summary = summaryOf(container);
    // 口径：分母 = 总数 − skipped（skipped 是显式决定不做），failed 留在分母里（缺口要看得见）
    expect(summary).toContain('1/3 完成');
    expect(summary).toContain('1 失败');
    expect(summary).toContain('1 跳过');
    expect(summary).toContain('1 未知状态');
    // 未知状态在下拉里如实显示，不被塞进 pending，也不被并进 skipped/failed
    const sel = within(rowOf(container, 4)).getByLabelText('状态') as HTMLSelectElement;
    expect(sel.value).toBe('reviewing');
    expect(Array.from(sel.options).map(o => o.value)).toEqual(['reviewing', ...VOCAB]);
    // failed 的 error 只在展开态出现
    expect(screen.queryByText(/boom/)).toBeNull();
  });

  it('焦点高亮跟 props.focusId 走，前端不自己推导', () => {
    const tasks = [
      makeTask({ id: 1, status: 'failed', error: 'e' }),
      makeTask({ id: 2, status: 'in_progress' }),
      makeTask({ id: 3, status: 'pending' }),
    ];
    const { container } = renderPanel(tasks, 1);
    // 前端若自己按「in_progress > failed > pending」推导，高亮会落到 2 号 → 这条红
    expect(rowOf(container, 1).getAttribute('data-focus')).toBe('true');
    expect(rowOf(container, 2).getAttribute('data-focus')).toBeNull();
    expect(rowOf(container, 3).getAttribute('data-focus')).toBeNull();
  });

  it('改 detail → PATCH 只带 detail，写成功后触发重新 GET', async () => {
    const tasks = [makeTask({ id: 1 }), makeTask({ id: 2, detail: '旧详情' })];
    const { container } = renderPanel(tasks, 2);
    const r2 = rowOf(container, 2);
    fireEvent.click(within(r2).getByLabelText('展开任务'));
    const box = within(r2).getByLabelText('详情') as HTMLTextAreaElement;
    expect(box.value).toBe('旧详情');
    fireEvent.change(box, { target: { value: '新详情' } });
    fireEvent.blur(box);
    await waitFor(() => expect(updateTask).toHaveBeenCalledTimes(1));
    // 精确对象匹配 = 钉住「只带 detail」：回传整条 task（含只读字段）会红
    expect(updateTask).toHaveBeenCalledWith('s1', 2, { detail: '新详情' });
    // 写端点不广播 task_list/updated，刷新必须由面板触发（R3）
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it('编辑后值没变 → 不发 PATCH（否则白清 detail_origin_seq、让模型重注入原样内容）', async () => {
    const tasks = [makeTask({ id: 1, detail: '同样的详情' })];
    const { container } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));
    const box = within(r).getByLabelText('详情') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: '同样的详情' } }); // 改成同一个值
    fireEvent.blur(box);
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true });
    await flush();
    expect(updateTask).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('Ctrl/Cmd+Enter 提交；纯 Enter 不提交（多行框里必须是换行）', async () => {
    const tasks = [makeTask({ id: 1, detail: '旧' })];
    const { container } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));
    const box = within(r).getByLabelText('详情') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: '旧\n新行' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(updateTask).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('s1', 1, { detail: '旧\n新行' }));
  });

  it('移动：第二条上移 → body 只有 after_id=number 0；首条上移与末条下移 disabled', async () => {
    const tasks = [makeTask({ id: 1 }), makeTask({ id: 2 }), makeTask({ id: 3 })];
    const { container } = renderPanel(tasks);
    // 边界上禁用按钮，而不是发一个后端自锚守卫会忽略的请求
    expect((within(rowOf(container, 1)).getByLabelText('上移') as HTMLButtonElement).disabled).toBe(true);
    expect((within(rowOf(container, 3)).getByLabelText('下移') as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(rowOf(container, 2)).getByLabelText('上移'));
    await waitFor(() => expect(updateTask).toHaveBeenCalledTimes(1));
    expect(updateTask).toHaveBeenCalledWith('s1', 2, { after_id: 0 });
    const body = vi.mocked(updateTask).mock.calls[0][2];
    expect(Object.keys(body)).toEqual(['after_id']); // 顺带带 detail 会清掉 detail_origin_seq
    expect(body.after_id).toBe(0);
    expect(typeof body.after_id).toBe('number'); // 字符串 "0" 会 422
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));

    // 下移 = 锚到后一条
    fireEvent.click(within(rowOf(container, 1)).getByLabelText('下移'));
    await waitFor(() => expect(updateTask).toHaveBeenLastCalledWith('s1', 1, { after_id: 2 }));
  });

  it('deleteTask 返回 200 + success:false 不算成功：记警告、不刷新', async () => {
    vi.mocked(deleteTask).mockResolvedValue({ success: false, message: 'task not found' });
    const tasks = [makeTask({ id: 1 }), makeTask({ id: 2 })];
    const { container } = renderPanel(tasks);
    const warn = vi.mocked(console.warn);

    fireEvent.click(within(rowOf(container, 1)).getByLabelText('删除'));
    await waitFor(() => expect(deleteTask).toHaveBeenCalledWith('s1', 1));
    await waitFor(() => expect(warn).toHaveBeenCalledTimes(1));
    expect(String(warn.mock.calls[0])).toContain('task not found');
    expect(onChanged).not.toHaveBeenCalled(); // 没删掉就不该让上层以为清单变了

    // 对照：success:true 才刷新（否则上面那条「不刷新」在坏实现下也可能碰巧通过）
    vi.mocked(deleteTask).mockResolvedValue({ success: true, message: 'ok' });
    fireEvent.click(within(rowOf(container, 2)).getByLabelText('删除'));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it('渐进披露：折叠看不到 detail，展开看到全文且有滚动上限', () => {
    const long = 'D'.repeat(6000); // 后端 DETAIL_DISCLOSURE_CHAR_LIMIT 量级
    const tasks = [makeTask({ id: 1, detail: long })];
    const { container } = renderPanel(tasks, 1);
    const r = rowOf(container, 1);
    expect(within(r).queryByLabelText('详情')).toBeNull();
    expect(screen.queryByText(long)).toBeNull();

    fireEvent.click(within(r).getByLabelText('展开任务'));
    const box = within(r).getByLabelText('详情') as HTMLTextAreaElement;
    expect(box.value).toBe(long); // 全文，不截断
    expect(box.className).toContain('max-h-48'); // 滚动上限：不能把面板撑爆
    expect(box.className).toContain('overflow-y-auto');
    expect(box.className).toContain('font-mono');

    fireEvent.click(within(r).getByLabelText('折叠任务'));
    expect(within(r).queryByLabelText('详情')).toBeNull();
  });

  it('插入：末尾追加省略 after_id，行内插入带 number after_id', async () => {
    const tasks = [makeTask({ id: 1 }), makeTask({ id: 2 })];
    const { container } = renderPanel(tasks);

    fireEvent.click(screen.getByLabelText('末尾追加任务'));
    fireEvent.change(screen.getByLabelText('新任务内容'), { target: { value: '追加的活' } });
    fireEvent.click(screen.getByLabelText('确认插入'));
    await waitFor(() =>
      expect(createTask).toHaveBeenCalledWith('s1', { content: '追加的活', detail: '', acceptance: '' }),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));

    fireEvent.click(within(rowOf(container, 1)).getByLabelText('在此条后插入'));
    fireEvent.change(screen.getByLabelText('新任务内容'), { target: { value: '插在1后' } });
    fireEvent.change(screen.getByLabelText('新任务验收'), { target: { value: 'AC' } });
    fireEvent.click(screen.getByLabelText('确认插入'));
    await waitFor(() =>
      expect(createTask).toHaveBeenLastCalledWith('s1', {
        content: '插在1后',
        detail: '',
        acceptance: 'AC',
        after_id: 1,
      }),
    );
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it('状态下拉：五值词表，PATCH 只带 status', async () => {
    const tasks = [makeTask({ id: 1 }), makeTask({ id: 2, status: 'pending' })];
    const { container } = renderPanel(tasks);
    const sel = within(rowOf(container, 2)).getByLabelText('状态') as HTMLSelectElement;
    expect(Array.from(sel.options).map(o => o.value)).toEqual(VOCAB);
    fireEvent.change(sel, { target: { value: 'in_progress' } });
    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('s1', 2, { status: 'in_progress' }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it('空清单不渲染（保留原行为）', () => {
    const { container } = renderPanel([]);
    expect(container.querySelector('[data-task-summary]')).toBeNull();
  });
});

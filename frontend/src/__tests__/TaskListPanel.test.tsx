/**
 * TaskListPanel（Plan 3b B2）：面板从只读状态显示器变成 plan 的执行界面。
 *
 * 每条测试都是对着「把实现改坏，这条会不会红」写的，钉住点在各自的注释里：
 * M7 摘要口径、焦点不由前端推导、PATCH 只带被改的字段、值没变不发请求、
 * 移动的 after_id 是 number 且边界 disabled、delete 的 200+success:false 不算成功、
 * 渐进披露（detail 只在展开态出现且有滚动上限）。
 * 修复轮补的：空白 content 不发 PATCH、写路径的 in-flight 守卫、过期草稿被丢弃、
 * 插入草稿不住在锚点行里、上移的通用分支、content/acceptance 的 body 形状、
 * 进度条的分母、写失败在面板上可见。
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

const panelEl = (tasks: TaskItem[], focusId: number | null = null) => (
  <TaskListPanel tasks={tasks} focusId={focusId} sessionId="s1" onChanged={onChanged} />
);

const renderPanel = (tasks: TaskItem[], focusId: number | null = null) =>
  render(panelEl(tasks, focusId));

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
    // F10：收尾断言挪进 waitFor——放在外面时对微任务顺序敏感，是 flake 源
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(2));
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

  // ─────────────── 修复轮：F1 / F2 / F3 / F4 / F5 / F7 ───────────────

  it('F1：清空 content 后失焦 → 不发 PATCH、丢草稿、记警告', async () => {
    const tasks = [makeTask({ id: 1, content: '原来的一行摘要' })];
    const { container } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));
    const box = within(r).getByLabelText('内容') as HTMLTextAreaElement;

    // 「全选内容 → 失焦」这个常见误操作此前会 PATCH {content:""} → 200，
    // 于是常驻摘要渲染成一行空任务、披露块标题变成 `## 当前任务的执行方案（#1 ）`
    fireEvent.change(box, { target: { value: '   ' } });
    fireEvent.blur(box);
    await flush();
    expect(updateTask).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
    expect(String(vi.mocked(console.warn).mock.calls)).toContain('空白 content');
    expect(box.value).toBe('原来的一行摘要'); // 草稿被丢掉，回到服务端的值

    // 对照：非空 content 照常提交，而且是 trim 过的（与 InsertForm 同一口径）
    fireEvent.change(box, { target: { value: '  改过的摘要  ' } });
    fireEvent.blur(box);
    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('s1', 1, { content: '改过的摘要' }));
  });

  it('F1：detail / acceptance 允许清空（清空验收条件是合法操作）', async () => {
    const tasks = [makeTask({ id: 1, acceptance: '旧验收', detail: '旧详情' })];
    const { container } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));

    const abox = within(r).getByLabelText('验收') as HTMLTextAreaElement;
    fireEvent.change(abox, { target: { value: '' } });
    fireEvent.blur(abox);
    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('s1', 1, { acceptance: '' }));

    const dbox = within(r).getByLabelText('详情') as HTMLTextAreaElement;
    fireEvent.change(dbox, { target: { value: '' } });
    fireEvent.blur(dbox);
    await waitFor(() => expect(updateTask).toHaveBeenLastCalledWith('s1', 1, { detail: '' }));
  });

  it('F2：写在途时的第二次提交被守卫挡住，同一个 PATCH 不会发两遍', async () => {
    // 永不 resolve 的写：把「在途」这个窗口拉成无限长
    vi.mocked(updateTask).mockReturnValue(new Promise(() => {}) as never);
    const tasks = [makeTask({ id: 1, detail: '旧详情' })];
    const { container } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));
    const box = within(r).getByLabelText('详情') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: '新详情' } });

    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true });
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true });
    fireEvent.blur(box);
    await flush();
    // 没有守卫的话这里是 3 次 → detail_origin_seq 被清 3 次 → 模型把同一段 detail
    // 重新注入 3 轮，正是本设计要防的那个成本
    expect(updateTask).toHaveBeenCalledTimes(1);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('F3：服务端改了同一个字段 → 过期草稿被丢弃，textarea 立刻显示新值', async () => {
    const tasks = [makeTask({ id: 1, detail: '用户开始编辑时的基线' })];
    const { container, rerender } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));
    const box = within(r).getByLabelText('详情') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: '用户正在打的更旧的草稿' } });
    expect(box.value).toBe('用户正在打的更旧的草稿');

    // Agent 通过工具改了同一条的 detail → WS task_list/updated → 上层重取 → 新 props
    rerender(panelEl([makeTask({ id: 1, detail: 'Agent 刚写的新值' })]));
    const box2 = within(rowOf(container, 1)).getByLabelText('详情') as HTMLTextAreaElement;
    expect(box2.value).toBe('Agent 刚写的新值');
    expect(String(vi.mocked(console.warn).mock.calls)).toContain('过期草稿');

    // 失焦不得把新值覆盖回更旧的草稿
    fireEvent.blur(box2);
    fireEvent.keyDown(box2, { key: 'Enter', ctrlKey: true });
    await flush();
    expect(updateTask).not.toHaveBeenCalled();
  });

  it('F3：编辑没被打断时草稿照常活着（base 一致就不丢）', async () => {
    const tasks = [makeTask({ id: 1, detail: '基线' })];
    const { container, rerender } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));
    fireEvent.change(within(r).getByLabelText('详情'), { target: { value: '打到一半' } });

    // 无关的 re-GET（别的条目变了）不得顺手丢掉用户正在打的字
    rerender(panelEl([makeTask({ id: 1, detail: '基线' }), makeTask({ id: 2, detail: '新来的' })]));
    const box = within(rowOf(container, 1)).getByLabelText('详情') as HTMLTextAreaElement;
    expect(box.value).toBe('打到一半');
    fireEvent.blur(box);
    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('s1', 1, { detail: '打到一半' }));
  });

  it('F4：锚点行消失 → 不崩、不静默发请求，插入草稿挪到末尾槽位还在', async () => {
    const tasks = [makeTask({ id: 1 }), makeTask({ id: 2 })];
    const { container, rerender } = renderPanel(tasks);
    fireEvent.click(within(rowOf(container, 2)).getByLabelText('在此条后插入'));
    fireEvent.change(screen.getByLabelText('新任务内容'), { target: { value: '新活' } });
    fireEvent.change(screen.getByLabelText('新任务详情'), { target: { value: '已经敲了一半的方案' } });

    // 锚点那条被 Agent 删掉了 → 重取回来的清单里没有它
    rerender(panelEl([makeTask({ id: 1 })]));
    expect(createTask).not.toHaveBeenCalled();
    // 表单此前渲染在锚点行内部，会跟着那行一起 unmount、把用户敲的 detail 无声丢掉
    expect((screen.getByLabelText('新任务详情') as HTMLTextAreaElement).value).toBe(
      '已经敲了一半的方案',
    );
    fireEvent.click(screen.getByLabelText('确认插入'));
    // 挪到末尾槽位之后就是「追加末尾」：省略 after_id，不会写一个已不存在的锚
    await waitFor(() =>
      expect(createTask).toHaveBeenCalledWith('s1', {
        content: '新活',
        detail: '已经敲了一半的方案',
        acceptance: '',
      }),
    );
  });

  it('F5：上移的通用分支——第 3 行上移锚到第 1 行（不是第 2 行）', async () => {
    const tasks = [makeTask({ id: 1 }), makeTask({ id: 2 }), makeTask({ id: 3 })];
    const { container } = renderPanel(tasks);
    fireEvent.click(within(rowOf(container, 3)).getByLabelText('上移'));
    await waitFor(() => expect(updateTask).toHaveBeenCalledTimes(1));
    // 写成 tasks[index-1].id 会得到 2 = 自锚 → 每次上移都静默变成 no-op
    expect(updateTask).toHaveBeenCalledWith('s1', 3, { after_id: 1 });
  });

  it('F5：content / acceptance 的提交 body 恰好只含自己那个字段，不含 detail', async () => {
    const tasks = [makeTask({ id: 1, content: '旧摘要', acceptance: '旧验收' })];
    const { container } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));

    const cbox = within(r).getByLabelText('内容') as HTMLTextAreaElement;
    fireEvent.change(cbox, { target: { value: '新摘要' } });
    fireEvent.blur(cbox);
    await waitFor(() => expect(updateTask).toHaveBeenCalledTimes(1));
    expect(updateTask).toHaveBeenCalledWith('s1', 1, { content: '新摘要' });
    // 把 content 编辑发成 {detail: next} 的话，这里会红——而且那会顺手清掉
    // detail_origin_seq，是后果最重的那个字段
    expect(Object.keys(vi.mocked(updateTask).mock.calls[0][2])).toEqual(['content']);

    const abox = within(r).getByLabelText('验收') as HTMLTextAreaElement;
    fireEvent.change(abox, { target: { value: '新验收' } });
    fireEvent.blur(abox);
    await waitFor(() => expect(updateTask).toHaveBeenCalledTimes(2));
    expect(updateTask).toHaveBeenLastCalledWith('s1', 1, { acceptance: '新验收' });
    expect(Object.keys(vi.mocked(updateTask).mock.calls[1][2])).toEqual(['acceptance']);
  });

  it('F5：进度条分母 = 总数 − skipped（不是 tasks.length）', () => {
    const tasks = [
      makeTask({ id: 1, status: 'completed' }),
      makeTask({ id: 2, status: 'completed' }),
      makeTask({ id: 3, status: 'skipped' }),
      makeTask({ id: 4, status: 'pending' }),
    ];
    const { container } = renderPanel(tasks);
    const bar = container.querySelector('[data-task-progress]');
    expect(bar).not.toBeNull();
    // 2/3 = 66.67%；用 completed / tasks.length 的实现会得到 50%
    expect(parseFloat((bar as HTMLElement).style.width)).toBeCloseTo(200 / 3, 3);
  });

  it('F7：写失败在面板头部可见（受控下拉弹回原值时不能看起来像「点了没反应」）', async () => {
    vi.mocked(updateTask).mockRejectedValue(
      Object.assign(new Error('Failed to update task: content cannot be blank'), { status: 422 }),
    );
    const tasks = [makeTask({ id: 1 })];
    const { container } = renderPanel(tasks);
    expect(container.querySelector('[data-task-error]')).toBeNull();

    fireEvent.change(within(rowOf(container, 1)).getByLabelText('状态'), {
      target: { value: 'in_progress' },
    });
    await waitFor(() => {
      const el = container.querySelector('[data-task-error]');
      expect(el).not.toBeNull();
      expect(el!.textContent).toContain('422'); // 状态码进文案，不只是 console
      expect(el!.textContent).toContain('content cannot be blank');
    });
    // 状态没有被乐观改掉：store 里还是上一次 GET 的真相
    expect((within(rowOf(container, 1)).getByLabelText('状态') as HTMLSelectElement).value).toBe(
      'pending',
    );
  });

  it('F7：delete 的 200 + success:false 也算失败，同样在面板上可见', async () => {
    vi.mocked(deleteTask).mockResolvedValue({ success: false, message: 'task not found' });
    const { container } = renderPanel([makeTask({ id: 1 })]);
    fireEvent.click(within(rowOf(container, 1)).getByLabelText('删除'));
    await waitFor(() => {
      const el = container.querySelector('[data-task-error]');
      expect(el).not.toBeNull();
      expect(el!.textContent).toContain('task not found');
    });
  });

  it('I-3：写成功但刷新失败 → 头部出现错误（不是只有写失败才报错）', async () => {
    // onChanged 返回 false = 上层的 fetchTasks 失败了。它刻意返回布尔值而不是抛：
    // 同一个函数还被 WS 事件处理器与挂载 effect 以 fire-and-forget 方式调用，抛出去
    // 会变成 unhandled rejection。未修时 runWrite 看到的是 resolved，于是 lastError
    // 不亮、草稿已丢、textarea 弹回编辑前的值——用户的编辑看起来凭空消失，而改动
    // 其实**已经在服务端了**。
    onChanged.mockResolvedValue(false);
    const tasks = [makeTask({ id: 1, detail: '旧详情' })];
    const { container } = renderPanel(tasks);
    const r = rowOf(container, 1);
    fireEvent.click(within(r).getByLabelText('展开任务'));
    const box = within(r).getByLabelText('详情') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: '新详情' } });
    fireEvent.blur(box);

    await waitFor(() => expect(updateTask).toHaveBeenCalledTimes(1));
    await waitFor(() => {
      const el = container.querySelector('[data-task-error]');
      expect(el).not.toBeNull();
      expect(el!.textContent).toContain('刷新');
      // 写其实成功了：文案不得说成「写入失败」，否则用户会把同一次编辑再提交一遍
      expect(el!.textContent).not.toContain('写入失败');
    });

    // 对照：刷新成功（返回 true）时不亮错误。没有这半边，「任何写之后都亮错误」的
    // 坏实现也会绿。
    onChanged.mockResolvedValue(true);
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    await flush();
    const box2 = within(rowOf(container, 1)).getByLabelText('详情') as HTMLTextAreaElement;
    fireEvent.change(box2, { target: { value: '再改一次' } });
    fireEvent.blur(box2);
    await waitFor(() => expect(updateTask).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(2));
    await flush();
    expect(container.querySelector('[data-task-error]')).toBeNull();
  });
});

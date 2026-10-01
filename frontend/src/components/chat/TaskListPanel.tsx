import { memo, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, ReactNode } from 'react';
import {
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Clock,
  ListTodo,
  Loader2,
  Plus,
  Trash2,
  XCircle,
} from 'lucide-react';
import { createTask, deleteTask, updateTask } from '../../api/client';
import type { TaskStatus, TaskUpdateInput } from '../../api/client';
import type { TaskItem } from '../../store/SessionStore';

interface TaskListPanelProps {
  tasks: TaskItem[];
  /** 后端 `find_focus` 算出的焦点条 id。前端**不**自己推导（R3）：焦点规则
   *  `in_progress > failed > pending`、同档取列表顺序第一个，前端另推一份一定会漂移。 */
  focusId?: number | null;
  sessionId?: string;
  /** 写成功之后重新 GET 一次。写端点刻意不广播 `task_list/updated`（那条 WS 分支只服务
   *  Agent 侧的工具调用），HTTP 写的发起方就是面板自己，所以刷新得由面板触发。 */
  onChanged?: () => void | Promise<void>;
}

/** 后端五值状态词表（与 `TaskStatus` 同源）。 */
const STATUSES: TaskStatus[] = ['pending', 'in_progress', 'completed', 'skipped', 'failed'];

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: '待办',
  in_progress: '进行中',
  completed: '完成',
  skipped: '跳过',
  failed: '失败',
};

/** detail 可以到 6000 字符（后端 DETAIL_DISCLOSURE_CHAR_LIMIT），必须有滚动上限，
 *  否则展开一条长 detail 就把整个面板撑爆、输入框顶出视口。 */
const DETAIL_BOX_CLASS = 'max-h-48 overflow-y-auto';

type EditableField = 'content' | 'detail' | 'acceptance';

const FIELD_LABEL: Record<EditableField, string> = {
  content: '内容',
  detail: '详情',
  acceptance: '验收',
};

const draftKey = (id: number, field: EditableField) => `${id}\u0000${field}`;

const getStatusIcon = (status: string) => {
  switch (status) {
    case 'completed':
      return <CheckCircle2 className="w-4 h-4 text-green-500 shrink-0" />;
    case 'in_progress':
      return <Clock className="w-4 h-4 text-indigo-500 animate-pulse shrink-0" />;
    case 'skipped':
      return <XCircle className="w-4 h-4 text-gray-400 shrink-0" />;
    case 'failed':
      return <XCircle className="w-4 h-4 text-red-500 shrink-0" />;
    default:
      // 未知状态（后端将来加第六种）也走这里：画个空圈，不假装它是 pending
      return <Circle className="w-4 h-4 text-gray-300 shrink-0" />;
  }
};

const getStatusColor = (status: string) => {
  switch (status) {
    case 'completed':
      return 'text-gray-400 line-through';
    case 'skipped':
      return 'text-gray-400 line-through';
    case 'failed':
      return 'text-red-700 font-medium';
    case 'in_progress':
      return 'text-indigo-700 font-medium';
    default:
      return 'text-gray-700';
  }
};

/** 行内的小图标按钮：边界上 disabled，而不是发一个后端一定会忽略的请求。 */
const RowButton = ({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) => (
  <button
    type="button"
    aria-label={label}
    title={label}
    onClick={onClick}
    disabled={disabled}
    className="p-0.5 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100 disabled:opacity-30 disabled:hover:bg-transparent disabled:cursor-not-allowed"
  >
    {children}
  </button>
);

/** 插入表单：自己管三个输入框的本地状态，成功之后由父组件关掉。 */
const InsertForm = ({
  busy,
  onCancel,
  onSubmit,
}: {
  busy: boolean;
  onCancel: () => void;
  onSubmit: (content: string, detail: string, acceptance: string) => Promise<void>;
}) => {
  const [content, setContent] = useState('');
  const [detail, setDetail] = useState('');
  const [acceptance, setAcceptance] = useState('');

  const submit = () => {
    if (busy || !content.trim()) return;
    void onSubmit(content.trim(), detail, acceptance);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Ctrl/Cmd+Enter 提交；纯 Enter 在多行框里必须是换行
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onCancel();
    }
  };

  const boxClass =
    'w-full text-xs border border-gray-200 rounded px-2 py-1 bg-white focus:outline-none focus:ring-1 focus:ring-indigo-300';

  return (
    <div className="mt-2 space-y-1.5 pl-5 pr-1">
      <textarea
        aria-label="新任务内容"
        rows={1}
        value={content}
        disabled={busy}
        placeholder="新任务（一句话摘要）"
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => setContent(e.target.value)}
        onKeyDown={onKeyDown}
        className={boxClass}
      />
      <textarea
        aria-label="新任务验收"
        rows={1}
        value={acceptance}
        disabled={busy}
        placeholder="验收标准（可选）"
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => setAcceptance(e.target.value)}
        onKeyDown={onKeyDown}
        className={boxClass}
      />
      <textarea
        aria-label="新任务详情"
        rows={3}
        value={detail}
        disabled={busy}
        placeholder="详细方案（可选，展开态才看得到）"
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => setDetail(e.target.value)}
        onKeyDown={onKeyDown}
        className={`${boxClass} font-mono whitespace-pre-wrap ${DETAIL_BOX_CLASS}`}
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-label="确认插入"
          onClick={submit}
          disabled={busy || !content.trim()}
          className="text-xs px-2 py-0.5 rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          插入
        </button>
        <button
          type="button"
          aria-label="取消插入"
          onClick={onCancel}
          disabled={busy}
          className="text-xs px-2 py-0.5 rounded text-gray-500 hover:bg-gray-100 disabled:opacity-40"
        >
          取消
        </button>
        <span className="text-[11px] text-gray-400">Ctrl/Cmd+Enter 提交 · Esc 取消</span>
      </div>
    </div>
  );
};

/**
 * 任务清单面板 = plan 的执行界面（Plan 3b B2）。
 *
 * 这个特性把「plan 模式」的执行状态机整个删掉了，task_list 现在就是 plan，
 * 所以面板不再是只读状态显示器，而是用户干预计划的唯一入口：展开看 detail、
 * 内联改 content/detail/acceptance、切状态、插入、删除、上下移动。
 *
 * 一律**不做乐观更新**（照 BackgroundTasksPanel 的房子风格）：写成功之后调 `onChanged()`
 * 让上层重新 GET，面板与 store 都以后端为准。理由是焦点条只能由后端 `find_focus` 算，
 * 前端本地拼一份一定会与后端漂移。失败只 `console.error('[TASK]', ...)`、不动 store。
 */
export const TaskListPanel = memo(function TaskListPanel({
  tasks,
  focusId,
  sessionId,
  onChanged,
}: TaskListPanelProps) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  /** 编辑中的草稿，key = `${id}\0${field}`；不在草稿里的字段显示 store 里的值。
   *  值类型带 `undefined` 是为了让「没编辑过」与「编辑成空串」可区分。 */
  const [drafts, setDrafts] = useState<Record<string, string | undefined>>({});
  /** in-flight 的写操作，key = `task-${id}`（行内四种写）或 `insert-${slot}`。 */
  const [busy, setBusy] = useState<Set<string>>(new Set());
  /** 打开着的插入表单：afterId=null 表示追加到末尾（POST 省略 after_id）。 */
  const [insertSlot, setInsertSlot] = useState<{ afterId: number | null } | null>(null);

  // 空清单不占地方（保留原行为）。代价是「插入第一条」没有入口——第一条由模型或 plan 物化产生。
  if (tasks.length === 0) return null;

  const canWrite = Boolean(sessionId);

  const runWrite = async (opKey: string, fn: () => Promise<void>) => {
    setBusy(prev => new Set(prev).add(opKey));
    try {
      await fn();
    } catch (err) {
      // 失败不乐观回滚也不清空：store 里还是上一次 GET 的真相，日志是唯一出口
      console.error('[TASK] write failed:', opKey, err);
    } finally {
      setBusy(prev => {
        const next = new Set(prev);
        next.delete(opKey);
        return next;
      });
    }
  };

  const toggle = (id: number) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const discardDraft = (id: number, field: EditableField) => {
    const key = draftKey(id, field);
    setDrafts(prev => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  /** 失焦或 Ctrl/Cmd+Enter 提交单个字段的 PATCH。 */
  const commitField = (task: TaskItem, field: EditableField) => {
    // 局部 const：`sessionId` 是解构出来的参数（可变绑定），在闭包里 TS 不保留收窄
    const sid = sessionId;
    if (!sid) return;
    const key = draftKey(task.id, field);
    const next = drafts[key];
    if (next === undefined) return; // 没编辑过，什么都不发
    if (next === task[field]) {
      // 值没变就**不发请求**：白改一次 detail 会清掉 detail_origin_seq，
      // 让模型下一轮把一模一样的 detail 重新注入一遍。
      discardDraft(task.id, field);
      return;
    }
    // 只带被改的那一个字段。整条 task 回传会把只读字段（detail_origin_seq / started_seq /
    // created_at…）一起发过去，而且顺带改 detail 会白白触发一次重新披露。
    const payload: TaskUpdateInput =
      field === 'content' ? { content: next } : field === 'acceptance' ? { acceptance: next } : { detail: next };
    void runWrite(`task-${task.id}`, async () => {
      await updateTask(sid, task.id, payload);
      discardDraft(task.id, field);
      await onChanged?.();
    });
  };

  const handleStatus = (task: TaskItem, next: string) => {
    const sid = sessionId;
    if (!sid || next === task.status) return;
    void runWrite(`task-${task.id}`, async () => {
      await updateTask(sid, task.id, { status: next as TaskStatus });
      await onChanged?.();
    });
  };

  /**
   * 上下移动 = PATCH **只**带 `after_id`（带别的字段会顺带改数据，尤其 detail 会清掉
   * detail_origin_seq、让模型下一轮重注入一遍原样内容）。
   * `after_id` 必须是 number：后端声明成 int，字符串 "3" 会 422。
   * 上移 = 锚到「前一条的前一条」；本条是第二条时那个锚不存在，用 0（后端约定 0 = 插到最前，
   * task_store._insert_after）。下移 = 锚到「后一条」。
   */
  const move = (index: number, dir: -1 | 1) => {
    const sid = sessionId;
    if (!sid) return;
    const task = tasks[index];
    if (!task) return;
    let afterId: number;
    if (dir === -1) {
      if (index === 0) return;
      afterId = index === 1 ? 0 : tasks[index - 2].id;
    } else {
      if (index >= tasks.length - 1) return;
      afterId = tasks[index + 1].id;
    }
    void runWrite(`task-${task.id}`, async () => {
      await updateTask(sid, task.id, { after_id: afterId });
      await onChanged?.();
    });
  };

  const remove = (task: TaskItem) => {
    const sid = sessionId;
    if (!sid) return;
    void runWrite(`task-${task.id}`, async () => {
      const res = await deleteTask(sid, task.id);
      if (!res.success) {
        // 未知 id 是 **200 + success:false**，不是 HTTP 错误：只看有没有抛就会把失败当成功。
        console.warn('[TASK] delete rejected:', res.message);
        return;
      }
      await onChanged?.();
    });
  };

  const submitInsert = async (afterId: number | null, content: string, detail: string, acceptance: string) => {
    const sid = sessionId;
    if (!sid) return;
    await runWrite(`insert-${afterId ?? 'end'}`, async () => {
      // TaskCreateInput 的 detail/acceptance 在生成类型里是必填（后端有默认值），显式给空串。
      await createTask(
        sid,
        afterId === null ? { content, detail, acceptance } : { content, detail, acceptance, after_id: afterId },
      );
      setInsertSlot(null);
      await onChanged?.();
    });
  };

  const renderField = (task: TaskItem, field: EditableField, mono = false, rows = 2) => {
    const key = draftKey(task.id, field);
    const value = drafts[key] ?? task[field];
    const rowBusy = busy.has(`task-${task.id}`);
    return (
      <div key={field}>
        <div className="text-[11px] text-gray-400 mb-0.5">{FIELD_LABEL[field]}</div>
        <textarea
          aria-label={FIELD_LABEL[field]}
          rows={rows}
          value={value}
          disabled={!canWrite || rowBusy}
          onChange={(e: ChangeEvent<HTMLTextAreaElement>) =>
            setDrafts(prev => ({ ...prev, [key]: e.target.value }))
          }
          onBlur={() => commitField(task, field)}
          onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
            // 纯 Enter 在 detail 这种多行框里必须是换行，不能提交
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              commitField(task, field);
            } else if (e.key === 'Escape') {
              e.preventDefault();
              discardDraft(task.id, field);
            }
          }}
          className={`w-full text-xs border border-gray-200 rounded px-2 py-1 bg-white focus:outline-none focus:ring-1 focus:ring-indigo-300 disabled:bg-gray-50 disabled:text-gray-500 ${
            mono ? `font-mono whitespace-pre-wrap ${DETAIL_BOX_CLASS}` : ''
          }`}
        />
      </div>
    );
  };

  // ── M7 计数：failed / skipped 必须与 pending 区分开 ──────────────────────────
  let completed = 0;
  let failed = 0;
  let skipped = 0;
  let inProgress = 0;
  let pending = 0;
  let unknown = 0;
  for (const t of tasks) {
    if (t.status === 'completed') completed++;
    else if (t.status === 'failed') failed++;
    else if (t.status === 'skipped') skipped++;
    else if (t.status === 'in_progress') inProgress++;
    else if (t.status === 'pending') pending++;
    else unknown++;
  }
  // 进度条口径：分母 = 总数 − skipped。
  // 为什么把 skipped 踢出分母：skipped 是「显式决定不做」，留在分母里进度条永远到不了 100%，
  // 用户会以为还有活没干；它在摘要里单独计数，所以信息没丢。
  // failed 刻意**留在**分母里：它确实是没做完的活，缺口必须看得见（M7 的原问题就是 failed
  // 在汇总里与 pending 长得一样，而 failed 现在是可达状态）。
  // 未知状态（后端将来加第六种）同样留在分母 = 保守地算「未完成」，但摘要里单独标出条数，
  // 于是它不会被静默当成「永久已结束」——这正是 R2 把 status 放宽成 str 的代价面。
  const denominator = Math.max(tasks.length - skipped, 0);
  const unfinished = pending + inProgress + failed + unknown;

  return (
    <div className="mx-4 mb-2">
      <div className="rounded-lg border border-gray-200 bg-white shadow-sm overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-2 bg-gray-50 border-b border-gray-200">
          <ListTodo className="w-4 h-4 text-gray-500 shrink-0" />
          <span className="text-sm font-medium text-gray-700">任务清单</span>
          <span className="text-xs text-gray-500 ml-auto" data-task-summary>
            {denominator > 0 ? `${completed}/${denominator} 完成` : `${completed} 完成`}
            {inProgress > 0 && ` · ${inProgress} 进行中`}
            {failed > 0 && ` · ${failed} 失败`}
            {skipped > 0 && ` · ${skipped} 跳过`}
            {unknown > 0 && ` · ${unknown} 未知状态`}
          </span>
          <RowButton
            label="末尾追加任务"
            disabled={!canWrite}
            onClick={() =>
              setInsertSlot(prev => (prev && prev.afterId === null ? null : { afterId: null }))
            }
          >
            <Plus className="w-3.5 h-3.5" />
          </RowButton>
        </div>

        <div className="divide-y divide-gray-100">
          {tasks.map((task, index) => {
            const isOpen = expanded.has(task.id);
            const isFocus = focusId != null && focusId === task.id;
            const rowBusy = busy.has(`task-${task.id}`);
            const knownStatus = (STATUSES as string[]).includes(task.status);
            return (
              <div key={task.id} data-task-id={task.id} data-focus={isFocus ? 'true' : undefined}>
                <div
                  className={`flex items-start gap-2 px-3 py-2 hover:bg-gray-50 ${
                    isFocus ? 'bg-indigo-50/70 border-l-2 border-indigo-400' : 'border-l-2 border-transparent'
                  }`}
                >
                  <RowButton label={isOpen ? '折叠任务' : '展开任务'} onClick={() => toggle(task.id)}>
                    {isOpen ? (
                      <ChevronDown className="w-3.5 h-3.5" />
                    ) : (
                      <ChevronRight className="w-3.5 h-3.5" />
                    )}
                  </RowButton>
                  <div className="flex-1 min-w-0 cursor-pointer" onClick={() => toggle(task.id)}>
                    <div className="flex items-start gap-1.5">
                      {getStatusIcon(task.status)}
                      <p
                        className={`text-sm flex-1 truncate ${getStatusColor(task.status)}`}
                        title={task.content}
                      >
                        {task.content}
                      </p>
                    </div>
                    {task.acceptance && (
                      <p className="text-xs text-gray-400 truncate pl-[22px]" title={task.acceptance}>
                        {task.acceptance}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <select
                      aria-label="状态"
                      value={task.status}
                      disabled={!canWrite || rowBusy}
                      onChange={e => handleStatus(task, e.target.value)}
                      className="text-xs border border-gray-200 rounded px-1 py-0.5 bg-white text-gray-600 disabled:bg-gray-50"
                      title="改状态（下一轮模型即可见）"
                    >
                      {/* 后端 status 是宽松的 str：收到词表外的值时如实显示，不假装它是 pending */}
                      {!knownStatus && <option value={task.status}>未知：{task.status}</option>}
                      {STATUSES.map(s => (
                        <option key={s} value={s}>
                          {STATUS_LABEL[s]}
                        </option>
                      ))}
                    </select>
                    <RowButton
                      label="上移"
                      disabled={!canWrite || rowBusy || index === 0}
                      onClick={() => move(index, -1)}
                    >
                      <ArrowUp className="w-3.5 h-3.5" />
                    </RowButton>
                    <RowButton
                      label="下移"
                      disabled={!canWrite || rowBusy || index === tasks.length - 1}
                      onClick={() => move(index, 1)}
                    >
                      <ArrowDown className="w-3.5 h-3.5" />
                    </RowButton>
                    <RowButton
                      label="在此条后插入"
                      disabled={!canWrite}
                      onClick={() =>
                        setInsertSlot(prev => (prev && prev.afterId === task.id ? null : { afterId: task.id }))
                      }
                    >
                      <Plus className="w-3.5 h-3.5" />
                    </RowButton>
                    <RowButton
                      label="删除"
                      disabled={!canWrite || rowBusy}
                      onClick={() => remove(task)}
                    >
                      {rowBusy ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <Trash2 className="w-3.5 h-3.5" />
                      )}
                    </RowButton>
                  </div>
                </div>

                {isOpen && (
                  <div className="px-3 pb-2 pt-1 space-y-2 bg-gray-50/40">
                    {renderField(task, 'content', false, 2)}
                    {renderField(task, 'acceptance', false, 1)}
                    {renderField(task, 'detail', true, 6)}
                    {task.status === 'failed' && task.error && (
                      <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1 whitespace-pre-wrap max-h-32 overflow-y-auto">
                        <span className="font-medium">错误：</span>
                        {task.error}
                      </div>
                    )}
                    <div className="text-[11px] text-gray-400 font-mono">
                      创建 {task.created_at} · 更新 {task.updated_at}
                    </div>
                  </div>
                )}

                {insertSlot && insertSlot.afterId === task.id && (
                  <div className="px-3 pb-2">
                    <InsertForm
                      busy={busy.has(`insert-${task.id}`)}
                      onCancel={() => setInsertSlot(null)}
                      onSubmit={(content, detail, acceptance) =>
                        submitInsert(task.id, content, detail, acceptance)
                      }
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {insertSlot && insertSlot.afterId === null && (
          <div className="px-3 py-2 border-t border-gray-100">
            <InsertForm
              busy={busy.has('insert-end')}
              onCancel={() => setInsertSlot(null)}
              onSubmit={(content, detail, acceptance) => submitInsert(null, content, detail, acceptance)}
            />
          </div>
        )}

        {denominator > 0 && unfinished > 0 && (
          <div className="px-3 py-1.5 bg-gray-50 border-t border-gray-100">
            <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden">
              <div
                className="h-full bg-green-500 transition-all duration-300"
                style={{ width: `${Math.min((completed / denominator) * 100, 100)}%` }}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
});

export default TaskListPanel;

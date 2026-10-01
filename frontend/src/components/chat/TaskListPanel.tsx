import { memo, useEffect, useState } from 'react';
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
import type { TaskApiError, TaskStatus, TaskUpdateInput } from '../../api/client';
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

/** 整个清单的上限（F8）。detail 自己的 max-h-48 只保住「单条长 detail」，保不住
 *  「同时展开多条」：面板是 ChatPage 那个 `min-h-0` ChatView 的 flex 兄弟，没有上限时
 *  展开几行就把对话记录挤扁、甚至把输入框顶出视口。
 *  取 24rem ≈ 一条**完全展开**的行的高度（detail 上限 12rem + content/acceptance 两个
 *  小框 + 行头 + 时间戳）：于是展开单条不会触发外层滚动，从第二条展开起才开始滚。 */
const LIST_BOX_CLASS = 'max-h-96 overflow-y-auto';

/** 时间戳显示（F9）：复用本仓库既有的内联口径（`nodes/AssistantNodeView.tsx` 与
 *  `UserNodeView.tsx` 都是 `new Date(x).toLocaleTimeString('zh-CN', ...)`），
 *  前端没有共享的时间格式化模块，也不为这一处新引入依赖或新写一个模块。
 *  解析不出来就把原始串如实显示——显示 "Invalid Date" 比显示原始 ISO 更难读。 */
const formatStamp = (iso: string) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
};

type EditableField = 'content' | 'detail' | 'acceptance';

const FIELD_LABEL: Record<EditableField, string> = {
  content: '内容',
  detail: '详情',
  acceptance: '验收',
};

const draftKey = (id: number, field: EditableField) => `${id}\u0000${field}`;

/** 插入表单的三个框的值（F4）。 */
type InsertDraft = { content: string; detail: string; acceptance: string };

const EMPTY_INSERT: InsertDraft = { content: '', detail: '', acceptance: '' };

/** 插入槽位 key：`after-<id>` = 插在某条之后，`after-end` = 追加末尾。
 *  同一个串既做 insertDrafts 的 key 又做 busy 的 key（前缀与行内写的 `task-` 不撞）。 */
const SLOT_END = 'after-end';
const slotKey = (afterId: number | null) => (afterId === null ? SLOT_END : `after-${afterId}`);
const slotAfterId = (slot: string): number | null =>
  slot === SLOT_END ? null : Number(slot.slice('after-'.length));

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

/** 插入表单：受控组件。三个框的值住在**面板** state（按槽位 key）里而不是它自己的
 *  useState 里（F4）——表单渲染在它所锚定的那一行内部，那条任务被删/被重取掉时表单会
 *  连同用户已经敲进去的 detail 一起 unmount，无声无息。值提上去之后锚点行消失也能保住。 */
const InsertForm = ({
  busy,
  value,
  onChange,
  onCancel,
  onSubmit,
}: {
  busy: boolean;
  value: InsertDraft;
  onChange: (patch: Partial<InsertDraft>) => void;
  onCancel: () => void;
  onSubmit: () => void;
}) => {
  const submit = () => {
    if (busy || !value.content.trim()) return;
    onSubmit();
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
        value={value.content}
        disabled={busy}
        placeholder="新任务（一句话摘要）"
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => onChange({ content: e.target.value })}
        onKeyDown={onKeyDown}
        className={boxClass}
      />
      <textarea
        aria-label="新任务验收"
        rows={1}
        value={value.acceptance}
        disabled={busy}
        placeholder="验收标准（可选）"
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => onChange({ acceptance: e.target.value })}
        onKeyDown={onKeyDown}
        className={boxClass}
      />
      <textarea
        aria-label="新任务详情"
        rows={3}
        value={value.detail}
        disabled={busy}
        placeholder="详细方案（可选，展开态才看得到）"
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => onChange({ detail: e.target.value })}
        onKeyDown={onKeyDown}
        className={`${boxClass} font-mono whitespace-pre-wrap ${DETAIL_BOX_CLASS}`}
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-label="确认插入"
          onClick={submit}
          disabled={busy || !value.content.trim()}
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
 * 前端本地拼一份一定会与后端漂移。失败不动 store，但**必须可见**：面板头部渲染一行
 * `lastError`（F7），console.error 只是附带。
 */
export const TaskListPanel = memo(function TaskListPanel({
  tasks,
  focusId,
  sessionId,
  onChanged,
}: TaskListPanelProps) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  /** 编辑中的草稿，key = `${id}\0${field}`，值 = `{ value, base }`。
   *  `base` 是打开编辑时的服务端值，用来在 re-GET 之后识别「这条草稿已过期」（F3）。 */
  const [drafts, setDrafts] = useState<Record<string, { value: string; base: string }>>({});
  /** in-flight 的写操作，key = `task-${id}`（行内四种写）或插入槽位 key。
   *  写路径顶部都加了守卫，同 key 不可能并发，所以 Set 就够，不需要计数器（F2）。 */
  const [busy, setBusy] = useState<Set<string>>(new Set());
  /** 打开着的插入表单的槽位 key（null = 没开着）。`after-end` = 追加末尾。 */
  const [insertSlot, setInsertSlot] = useState<string | null>(null);
  /** 按槽位存的插入草稿（F4）：值不住在表单组件里，锚点行消失也不丢。 */
  const [insertDrafts, setInsertDrafts] = useState<Record<string, InsertDraft>>({});
  /** 最近一次写失败的消息（F7）。状态下拉是受控组件、绑定 `task.status`，一次被拒的
   *  切换看起来就是「点了没反应」，所以失败必须在面板上可见，不能只进 console.error。 */
  const [lastError, setLastError] = useState<string | null>(null);

  /** 草稿过期检测 + 孤儿清理（F3 / Minor 8），跟着 `tasks` 走。
   *  草稿按 id 存、且在 re-GET 之后仍然活着：若用户正在编辑 `detail` 时 Agent 通过工具
   *  改了同一条的 `detail`（WS `task_list/updated` → 上层重取），textarea 里还是用户
   *  **更旧**的草稿，失焦就会把新值覆盖掉且毫无提示。裁定：任何 `base !== task[field]`
   *  的草稿**直接丢弃**并 warn，于是 textarea 立刻显示服务端的新值。刻意不做冲突合并 UI
   *  （那是新 affordance），也不做「仍然写过去」（那正是这条缺陷本身）。
   *  同一个 effect 顺手清掉 `tasks` 里已不存在的 id 的草稿与展开状态。 */
  useEffect(() => {
    const byId = new Map(tasks.map(t => [t.id, t]));
    setDrafts(prev => {
      let changed = false;
      const next: Record<string, { value: string; base: string }> = {};
      for (const [key, draft] of Object.entries(prev)) {
        const sep = key.indexOf('\u0000');
        const id = Number(key.slice(0, sep));
        const field = key.slice(sep + 1) as EditableField;
        const task = byId.get(id);
        if (task === undefined) {
          console.warn(`[TASK] 丢弃草稿：任务 #${id} 已不在清单里（${field}）`);
          changed = true;
          continue;
        }
        if (draft.base !== task[field]) {
          console.warn(`[TASK] 丢弃过期草稿：#${id} 的 ${field} 服务端已经变了，显示新值`);
          changed = true;
          continue;
        }
        next[key] = draft;
      }
      return changed ? next : prev;
    });
    setExpanded(prev => {
      let changed = false;
      const next = new Set<number>();
      for (const id of prev) {
        if (byId.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [tasks]);

  /** 插入表单的锚点行消失（F4）：草稿已经提到面板 state，于是把它挪到末尾槽位继续
   *  可见（POST 会追加到末尾），而不是跟着锚点行一起 unmount、无声丢掉用户敲进去的内容。 */
  useEffect(() => {
    if (insertSlot === null || insertSlot === SLOT_END) return;
    const afterId = slotAfterId(insertSlot);
    if (afterId !== null && tasks.some(t => t.id === afterId)) return;
    const orphan = insertSlot;
    console.warn(`[TASK] 插入表单的锚点 #${afterId} 已不在清单里，草稿挪到末尾追加`);
    setInsertDrafts(prev => {
      const moved = prev[orphan];
      if (moved === undefined) return prev;
      const next = { ...prev };
      delete next[orphan];
      next[SLOT_END] = { ...EMPTY_INSERT, ...next[SLOT_END], ...moved };
      return next;
    });
    setInsertSlot(SLOT_END);
  }, [tasks, insertSlot]);

  // 空清单不占地方（保留原行为）。代价是「插入第一条」没有入口——第一条由模型或 plan 物化产生。
  if (tasks.length === 0) return null;

  const canWrite = Boolean(sessionId);

  const runWrite = async (opKey: string, fn: () => Promise<void>) => {
    setBusy(prev => new Set(prev).add(opKey));
    // 错误在**新写开始时**清掉，而不是在成功后清：delete 的「200 + success:false」那条
    // 失败不抛异常（HTTP 是成功的），成功后清会把它当场抹掉。
    setLastError(null);
    try {
      await fn();
    } catch (err) {
      // 失败不乐观回滚也不清空 store：store 里还是上一次 GET 的真相。但必须**可见**（F7）。
      const e = err as TaskApiError;
      const code = e?.status != null ? `HTTP ${e.status}` : '网络错误';
      setLastError(`写入失败（${code}）：${e?.message ?? String(err)}`);
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

  /** 关掉某个槽位的插入表单并丢掉它的草稿（取消/提交成功都是用户主动的收尾）。 */
  const closeInsert = (slot: string) => {
    setInsertSlot(prev => (prev === slot ? null : prev));
    setInsertDrafts(prev => {
      if (!(slot in prev)) return prev;
      const next = { ...prev };
      delete next[slot];
      return next;
    });
  };

  /** 失焦或 Ctrl/Cmd+Enter 提交单个字段的 PATCH。 */
  const commitField = (task: TaskItem, field: EditableField) => {
    // 局部 const：`sessionId` 是解构出来的参数（可变绑定），在闭包里 TS 不保留收窄
    const sid = sessionId;
    if (!sid) return;
    const opKey = `task-${task.id}`;
    // in-flight 守卫（F2）：草稿只在 `await updateTask` **之后**才丢，所以在途期间的
    // 第二次提交会把同一个 PATCH 再发一遍 → `detail_origin_seq` 被清两次 → 正是本设计
    // 要防的重复注入。按钮/文本框的 disabled 只在渲染后才生效，挡不住这个窗口。
    if (busy.has(opKey)) return;
    const key = draftKey(task.id, field);
    const draft = drafts[key];
    if (draft === undefined) return; // 没编辑过，什么都不发
    // content 提交前 trim（InsertForm 一直 trim，面板内部此前不一致）
    const next = field === 'content' ? draft.value.trim() : draft.value;
    if (next === task[field]) {
      // 值没变就**不发请求**：白改一次 detail 会清掉 detail_origin_seq，
      // 让模型下一轮把一模一样的 detail 重新注入一遍。
      discardDraft(task.id, field);
      return;
    }
    if (field === 'content' && next === '') {
      // 空白 content **不发请求**（F1）：后端工具层与 PATCH 端点都会拒（422 / Error），
      // 而「全选 → 失焦」是常见误操作，发一个注定被拒的写只是把错误丢回给用户。
      // detail / acceptance 允许为空（清空验收条件是合法操作），所以只挡 content。
      console.warn(`[TASK] 丢弃空白 content 草稿：#${task.id}（一行摘要不能为空）`);
      discardDraft(task.id, field);
      return;
    }
    // 只带被改的那一个字段。整条 task 回传会把只读字段（detail_origin_seq / started_seq /
    // created_at…）一起发过去，而且顺带改 detail 会白白触发一次重新披露。
    const payload: TaskUpdateInput =
      field === 'content' ? { content: next } : field === 'acceptance' ? { acceptance: next } : { detail: next };
    void runWrite(opKey, async () => {
      await updateTask(sid, task.id, payload);
      discardDraft(task.id, field);
      await onChanged?.();
    });
  };

  const handleStatus = (task: TaskItem, next: string) => {
    const sid = sessionId;
    if (!sid || next === task.status) return;
    const opKey = `task-${task.id}`;
    if (busy.has(opKey)) return; // F2
    void runWrite(opKey, async () => {
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
    const opKey = `task-${task.id}`;
    if (busy.has(opKey)) return; // F2
    let afterId: number;
    if (dir === -1) {
      if (index === 0) return;
      afterId = index === 1 ? 0 : tasks[index - 2].id;
    } else {
      if (index >= tasks.length - 1) return;
      afterId = tasks[index + 1].id;
    }
    void runWrite(opKey, async () => {
      await updateTask(sid, task.id, { after_id: afterId });
      await onChanged?.();
    });
  };

  const remove = (task: TaskItem) => {
    const sid = sessionId;
    if (!sid) return;
    const opKey = `task-${task.id}`;
    if (busy.has(opKey)) return; // F2
    void runWrite(opKey, async () => {
      const res = await deleteTask(sid, task.id);
      if (!res.success) {
        // 未知 id 是 **200 + success:false**，不是 HTTP 错误：只看有没有抛就会把失败当成功。
        console.warn('[TASK] delete rejected:', res.message);
        setLastError(`删除失败：${res.message || '未知原因'}`);
        return;
      }
      await onChanged?.();
    });
  };

  const submitInsert = async (slot: string) => {
    const sid = sessionId;
    if (!sid) return;
    if (busy.has(slot)) return; // F2
    const draft = insertDrafts[slot] ?? EMPTY_INSERT;
    const content = draft.content.trim();
    if (!content) return; // 空 content 不发请求（POST 端点会 422，别白发一次）
    const afterId = slotAfterId(slot);
    await runWrite(slot, async () => {
      // TaskCreateInput 的 detail/acceptance 在生成类型里是必填（后端有默认值），显式给空串。
      await createTask(
        sid,
        afterId === null
          ? { content, detail: draft.detail, acceptance: draft.acceptance }
          : { content, detail: draft.detail, acceptance: draft.acceptance, after_id: afterId },
      );
      closeInsert(slot);
      await onChanged?.();
    });
  };

  const renderField = (task: TaskItem, field: EditableField, mono = false, rows = 2) => {
    const key = draftKey(task.id, field);
    const draft = drafts[key];
    const value = draft === undefined ? task[field] : draft.value;
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
            // base 记的是**打开编辑时**的服务端值：过期检测靠它（F3）
            setDrafts(prev => ({ ...prev, [key]: { value: e.target.value, base: task[field] } }))
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
            onClick={() => setInsertSlot(prev => (prev === SLOT_END ? null : SLOT_END))}
          >
            <Plus className="w-3.5 h-3.5" />
          </RowButton>
        </div>

        {lastError && (
          // 写失败必须可见（F7）：受控的状态下拉在被拒之后弹回原值，看起来就是
          // 「点了没反应」。刻意是面板级一行小字，不给每个字段单独加 pending 状态
          // ——F6 修好之后回弹窗口已经很短，字段级状态是不成比例的。
          <div
            data-task-error
            className="px-3 py-1 text-[11px] text-red-600 bg-red-50 border-b border-red-100"
          >
            {lastError}
          </div>
        )}

        {/* 清单自身的高度上限（F8）：见 LIST_BOX_CLASS 的注释 */}
        <div className={`divide-y divide-gray-100 ${LIST_BOX_CLASS}`}>
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
                        setInsertSlot(prev => (prev === slotKey(task.id) ? null : slotKey(task.id)))
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
                      创建 {formatStamp(task.created_at)} · 更新 {formatStamp(task.updated_at)}
                    </div>
                  </div>
                )}

                {insertSlot === slotKey(task.id) && (
                  <div className="px-3 pb-2">
                    <InsertForm
                      busy={busy.has(slotKey(task.id))}
                      value={insertDrafts[slotKey(task.id)] ?? EMPTY_INSERT}
                      onChange={patch =>
                        setInsertDrafts(prev => ({
                          ...prev,
                          [slotKey(task.id)]: { ...EMPTY_INSERT, ...prev[slotKey(task.id)], ...patch },
                        }))
                      }
                      onCancel={() => closeInsert(slotKey(task.id))}
                      onSubmit={() => void submitInsert(slotKey(task.id))}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {insertSlot === SLOT_END && (
          <div className="px-3 py-2 border-t border-gray-100">
            <InsertForm
              busy={busy.has(SLOT_END)}
              value={insertDrafts[SLOT_END] ?? EMPTY_INSERT}
              onChange={patch =>
                setInsertDrafts(prev => ({
                  ...prev,
                  [SLOT_END]: { ...EMPTY_INSERT, ...prev[SLOT_END], ...patch },
                }))
              }
              onCancel={() => closeInsert(SLOT_END)}
              onSubmit={() => void submitInsert(SLOT_END)}
            />
          </div>
        )}

        {denominator > 0 && unfinished > 0 && (
          <div className="px-3 py-1.5 bg-gray-50 border-t border-gray-100">
            <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden">
              <div
                data-task-progress
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

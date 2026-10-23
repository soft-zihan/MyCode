import { useEffect, useRef, useState } from 'react';
import { GitBranch, ChevronDown, RefreshCw } from 'lucide-react';
import {
  fetchBranches, checkoutBranch, BranchCheckoutError, BranchListResponse, BranchEntry,
} from '../../../api/client';

interface BranchSwitcherProps {
  cwd: string;
  onSwitched?: (info: BranchListResponse) => void;
}

/**
 * 项目行上的分支切换器。
 *
 * 挂载时就拉一次（不只在下拉打开时），因为「当前在哪个分支」是要一直看得见的信息——
 * 与 worktree 清单同一个理由（SessionsPanel 的 loadWorktrees 也是每个项目拉一次）。
 *
 * 远端分支只在点刷新时 fetch：`git fetch` 要走网络，挂载时对每个项目都 fetch 一遍，
 * 项目一多首屏就会被网络拖住，而断网时还会白等一个超时。
 */
export function BranchSwitcher({ cwd, onSwitched }: BranchSwitcherProps) {
  const [info, setInfo] = useState<BranchListResponse | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  const load = async (refreshRemote = false) => {
    setBusy(true);
    setError(null);
    try {
      const next = await fetchBranches(cwd, refreshRemote);
      setInfo(next);
      // fetch 失败不是致命错误：本地已知的分支照样能用，把原因说出来就行
      if (next.fetch_error) setError(`fetch failed: ${next.fetch_error}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load branches');
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { load(false); }, [cwd]);

  // 点面板外面就收起来。用 mousedown 而不是 click：click 会在切换按钮自己的
  // onClick 之后触发，于是刚点开就被这条监听关掉。
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  if (!info || !info.git) return null;      // 非 git 项目：不显示入口（与 worktree 同一处理）

  const switchTo = async (ref: string) => {
    if (ref === info.current) { setOpen(false); return; }
    setBusy(true);
    setError(null);
    try {
      const next = await checkoutBranch(cwd, ref);
      setInfo(next);
      setOpen(false);
      onSwitched?.(next);
    } catch (err) {
      if (err instanceof BranchCheckoutError && err.dirty) {
        // 后端拒绝切换脏工作树。这里不复述整段中文原因，只把它原样给出来——
        // 它已经写清楚了该怎么办（提交或 stash），前端再包一层反而会丢信息。
        setError(err.message);
      } else {
        setError(err instanceof Error ? err.message : 'Failed to switch branch');
      }
    } finally {
      setBusy(false);
    }
  };

  const locals = info.branches.filter(b => b.kind === 'local');
  const remotes = info.branches.filter(b => b.kind === 'remote');
  const label = info.current ?? (info.detached ? 'detached' : '—');

  return (
    <div className="relative" ref={boxRef}>
      <button
        onClick={() => setOpen(v => !v)}
        disabled={busy}
        className="flex items-center gap-0.5 px-1 py-0.5 rounded text-[10px] text-emerald-700 bg-emerald-50 hover:bg-emerald-100 max-w-[110px] disabled:opacity-50"
        title={`Current branch: ${label}${info.dirty ? ' (uncommitted changes)' : ''}`}
      >
        <GitBranch className="w-3 h-3 flex-shrink-0" />
        <span className="truncate">{label}</span>
        {info.dirty && <span className="w-1 h-1 rounded-full bg-amber-500 flex-shrink-0" title="Uncommitted changes" />}
        <ChevronDown className="w-2.5 h-2.5 flex-shrink-0 opacity-60" />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 z-30 w-64 bg-white border border-gray-200 rounded-md shadow-lg">
          <div className="px-2 py-1.5 border-b border-gray-100 flex items-center justify-between">
            <span className="text-[10px] font-medium text-gray-500 uppercase tracking-wide">Branches</span>
            <button
              onClick={() => load(true)}
              disabled={busy}
              className="p-0.5 rounded hover:bg-gray-100 text-gray-500 disabled:opacity-40"
              title="git fetch --prune and reload"
            >
              <RefreshCw className={`w-3 h-3 ${busy ? 'animate-spin' : ''}`} />
            </button>
          </div>

          {error && (
            <div className="px-2 py-1.5 text-[10px] text-red-600 bg-red-50 border-b border-red-100 whitespace-pre-wrap">
              {error}
            </div>
          )}

          <div className="max-h-64 overflow-y-auto py-1">
            <BranchSection title="Local" items={locals} current={info.current ?? null} onPick={switchTo} />
            <BranchSection title="Remote" items={remotes} current={info.current ?? null} onPick={switchTo} />
          </div>
        </div>
      )}
    </div>
  );
}

function BranchSection({
  title, items, current, onPick,
}: {
  title: string;
  items: BranchEntry[];
  current: string | null;
  onPick: (ref: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <div className="px-2 pt-1 pb-0.5 text-[9px] text-gray-400 uppercase">{title}</div>
      {items.map(b => (
        <button
          key={b.name}
          onClick={() => onPick(b.name)}
          className={`w-full text-left px-2 py-1 text-[11px] flex items-center gap-1 hover:bg-indigo-50 ${
            isCurrentBranch(b.name, current) ? 'text-indigo-700 font-medium' : 'text-gray-700'
          }`}
        >
          <GitBranch className="w-3 h-3 flex-shrink-0 opacity-50" />
          <span className="truncate flex-1" title={b.name}>{b.name}</span>
          {b.head && <span className="text-[9px] text-gray-400 font-mono">{b.head}</span>}
        </button>
      ))}
    </div>
  );
}

/**
 * 远端项显示成 `origin/foo`，而 current 是本地分支名 `foo`——两者都要认成「当前」。
 * 否则切到 origin/foo 之后（后端会 DWIM 出本地 foo）下拉里两条都不高亮，
 * 看着像切换没生效。
 */
function isCurrentBranch(name: string, current: string | null): boolean {
  if (!current) return false;
  if (name === current) return true;
  const slash = name.indexOf('/');
  return slash >= 0 && name.slice(slash + 1) === current;
}

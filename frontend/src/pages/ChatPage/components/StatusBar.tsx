import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Folder, GitBranch, Monitor, Cloud, ChevronDown, RefreshCw, ExternalLink, Plus,
} from 'lucide-react';
import {
  fetchProjects, fetchWorktrees, fetchBranches, checkoutBranch, fetchCloudStatus,
  BranchListResponse, WorktreeEntry, Project, CloudStatus,
} from '../../../api/client';
import { CloudPanel } from './CloudPanel';

interface StatusBarProps {
  /** 当前生效的工作目录：有会话时是会话的 cwd，否则回退 lastCwd / 第一个项目 */
  cwd: string | null;
  /** 切换执行目标（本地根目录 / 某个 worktree）。由上层决定是切会话还是开新会话 */
  onSwitchCwd: (cwd: string) => void;
  /** 「打开别的目录」：复用已有的 DirectoryPicker */
  onOpenFolder: () => void;
}

/**
 * 底部状态栏：工作区 / 执行目标 / 分支，三段，照 Qoder、VS Code、Codex 的惯例放在窗口底部。
 *
 * 为什么放这里而不是项目行里：这三样描述的是「**当前**在哪干活」，是全局单一状态；
 * 塞进每个项目行会变成 N 份互相不一致的控件，看一眼不知道哪个算数（2026-10-24 用户原话：
 * 「学学 qoder 的布局，你弄得乱七八糟我都看不懂」）。行里只留与会话列表本身有关的东西。
 *
 * 三段的取值都从当前 cwd 推导：执行目标＝cwd 是项目根就是「本地」、是某个 worktree 就是
 * 那个 worktree 的名字；分支＝cwd 的 HEAD。云端不是本地会话的一种 cwd，所以它在
 * 执行目标菜单里单独一项，点开展示云端实例的探活与推送面板。
 */
export function StatusBar({ cwd, onSwitchCwd, onOpenFolder }: StatusBarProps) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [worktrees, setWorktrees] = useState<Record<string, WorktreeEntry[]>>({});
  const [branches, setBranches] = useState<BranchListResponse | null>(null);
  const [cloud, setCloud] = useState<CloudStatus | null>(null);
  const [menu, setMenu] = useState<null | 'workspace' | 'target' | 'branch' | 'cloud'>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const barRef = useRef<HTMLDivElement>(null);

  // 项目与 worktree 清单：用来把 cwd 翻译成「哪个项目 / 是不是 worktree」
  const loadProjects = useCallback(async () => {
    try {
      const ps = await fetchProjects();
      setProjects(ps);
      const wts = await Promise.all(
        ps.map(async p => {
          try {
            const { worktrees: list, git } = await fetchWorktrees(p.cwd);
            return [p.cwd, git ? list : []] as const;
          } catch {
            return [p.cwd, []] as const;
          }
        })
      );
      setWorktrees(Object.fromEntries(wts));
    } catch {
      /* 状态栏拿不到清单就只显示 cwd 本身，不值得弹错 */
    }
  }, []);

  const loadBranches = useCallback(async (dir: string | null, refresh = false) => {
    if (!dir) { setBranches(null); return; }
    try {
      setBranches(await fetchBranches(dir, refresh));
    } catch {
      setBranches(null);
    }
  }, []);

  useEffect(() => { loadProjects(); }, [loadProjects]);
  useEffect(() => { loadBranches(cwd); }, [cwd, loadBranches]);
  // 云端探活只在菜单打开时做：它是一次跨进程网络请求，不该为状态栏上的一颗点常驻轮询
  useEffect(() => {
    if (menu !== 'cloud') return;
    fetchCloudStatus().then(setCloud).catch(() => setCloud(null));
  }, [menu]);

  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) setMenu(null);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [menu]);

  const project = projects.find(p => p.cwd === cwd) ?? null;
  const ownerEntry = !project
    ? Object.values(worktrees).flat().find(w => w.directory === cwd) ?? null
    : null;
  const ownerProject = ownerEntry
    ? projects.find(p => (worktrees[p.cwd] || []).some(w => w.directory === cwd)) ?? null
    : project;
  const targetLabel = project ? '本地' : ownerEntry ? (ownerEntry.name || 'worktree') : '本地';
  const worktreeOptions = ownerProject ? (worktrees[ownerProject.cwd] || []).filter(w => w.kind === 'linked') : [];

  const switchBranch = async (ref: string) => {
    if (!cwd || ref === branches?.current) { setMenu(null); return; }
    setBusy(true);
    setError(null);
    try {
      const next = await checkoutBranch(cwd, ref);
      setBranches(next);
      setMenu(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to switch branch');
    } finally {
      setBusy(false);
    }
  };

  const dirName = cwd ? (cwd.split('/').pop() || cwd) : '未选择工作区';

  return (
    <div
      ref={barRef}
      className="flex-shrink-0 border-t border-gray-200 bg-gray-50 h-7 flex items-stretch text-[11px] text-gray-600 select-none relative z-20"
    >
      <BarSegment
        open={menu === 'workspace'}
        onToggle={() => setMenu(menu === 'workspace' ? null : 'workspace')}
        icon={<Folder className="w-3 h-3" />}
        label={project?.name || ownerProject?.name || dirName}
        title={cwd || '尚未选择工作区'}
      />
      <BarSegment
        open={menu === 'target'}
        onToggle={() => setMenu(menu === 'target' ? null : 'target')}
        icon={ownerEntry ? <GitBranch className="w-3 h-3" /> : <Monitor className="w-3 h-3" />}
        label={targetLabel}
        title={`执行目标：${targetLabel}${ownerEntry ? `（${cwd}）` : ''}`}
      />
      <BarSegment
        open={menu === 'branch'}
        onToggle={() => setMenu(menu === 'branch' ? null : 'branch')}
        icon={<GitBranch className="w-3 h-3" />}
        label={branches?.git ? (branches.current ?? (branches.detached ? 'detached' : '—')) : '非 git'}
        title={branches?.dirty ? '当前分支有未提交改动' : 'Git 分支'}
        dot={branches?.dirty ? 'bg-amber-500' : undefined}
      />

      <div className="flex-1" />

      <button
        onClick={() => setMenu(menu === 'cloud' ? null : 'cloud')}
        className={`px-2 flex items-center gap-1 hover:bg-gray-200/70 ${menu === 'cloud' ? 'bg-sky-100 text-sky-800' : ''}`}
        title="云端（K8s）实例状态与推送"
      >
        <Cloud className="w-3 h-3" />
        <span
          className={`w-1.5 h-1.5 rounded-full ${
            cloud === null ? 'bg-gray-300' : cloud.reachable ? 'bg-emerald-500' : 'bg-red-500'
          }`}
        />
        <span>云端</span>
      </button>

      {/* ── 菜单 ─────────────────────────────────────────────── */}
      {menu === 'workspace' && (
        <Menu width="w-72" align="left">
          <MenuHeader>工作区</MenuHeader>
          <div className="max-h-64 overflow-y-auto py-1">
            {projects.map(p => (
              <MenuItem
                key={p.cwd}
                active={p.cwd === cwd}
                onClick={() => { setMenu(null); if (p.cwd !== cwd) onSwitchCwd(p.cwd); }}
                icon={<Folder className="w-3 h-3 opacity-60" />}
                label={p.name || p.cwd.split('/').pop() || p.cwd}
                hint={p.cwd}
              />
            ))}
          </div>
          <div className="border-t border-gray-100 py-1">
            <MenuItem onClick={() => { setMenu(null); onOpenFolder(); }} icon={<Plus className="w-3 h-3 opacity-60" />} label="打开别的目录…" />
          </div>
        </Menu>
      )}

      {menu === 'target' && (
        <Menu width="w-72" align="left">
          <MenuHeader>执行目标</MenuHeader>
          <div className="py-1">
            {ownerProject && (
              <MenuItem
                active={!!project}
                onClick={() => { setMenu(null); if (!project) onSwitchCwd(ownerProject.cwd); }}
                icon={<Monitor className="w-3 h-3 opacity-60" />}
                label="本地"
                hint={ownerProject.cwd}
              />
            )}
            {worktreeOptions.map(w => (
              <MenuItem
                key={w.directory}
                active={w.directory === cwd}
                onClick={() => { setMenu(null); if (w.directory !== cwd) onSwitchCwd(w.directory); }}
                icon={<GitBranch className="w-3 h-3 opacity-60" />}
                label={w.name || w.directory.split('/').pop() || w.directory}
                hint={w.directory}
              />
            ))}
            {ownerProject && worktreeOptions.length === 0 && (
              <div className="px-2 py-1 text-[10px] text-gray-400">这个项目还没有 worktree</div>
            )}
          </div>
          <div className="border-t border-gray-100 py-1">
            <MenuItem
              onClick={() => setMenu('cloud')}
              icon={<Cloud className="w-3 h-3 opacity-60" />}
              label="云端（K8s 集群里的同一个后端）"
            />
          </div>
        </Menu>
      )}

      {menu === 'branch' && (
        <Menu width="w-72" align="left">
          <MenuHeader>
            <span className="flex-1">Git 分支 · {dirName}</span>
            <button
              onClick={() => loadBranches(cwd, true)}
              disabled={busy}
              className="p-0.5 rounded hover:bg-gray-100 text-gray-500 disabled:opacity-40"
              title="git fetch --prune 后重新加载"
            >
              <RefreshCw className={`w-3 h-3 ${busy ? 'animate-spin' : ''}`} />
            </button>
          </MenuHeader>
          {error && <div className="px-2 py-1.5 text-[10px] text-red-600 bg-red-50 whitespace-pre-wrap">{error}</div>}
          {branches?.fetch_error && (
            <div className="px-2 py-1.5 text-[10px] text-amber-700 bg-amber-50 whitespace-pre-wrap">
              fetch 失败：{branches.fetch_error}
            </div>
          )}
          <div className="max-h-64 overflow-y-auto py-1">
            {!branches?.git && <div className="px-2 py-1 text-[10px] text-gray-400">当前工作区不是 git 仓库</div>}
            {(['local', 'remote'] as const).map(kind => {
              const items = (branches?.branches || []).filter(b => b.kind === kind);
              if (items.length === 0) return null;
              return (
                <div key={kind}>
                  <div className="px-2 pt-1 pb-0.5 text-[9px] text-gray-400 uppercase">{kind}</div>
                  {items.map(b => (
                    <MenuItem
                      key={b.name}
                      active={isCurrent(b.name, branches?.current ?? null)}
                      onClick={() => switchBranch(b.name)}
                      icon={<GitBranch className="w-3 h-3 opacity-50" />}
                      label={b.name}
                      hint={b.head ?? undefined}
                    />
                  ))}
                </div>
              );
            })}
          </div>
        </Menu>
      )}

      {menu === 'cloud' && (
        <div className="absolute bottom-full right-0 mb-1 w-96 max-w-[90vw] bg-white border border-gray-200 rounded-md shadow-xl p-2">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] font-medium text-gray-500 uppercase tracking-wide">云端实例</span>
            {cloud?.reachable && (
              <a
                href={cloud.url}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-0.5 text-[10px] text-sky-700 hover:text-sky-900"
              >
                在浏览器打开 <ExternalLink className="w-2.5 h-2.5" />
              </a>
            )}
          </div>
          {cwd ? <CloudPanel cwd={cwd} /> : <div className="text-[11px] text-gray-400 p-2">先选一个工作区</div>}
        </div>
      )}
    </div>
  );
}

/** 远端项是 origin/foo，current 是本地名 foo，两者都算「当前」，否则高亮会消失 */
function isCurrent(name: string, current: string | null): boolean {
  if (!current) return false;
  if (name === current) return true;
  const slash = name.indexOf('/');
  return slash >= 0 && name.slice(slash + 1) === current;
}

function BarSegment({
  open, onToggle, icon, label, title, dot,
}: {
  open: boolean;
  onToggle: () => void;
  icon: React.ReactNode;
  label: string;
  title: string;
  dot?: string;
}) {
  return (
    <button
      onClick={onToggle}
      title={title}
      className={`px-2 flex items-center gap-1 hover:bg-gray-200/70 max-w-[220px] ${open ? 'bg-gray-200' : ''}`}
    >
      {icon}
      <span className="truncate">{label}</span>
      {dot && <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dot}`} />}
      <ChevronDown className="w-2.5 h-2.5 opacity-50 flex-shrink-0" />
    </button>
  );
}

function Menu({ children, width, align }: { children: React.ReactNode; width: string; align: 'left' | 'right' }) {
  return (
    <div
      className={`absolute bottom-full mb-1 ${width} max-w-[90vw] bg-white border border-gray-200 rounded-md shadow-xl ${
        align === 'left' ? 'left-0' : 'right-0'
      }`}
    >
      {children}
    </div>
  );
}

function MenuHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-2 py-1.5 border-b border-gray-100 flex items-center text-[10px] font-medium text-gray-500 uppercase tracking-wide">
      {children}
    </div>
  );
}

function MenuItem({
  active, onClick, icon, label, hint,
}: {
  active?: boolean;
  onClick: () => void;
  icon?: React.ReactNode;
  label: string;
  hint?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-2 py-1 flex items-center gap-1.5 hover:bg-indigo-50 ${
        active ? 'text-indigo-700 bg-indigo-50/60 font-medium' : 'text-gray-700'
      }`}
    >
      {icon}
      <span className="truncate flex-1">{label}</span>
      {hint && <span className="text-[9px] text-gray-400 font-mono truncate max-w-[45%]" title={hint}>{hint}</span>}
    </button>
  );
}

import { useCallback, useEffect, useState } from 'react';
import { Cloud, UploadCloud, ExternalLink, RefreshCw, AlertTriangle, CheckCircle2 } from 'lucide-react';
import {
  fetchCloudStatus, fetchCloudWorkspaces, pushProjectToCloud, fetchBranches,
  CloudStatus, CloudWorkspaceList, CloudImportResponse, BranchListResponse,
} from '../../../api/client';

interface CloudPanelProps {
  cwd: string;
}

/**
 * 「云端」执行目标面板。
 *
 * 云端实例就是 K8s 里那份**同一个后端**（deploy/base/deployment.yaml），所以这里不做什么
 * 特殊的远程协议：先探活，再把「当前项目 + 当前分支」推过去——云端自己去 git remote 克隆
 * 到它自己的 PVC 上（POST /api/workspace/import 在云端实例里执行）。这与 Codex cloud 的
 * 形状一致：云端环境自己拉代码，而不是把本地文件拷进去。
 *
 * 于是有一个必须说清楚的前提：**推的是远端仓库上的那个 ref，不是本地工作树**。
 * 本地没 push 的提交、以及只存在于本地的分支，云端拿不到——下面对此显式告警，
 * 而不是让用户推完之后在云端实例里困惑「我的改动呢」。
 */
export function CloudPanel({ cwd }: CloudPanelProps) {
  const [status, setStatus] = useState<CloudStatus | null>(null);
  const [branches, setBranches] = useState<BranchListResponse | null>(null);
  const [workspaces, setWorkspaces] = useState<CloudWorkspaceList | null>(null);
  const [pushed, setPushed] = useState<CloudImportResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      // 探活与读本地 git 状态互不依赖，并行拉；云端工作区清单要等探活成功才有意义
      const [st, br] = await Promise.all([
        fetchCloudStatus(),
        fetchBranches(cwd).catch(() => null),
      ]);
      setStatus(st);
      setBranches(br);
      setWorkspaces(st.reachable ? await fetchCloudWorkspaces().catch(() => null) : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load cloud status');
    } finally {
      setBusy(false);
    }
  }, [cwd]);

  useEffect(() => { load(); }, [load]);

  const remoteUrl = branches?.remote_url ?? null;
  const current = branches?.current ?? null;
  // 只存在于本地的分支，云端 clone 不到。判据：远端清单里有没有 <remote>/<current>。
  const onRemote = !!current && !!branches?.branches.some(
    b => b.kind === 'remote' && b.name.slice(b.name.indexOf('/') + 1) === current
  );
  const canPush = !!remoteUrl && !!current && onRemote;

  const handlePush = async () => {
    if (!remoteUrl || !current) return;
    setBusy(true);
    setError(null);
    setPushed(null);
    try {
      const r = await pushProjectToCloud({ repo_url: remoteUrl, ref: current });
      setPushed(r);
      setWorkspaces(await fetchCloudWorkspaces().catch(() => null));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to push to cloud');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1">
        <Cloud className="w-3 h-3 text-sky-600 flex-shrink-0" />
        <span className="text-[10px] font-medium text-sky-800 uppercase tracking-wide">Cloud</span>
        <span className="flex-1" />
        <button
          onClick={load}
          disabled={busy}
          className="p-0.5 rounded hover:bg-sky-100 text-sky-700 disabled:opacity-40"
          title="Re-probe cloud instance"
        >
          <RefreshCw className={`w-3 h-3 ${busy ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {status && (
        <div className="flex items-center gap-1.5 text-[11px]">
          <span
            className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
              status.reachable ? 'bg-emerald-500' : 'bg-red-500'
            }`}
            title={status.reachable ? 'reachable' : 'unreachable'}
          />
          <span className="text-gray-700 truncate font-mono" title={status.url}>{status.url}</span>
          {status.reachable && status.version && (
            <span className="text-gray-400 text-[10px] flex-shrink-0">
              v{status.version}{status.latency_ms != null ? ` · ${status.latency_ms}ms` : ''}
            </span>
          )}
          {status.reachable && (
            <a
              href={status.url}
              target="_blank"
              rel="noreferrer"
              className="ml-auto flex items-center gap-0.5 text-[10px] text-sky-700 hover:text-sky-900 flex-shrink-0"
              title="Open the cloud instance in a new tab"
            >
              Open <ExternalLink className="w-2.5 h-2.5" />
            </a>
          )}
        </div>
      )}

      {status && !status.reachable && (
        <Hint>
          Cloud instance not reachable ({status.error || 'unknown error'}).
          <br />
          Start it with <code className="font-mono bg-white/70 px-1 rounded">mycode cloud-up</code>.
        </Hint>
      )}

      {status?.reachable && status.auth_required && !status.authenticated && (
        <Hint warn>
          Cloud instance requires a token and this backend has none.
          <br />
          <code className="font-mono bg-white/70 px-1 rounded">mycode cloud-up</code> writes
          {' '}<code className="font-mono bg-white/70 px-1 rounded">~/.my-code/cloud.json</code>; restart the backend after it.
        </Hint>
      )}

      {error && <Hint warn>{error}</Hint>}

      {status?.reachable && (
        <>
          <div className="text-[10px] text-gray-500 leading-snug">
            {branches?.git ? (
              remoteUrl ? (
                <>
                  Source: <span className="font-mono text-gray-700">{remoteUrl}</span>
                  {current && <> @ <span className="font-mono text-gray-700">{current}</span></>}
                </>
              ) : (
                'This project has no git remote — the cloud clones from a remote, so there is nothing to push.'
              )
            ) : (
              'Not a git repository — the cloud clones from a remote, so this project cannot be pushed.'
            )}
          </div>

          {remoteUrl && current && !onRemote && (
            <Hint warn>
              Branch <span className="font-mono">{current}</span> only exists locally.
              {' '}<code className="font-mono bg-white/70 px-1 rounded">git push -u origin {current}</code> first,
              otherwise the cloud would clone without it.
            </Hint>
          )}

          <button
            onClick={handlePush}
            disabled={!canPush || busy}
            className="flex items-center gap-1 px-2 py-1 rounded text-[11px] bg-sky-600 text-white hover:bg-sky-700 disabled:opacity-40 disabled:cursor-not-allowed"
            title={canPush ? 'Clone this repo@branch into the cloud instance workspace' : 'Not pushable (see above)'}
          >
            <UploadCloud className="w-3 h-3" />
            {busy ? 'Working…' : `Push ${current || 'repo'} to cloud`}
          </button>

          {pushed && (
            <div className="flex items-start gap-1 text-[10px] text-emerald-800 bg-emerald-50 border border-emerald-200 rounded px-1.5 py-1">
              <CheckCircle2 className="w-3 h-3 flex-shrink-0 mt-px" />
              <span className="leading-snug">
                {pushed.created ? 'Cloned' : 'Updated'} into{' '}
                <span className="font-mono">{pushed.directory}</span>
                {pushed.branch && <> on <span className="font-mono">{pushed.branch}</span></>}
                {pushed.head && <> (<span className="font-mono">{pushed.head}</span>)</>}.
                {' '}Open the cloud instance and pick it from the project list.
              </span>
            </div>
          )}

          {workspaces && workspaces.workspaces.length > 0 && (
            <div>
              <div className="text-[9px] text-gray-400 uppercase mb-0.5">
                Cloud workspaces · <span className="font-mono normal-case">{workspaces.root}</span>
              </div>
              {workspaces.workspaces.map(w => (
                <div key={w.directory} className="flex items-center gap-1 text-[11px] text-gray-600 py-0.5">
                  <Cloud className="w-3 h-3 text-sky-400 flex-shrink-0" />
                  <span className="truncate flex-1" title={w.directory}>{w.name}</span>
                  {w.branch && <span className="font-mono text-[9px] text-gray-400">{w.branch}</span>}
                  {w.head && <span className="font-mono text-[9px] text-gray-400">{w.head}</span>}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Hint({ children, warn }: { children: React.ReactNode; warn?: boolean }) {
  return (
    <div
      className={`flex items-start gap-1 text-[10px] rounded px-1.5 py-1 leading-snug ${
        warn ? 'text-amber-900 bg-amber-50 border border-amber-200' : 'text-gray-600 bg-white/70 border border-gray-200'
      }`}
    >
      <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-px" />
      <span>{children}</span>
    </div>
  );
}

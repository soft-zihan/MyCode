import { useCallback, useEffect, useState } from 'react';
import {
  AUTH_REQUIRED_EVENT,
  getToken,
  setToken,
} from '../../api/auth';

/**
 * U13：服务端启用 MYCODE_AUTH_TOKEN 后的 token 输入门。
 * 挂载即查询 /api/auth/status（豁免端点）；需要鉴权且本地无 token、
 * 或任意 API 返回 401 / WS 被 4401 关闭时弹出全屏输入。
 */
export default function AuthGate() {
  const [required, setRequired] = useState(false);
  const [token, setTokenInput] = useState('');

  useEffect(() => {
    let cancelled = false;
    fetch('/api/auth/status')
      .then((res) => res.json())
      .then((data: { auth_required?: boolean }) => {
        if (!cancelled && data.auth_required && !getToken()) {
          setRequired(true);
        }
      })
      .catch(() => {
        /* 状态端点不可达时不打扰用户，401 拦截会再触发 */
      });
    const onAuthRequired = () => setRequired(true);
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
    return () => {
      cancelled = true;
      window.removeEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
    };
  }, []);

  const handleConnect = useCallback(() => {
    if (!token.trim()) return;
    setToken(token.trim());
    window.location.reload();
  }, [token]);

  if (!required) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-gray-900/60">
      <div className="w-96 rounded-lg bg-white p-6 shadow-xl">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">需要访问令牌</h2>
        <p className="text-sm text-gray-600 mb-4">
          服务端已启用 token 鉴权（MYCODE_AUTH_TOKEN）。输入令牌后连接。
        </p>
        <input
          type="password"
          value={token}
          onChange={(e) => setTokenInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleConnect()}
          placeholder="Access token"
          autoFocus
          className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 mb-4"
        />
        <button
          onClick={handleConnect}
          disabled={!token.trim()}
          className="w-full px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          连接
        </button>
      </div>
    </div>
  );
}

import { useEffect, useState } from 'react';
import { Cpu, Save, CheckCircle, XCircle, Loader } from 'lucide-react';
import {
  fetchEmbeddingConfig, saveEmbeddingConfig, verifyEmbeddingConfig,
  EmbeddingVerifyResult,
} from '../../api/client';

/**
 * Embedding 模型配置（BC-30）：wiki 语义召回/编译去重的向量化后端。
 * 独立于聊天模型端点——embedding 走专用 API（默认 SiliconFlow 免费模型）。
 */
export function EmbeddingConfigSection() {
  const [backend, setBackend] = useState('openai');
  const [baseUrl, setBaseUrl] = useState('https://api.siliconflow.cn/v1');
  const [model, setModel] = useState('BAAI/bge-large-zh-v1.5');
  const [apiKey, setApiKey] = useState('');
  const [apiKeySet, setApiKeySet] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [verifyResult, setVerifyResult] = useState<EmbeddingVerifyResult | null>(null);

  useEffect(() => {
    fetchEmbeddingConfig()
      .then((cfg) => {
        setBackend(cfg.backend);
        setBaseUrl(cfg.base_url);
        setModel(cfg.model);
        setApiKeySet(cfg.api_key_set);
      })
      .catch((e) => setMessage({ ok: false, text: `加载配置失败: ${e.message}` }))
      .finally(() => setLoading(false));
  }, []);

  const isOpenAI = backend === 'openai';

  const handleVerify = async () => {
    setVerifying(true);
    setVerifyResult(null);
    setMessage(null);
    try {
      const result = await verifyEmbeddingConfig({
        backend,
        base_url: baseUrl,
        model,
        api_key: apiKey || null, // 留空=用已保存密钥验证
      });
      setVerifyResult(result);
    } catch (e: any) {
      setMessage({ ok: false, text: `验证请求失败: ${e.message}` });
    } finally {
      setVerifying(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMessage(null);
    try {
      await saveEmbeddingConfig({
        backend,
        base_url: baseUrl,
        model,
        api_key: apiKey || null, // 留空=保留已存密钥
      });
      if (apiKey) {
        setApiKeySet(true);
        setApiKey('');
      }
      setMessage({ ok: true, text: '已保存。embedding 缓存按模型名分文件，切换模型后旧向量自动弃用。' });
    } catch (e: any) {
      setMessage({ ok: false, text: `保存失败: ${e.message}` });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="p-6 flex items-center gap-2 text-sm text-gray-500">
        <Loader className="w-4 h-4 animate-spin" /> 加载中…
      </div>
    );
  }

  const inputCls =
    'w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400';

  return (
    <div className="h-full overflow-y-auto">
      <div className="p-6 border-b border-gray-200">
        <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
          <Cpu className="w-6 h-6 text-indigo-500" />
          Embedding 模型
        </h1>
        <p className="text-sm text-gray-500 mt-1">
          wiki 语义召回与编译去重使用的向量化后端，独立于聊天模型端点。
        </p>
      </div>

      <div className="p-6 max-w-2xl space-y-5">
        <div>
          <label className="block text-xs font-semibold text-gray-600 mb-1.5">后端类型</label>
          <select
            className={inputCls}
            value={backend}
            onChange={(e) => setBackend(e.target.value)}
          >
            <option value="openai">OpenAI 兼容 API（云端，推荐）</option>
            <option value="ollama">ollama（本地 localhost:11434）</option>
          </select>
        </div>

        {isOpenAI && (
          <>
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1.5">Base URL</label>
              <input
                className={inputCls}
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.siliconflow.cn/v1"
              />
              <p className="text-[11px] text-gray-400 mt-1">
                OpenAI 兼容根路径（不含 /embeddings），如 SiliconFlow：https://api.siliconflow.cn/v1
              </p>
            </div>
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1.5">API Key</label>
              <input
                className={inputCls}
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={apiKeySet ? '已配置（留空保持不变）' : '输入 API Key'}
                autoComplete="new-password"
              />
              <p className="text-[11px] text-gray-400 mt-1">
                密钥仅保存在本机 ~/.my-code/config.json，接口不回显。
              </p>
            </div>
          </>
        )}

        <div>
          <label className="block text-xs font-semibold text-gray-600 mb-1.5">模型名</label>
          <input
            className={inputCls}
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={isOpenAI ? 'BAAI/bge-large-zh-v1.5' : 'qwen3-embedding'}
          />
          <p className="text-[11px] text-gray-400 mt-1">
            {isOpenAI
              ? 'SiliconFlow 免费模型：BAAI/bge-large-zh-v1.5（1024 维，中文优）'
              : 'ollama 本地已拉取的 embedding 模型名'}
          </p>
        </div>

        <div className="flex items-center gap-3 pt-1">
          <button
            onClick={handleVerify}
            disabled={verifying || saving}
            className="flex items-center px-4 py-2 bg-gray-600 text-white text-sm rounded hover:bg-gray-700 disabled:opacity-50 transition-colors"
          >
            {verifying ? <Loader className="w-4 h-4 mr-2 animate-spin" /> : <CheckCircle className="w-4 h-4 mr-2" />}
            验证连接
          </button>
          <button
            onClick={handleSave}
            disabled={saving || verifying}
            className="flex items-center px-4 py-2 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700 disabled:opacity-50 transition-colors"
          >
            {saving ? <Loader className="w-4 h-4 mr-2 animate-spin" /> : <Save className="w-4 h-4 mr-2" />}
            保存
          </button>
        </div>

        {verifyResult && (
          <div
            className={`flex items-start gap-2 p-3 rounded text-sm ${
              verifyResult.status === 'success'
                ? 'bg-green-50 text-green-800'
                : 'bg-red-50 text-red-700'
            }`}
          >
            {verifyResult.status === 'success' ? (
              <CheckCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            ) : (
              <XCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            )}
            <span>
              {verifyResult.message}
              {verifyResult.status === 'success' && (
                <span className="text-green-600">
                  {' '}· 维度 {verifyResult.dim} · 延迟 {verifyResult.latency_ms}ms
                </span>
              )}
            </span>
          </div>
        )}

        {message && (
          <div
            className={`flex items-start gap-2 p-3 rounded text-sm ${
              message.ok ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-700'
            }`}
          >
            {message.ok ? (
              <CheckCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            ) : (
              <XCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            )}
            <span>{message.text}</span>
          </div>
        )}
      </div>
    </div>
  );
}

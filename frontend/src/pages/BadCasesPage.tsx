import { useState, useEffect } from 'react';
import { AlertCircle, CheckCircle, Clock, XCircle, RefreshCw, ExternalLink } from 'lucide-react';
import { PageLayout } from '../components/PageLayout';

interface BadCase {
  id: string;
  session_id: string;
  source: string;
  status: string;
  severity: string;
  turn_number: number | null;
  step_number: number | null;
  tool_name: string | null;
  signal_type: string;
  reason: string;
  comment: string;
  expected_tool: string | null;
  created_at: number;
  updated_at: number;
}

interface BadCaseStats {
  total: number;
  pending: number;
  verified: number;
  fixed: number;
  flaky: number;
  ignored: number;
  by_source: {
    user_feedback: number;
    auto_detect: number;
    eval: number;
  };
}

const STATUS_CONFIG = {
  pending: { label: '待审核', color: 'text-yellow-600', icon: Clock },
  verified: { label: '已验证', color: 'text-indigo-600', icon: AlertCircle },
  fixed: { label: '已修复', color: 'text-green-600', icon: CheckCircle },
  flaky: { label: '偶发', color: 'text-gray-600', icon: XCircle },
  ignored: { label: '已忽略', color: 'text-gray-400', icon: XCircle },
};

const SEVERITY_CONFIG = {
  high: { label: '高', color: 'bg-red-100 text-red-800' },
  medium: { label: '中', color: 'bg-yellow-100 text-yellow-800' },
  low: { label: '低', color: 'bg-gray-100 text-gray-800' },
};

const SOURCE_CONFIG = {
  user_feedback: { label: '用户反馈', color: 'bg-purple-100 text-purple-800' },
  auto_detect: { label: '自动检测', color: 'bg-indigo-100 text-indigo-800' },
  eval: { label: '评测失败', color: 'bg-red-100 text-red-800' },
};

export default function BadCasesPage() {
  const [badCases, setBadCases] = useState<BadCase[]>([]);
  const [stats, setStats] = useState<BadCaseStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<string>('all');

  const loadBadCases = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filter !== 'all') params.set('source', filter);
      if (statusFilter !== 'all') params.set('status', statusFilter);
      
      const response = await fetch(`/api/bad-cases?${params}`);
      const data = await response.json();
      setBadCases(data.items);
      
      const statsResponse = await fetch('/api/bad-cases/stats/summary');
      const statsData = await statsResponse.json();
      setStats(statsData);
    } catch (error) {
      console.error('Failed to load bad cases:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadBadCases();
  }, [filter, statusFilter]);

  const handleUpdateStatus = async (badCaseId: string, status: string) => {
    try {
      await fetch(`/api/bad-cases/${badCaseId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      loadBadCases();
    } catch (error) {
      console.error('Failed to update status:', error);
    }
  };

  const formatTime = (timestamp: number) => {
    return new Date(timestamp * 1000).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <PageLayout sidebarContent={null}>
      <div className="p-6 space-y-6">
        <h1 className="text-2xl font-bold text-gray-900">Bad Case 墙</h1>
        {/* 统计卡片 */}
        {stats && (
          <div className="grid grid-cols-2 md:grid-cols-6 gap-4">
            <div className="bg-white rounded-lg p-4 border border-gray-200">
              <div className="text-2xl font-bold text-gray-900">{stats.total}</div>
              <div className="text-sm text-gray-500">总计</div>
            </div>
            <div className="bg-white rounded-lg p-4 border border-yellow-200">
              <div className="text-2xl font-bold text-yellow-600">{stats.pending}</div>
              <div className="text-sm text-gray-500">待审核</div>
            </div>
            <div className="bg-white rounded-lg p-4 border border-indigo-200">
              <div className="text-2xl font-bold text-indigo-600">{stats.verified}</div>
              <div className="text-sm text-gray-500">已验证</div>
            </div>
            <div className="bg-white rounded-lg p-4 border border-green-200">
              <div className="text-2xl font-bold text-green-600">{stats.fixed}</div>
              <div className="text-sm text-gray-500">已修复</div>
            </div>
            <div className="bg-white rounded-lg p-4 border border-gray-200">
              <div className="text-2xl font-bold text-gray-600">{stats.flaky}</div>
              <div className="text-sm text-gray-500">偶发</div>
            </div>
            <div className="bg-white rounded-lg p-4 border border-gray-200">
              <div className="text-2xl font-bold text-gray-400">{stats.ignored}</div>
              <div className="text-sm text-gray-500">已忽略</div>
            </div>
          </div>
        )}

        {/* 筛选 */}
        <div className="flex flex-wrap gap-2">
          <div className="flex gap-1">
            <button
              onClick={() => setFilter('all')}
              className={`px-3 py-1.5 text-sm rounded-md ${
                filter === 'all'
                  ? 'bg-indigo-600 text-white'
                  : 'bg-gray-100 text-gray-700'
              }`}
            >
              全部
            </button>
            <button
              onClick={() => setFilter('user_feedback')}
              className={`px-3 py-1.5 text-sm rounded-md ${
                filter === 'user_feedback'
                  ? 'bg-purple-600 text-white'
                  : 'bg-gray-100 text-gray-700'
              }`}
            >
              用户反馈
            </button>
            <button
              onClick={() => setFilter('auto_detect')}
              className={`px-3 py-1.5 text-sm rounded-md ${
                filter === 'auto_detect'
                  ? 'bg-indigo-600 text-white'
                  : 'bg-gray-100 text-gray-700'
              }`}
            >
              自动检测
            </button>
            <button
              onClick={() => setFilter('eval')}
              className={`px-3 py-1.5 text-sm rounded-md ${
                filter === 'eval'
                  ? 'bg-red-600 text-white'
                  : 'bg-gray-100 text-gray-700'
              }`}
            >
              评测失败
            </button>
          </div>
          
          <div className="flex gap-1 ml-auto">
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="px-3 py-1.5 text-sm rounded-md bg-gray-100 text-gray-700"
            >
              <option value="all">所有状态</option>
              <option value="pending">待审核</option>
              <option value="verified">已验证</option>
              <option value="fixed">已修复</option>
              <option value="flaky">偶发</option>
              <option value="ignored">已忽略</option>
            </select>
            
            <button
              onClick={loadBadCases}
              className="p-1.5 rounded-md bg-gray-100 text-gray-700 hover:bg-gray-200"
              title="刷新"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>

        {/* Bad Case 列表 */}
        <div className="space-y-3">
          {badCases.map((badCase) => {
            const StatusIcon = STATUS_CONFIG[badCase.status as keyof typeof STATUS_CONFIG]?.icon || Clock;
            const statusColor = STATUS_CONFIG[badCase.status as keyof typeof STATUS_CONFIG]?.color || 'text-gray-600';
            const severityColor = SEVERITY_CONFIG[badCase.severity as keyof typeof SEVERITY_CONFIG]?.color || '';
            const sourceColor = SOURCE_CONFIG[badCase.source as keyof typeof SOURCE_CONFIG]?.color || '';
            
            return (
              <div
                key={badCase.id}
                className="bg-white rounded-lg border border-gray-200 p-4"
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-2">
                      <StatusIcon className={`w-4 h-4 ${statusColor}`} />
                      <span className={`text-sm font-medium ${statusColor}`}>
                        {STATUS_CONFIG[badCase.status as keyof typeof STATUS_CONFIG]?.label}
                      </span>
                      <span className={`px-2 py-0.5 text-xs rounded-full ${severityColor}`}>
                        {SEVERITY_CONFIG[badCase.severity as keyof typeof SEVERITY_CONFIG]?.label}
                      </span>
                      <span className={`px-2 py-0.5 text-xs rounded-full ${sourceColor}`}>
                        {SOURCE_CONFIG[badCase.source as keyof typeof SOURCE_CONFIG]?.label}
                      </span>
                      <span className="text-xs text-gray-500">
                        {formatTime(badCase.created_at)}
                      </span>
                    </div>
                    
                    <div className="text-sm text-gray-900 mb-1">
                      <span className="font-mono text-xs bg-gray-100 px-1.5 py-0.5 rounded">
                        {badCase.session_id.slice(0, 8)}
                      </span>
                      {badCase.turn_number && (
                        <span className="ml-2 text-xs text-gray-500">
                          Turn {badCase.turn_number}
                          {badCase.step_number && `, Step ${badCase.step_number}`}
                        </span>
                      )}
                    </div>
                    
                    <div className="text-sm text-gray-700">
                      {badCase.reason}
                    </div>
                    
                    {badCase.comment && (
                      <div className="text-xs text-gray-500 mt-1">
                        {badCase.comment}
                      </div>
                    )}
                    
                  </div>
                  
                  <div className="flex gap-1 ml-4">
                    {badCase.status === 'pending' && (
                      <>
                        <button
                          onClick={() => handleUpdateStatus(badCase.id, 'verified')}
                          className="px-2 py-1 text-xs rounded bg-green-100 text-green-700 hover:bg-green-200"
                        >
                          确认
                        </button>
                        <button
                          onClick={() => handleUpdateStatus(badCase.id, 'ignored')}
                          className="px-2 py-1 text-xs rounded bg-gray-100 text-gray-700 hover:bg-gray-200"
                        >
                          忽略
                        </button>
                      </>
                    )}
                    
                    {badCase.status === 'verified' && (
                      <button
                        onClick={() => handleUpdateStatus(badCase.id, 'fixed')}
                        className="px-2 py-1 text-xs rounded bg-green-100 text-green-700 hover:bg-green-200"
                      >
                        标记已修复
                      </button>
                    )}
                    
                    <a
                      href={`/sessions/${badCase.session_id}`}
                      className="px-2 py-1 text-xs rounded bg-gray-100 text-gray-700 hover:bg-gray-200 flex items-center gap-1"
                      title="查看 Session"
                    >
                      <ExternalLink className="w-3 h-3" />
                      Session
                    </a>
                  </div>
                </div>
              </div>
            );
          })}
          
          {badCases.length === 0 && !loading && (
            <div className="text-center py-12 text-gray-500">
              暂无 bad case
            </div>
          )}
        </div>
      </div>
    </PageLayout>
  );
}

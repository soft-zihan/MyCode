import React from 'react';
import { Zap } from 'lucide-react';
import { sessionStore } from '../../store/SessionStore';
import { setAutoApprove } from '../../api/client';

/**
 * 每会话自动审批开关（裁定 1：每会话一个，不做全局总闸——「人需要走开时
 * 自然会开一个自动审批」）。
 *
 * 这是安全阀，打开时**必须视觉上明显**：关闭 = 幽灵按钮，打开 = 琥珀警示色
 * 实心。与旁边 PermissionSelect 的 YOLO（绿色系）视觉语言协调但可区分。
 *
 * 状态单一来源是 SessionStore（每会话槽位，切会话天然隔离）；点击做乐观
 * 更新，PUT 失败回滚并 console.error('[AUTOAPPROVE]', ...)。PUT 成功以后端
 * 回读的 enabled 为准。
 */
interface AutoApproveToggleProps {
  sessionId: string | null;
  enabled: boolean;
  disabled?: boolean;
}

export const AutoApproveToggle: React.FC<AutoApproveToggleProps> = ({
  sessionId,
  enabled,
  disabled = false,
}) => {
  const handleClick = async () => {
    if (!sessionId || disabled) return;
    const previous = enabled;
    const next = !enabled;
    // 乐观更新：父组件订阅同一个 store 槽位，视觉立即变化
    sessionStore.setAutoApprove(sessionId, next);
    try {
      const res = await setAutoApprove(sessionId, next);
      sessionStore.setAutoApprove(sessionId, res.enabled);
    } catch (err) {
      sessionStore.setAutoApprove(sessionId, previous);
      console.error('[AUTOAPPROVE] Failed to toggle auto-approve:', err);
    }
  };

  const inactive = disabled || !sessionId;

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={inactive}
      aria-pressed={enabled}
      data-auto-approve={enabled ? 'on' : 'off'}
      title={
        '本会话内自动批准 plan 审批与危险操作确认（每会话一个开关，默认关闭）。\n' +
        '打开后：正在等待的确认立即放行（热生效）；plan 审批自动走「批准并自动执行」路由，' +
        '不是「逐步确认」。\n' +
        '人走开时打开，回来后记得关掉。'
      }
      className={`auto-approve-toggle flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded border transition-colors ${
        enabled
          ? 'bg-amber-500 text-white border-amber-600 hover:bg-amber-600'
          : 'bg-transparent text-gray-500 border-gray-300 hover:bg-gray-50'
      } ${inactive ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
    >
      <Zap className="w-3.5 h-3.5" />
      自动审批
    </button>
  );
};

export default AutoApproveToggle;

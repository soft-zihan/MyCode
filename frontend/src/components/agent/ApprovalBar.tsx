import { ShieldAlert } from 'lucide-react';

interface ApprovalBarProps {
  toolName: string;
  command?: string;
  onApprove: () => void;
  onDeny: () => void;
}

/**
 * 工具授权请求条（非 plan 审批的通用权限确认）。
 * 与 PlanApprovalDialog 同一视觉语言：左侧语义色条 + 卡片 + 分级按钮。
 */
export function ApprovalBar({ toolName, command, onApprove, onDeny }: ApprovalBarProps) {
  return (
    <div className="mx-4 mb-2 animate-fade-in">
      <div className="rounded-xl border border-warning-200 border-l-4 border-l-warning bg-white shadow-sm overflow-hidden">
        <div className="flex items-start gap-3 px-4 py-3">
          <ShieldAlert className="w-4 h-4 text-warning-600 flex-shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-gray-900">需要授权</span>
              <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 font-mono">
                {toolName}
              </span>
            </div>
            {command && (
              <pre className="mt-2 text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded-lg p-2.5 overflow-x-auto overflow-y-auto whitespace-pre-wrap font-mono max-h-32">
                {command}
              </pre>
            )}
          </div>
          <div className="flex gap-2 flex-shrink-0">
            <button onClick={onDeny} className="btn-danger btn-sm">
              拒绝
            </button>
            <button onClick={onApprove} className="btn-primary btn-sm">
              批准
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default ApprovalBar;

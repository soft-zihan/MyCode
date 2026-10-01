import { WikiPanel } from './WikiPanel';

interface WikiPlanPanelProps {
  cwd: string | null;
  onFileSelect: (path: string) => void;
  selectedFile: string | null;
}

// 这里曾经有两个子标签：Wiki 与 Plan。Plan 子标签挂的是 PlanControlPanel——
// plan 系统的执行仪表盘（进度条、skip/redo、pause/resume/abandon、产物查看、
// ledger 日志、策略选择器）。执行状态的唯一载体已经是 task_list（由
// components/chat/TaskListPanel.tsx 呈现），那 8 个 REST 端点与 PlanControlPanel
// 一起删掉了，Plan 子标签因此再无内容，连标签栏一并去掉。
// 组件名保留 WikiPlanPanel 未改：改名会波及 ChatPage 的挂载点，属于另一件事。
export function WikiPlanPanel({ cwd, onFileSelect, selectedFile }: WikiPlanPanelProps) {
  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-auto">
        <WikiPanel cwd={cwd} onFileSelect={onFileSelect} selectedFile={selectedFile} />
      </div>
    </div>
  );
}

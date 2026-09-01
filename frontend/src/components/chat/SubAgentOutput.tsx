interface SubAgentOutputProps {
  id: string;
  type: string;
  output: string;
}

export function SubAgentOutput({ type, output }: SubAgentOutputProps) {
  return (
    <div className="mt-2 border border-purple-200 rounded-lg overflow-hidden bg-purple-50/30">
      <div className="flex items-center gap-2 px-3 py-1.5 bg-purple-100 border-b border-purple-200 text-xs text-purple-700 font-medium">
        <span>🤖</span>
        <span>子Agent: {type}</span>
      </div>
      <div className="h-64 overflow-y-auto p-3 text-xs font-mono whitespace-pre-wrap text-gray-700">
        {output}
      </div>
    </div>
  );
}

export default SubAgentOutput;

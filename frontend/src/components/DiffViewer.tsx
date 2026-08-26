interface DiffLine {
  type: 'added' | 'removed' | 'unchanged';
  oldLineNum: number | null;
  newLineNum: number | null;
  content: string;
}

function computeDiff(oldText: string, newText: string): DiffLine[] {
  const oldLines = oldText.split('\n');
  const newLines = newText.split('\n');
  
  // Simple LCS-based diff
  const m = oldLines.length;
  const n = newLines.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0));
  
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (oldLines[i - 1] === newLines[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }
  
  // Backtrack to find diff
  const result: DiffLine[] = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      result.unshift({
        type: 'unchanged',
        oldLineNum: i,
        newLineNum: j,
        content: oldLines[i - 1],
      });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      result.unshift({
        type: 'added',
        oldLineNum: null,
        newLineNum: j,
        content: newLines[j - 1],
      });
      j--;
    } else {
      result.unshift({
        type: 'removed',
        oldLineNum: i,
        newLineNum: null,
        content: oldLines[i - 1],
      });
      i--;
    }
  }
  
  return result;
}

interface DiffViewerProps {
  oldContent: string;
  newContent: string;
  maxHeight?: number;
}

export function DiffViewer({ oldContent, newContent, maxHeight = 400 }: DiffViewerProps) {
  const lines = computeDiff(oldContent, newContent);
  
  const addedCount = lines.filter(l => l.type === 'added').length;
  const removedCount = lines.filter(l => l.type === 'removed').length;
  
  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden text-xs font-mono">
      <div className="flex items-center gap-3 px-3 py-1.5 bg-gray-50 border-b border-gray-200 text-gray-600">
        <span>+{addedCount}</span>
        <span>-{removedCount}</span>
        <span>{lines.length} lines</span>
      </div>
      <div className="overflow-auto" style={{ maxHeight }}>
        <table className="w-full border-collapse">
          <tbody>
            {lines.map((line, idx) => {
              let bgClass = '';
              let prefix = ' ';
              if (line.type === 'added') {
                bgClass = 'bg-green-50';
                prefix = '+';
              } else if (line.type === 'removed') {
                bgClass = 'bg-red-50';
                prefix = '-';
              }
              return (
                <tr key={idx} className={bgClass}>
                  <td className="w-12 text-right pr-2 select-none text-gray-400 border-r border-gray-100">
                    {line.oldLineNum ?? ''}
                  </td>
                  <td className="w-12 text-right pr-2 select-none text-gray-400 border-r border-gray-100">
                    {line.newLineNum ?? ''}
                  </td>
                  <td className="w-5 text-center select-none text-gray-500">
                    {prefix}
                  </td>
                  <td className="px-2 whitespace-pre overflow-hidden text-ellipsis">
                    {line.content}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

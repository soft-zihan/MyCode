import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ThinkingBlock } from './ThinkingBlock';
import { ToolCollapsible } from './ToolCollapsible';
import { parseContentSections, splitToolSections } from './messageParser';

// 常见代码文件扩展名
const FILE_EXTENSIONS = [
  'py', 'ts', 'tsx', 'js', 'jsx', 'json', 'md', 'txt', 'html', 'css', 'scss', 'sass', 'less',
  'yaml', 'yml', 'toml', 'xml', 'sh', 'bash', 'zsh', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'hpp',
  'rb', 'php', 'swift', 'kt', 'scala', 'r', 'sql', 'graphql', 'proto', 'dockerfile', 'env',
  'gitignore', 'lock', 'cfg', 'ini', 'conf', 'log', 'csv', 'xlsx', 'pdf', 'doc', 'docx'
];

// 匹配文件路径的正则表达式
// 匹配反引号中的文件路径：`hello.py` 或 `src/components/Button.tsx`
const BACKTICK_FILE_REGEX = new RegExp(
  '`([^`]+\\.(' + FILE_EXTENSIONS.join('|') + '))`',
  'gi'
);

// 匹配独立的文件路径（不在反引号、链接或代码块中）
// 例如：hello.py 或 src/components/Button.tsx
const STANDALONE_FILE_REGEX = new RegExp(
  '(?<![`\\[\\/\\w])' + // 不在反引号、链接、URL、单词字符后
  '([\\w.-]+(?:\\/[\\w.-]+)*\\.(' + FILE_EXTENSIONS.join('|') + '))' +
  '(?![`\\]\\w])', // 不在反引号、链接、单词字符前
  'gi'
);

/**
 * 将文本中的文件路径转换为 markdown 链接
 */
function linkifyFilePaths(text: string): string {
  if (!text) return text;
  
  // 先处理反引号中的文件路径
  // `hello.py` -> [`hello.py`](hello.py)
  let result = text.replace(BACKTICK_FILE_REGEX, (match, path) => {
    // 检查是否已经是链接格式
    if (match.includes('](')) return match;
    return `[\`${path}\`](${path})`;
  });
  
  // 处理独立的文件路径（不在反引号中）
  // hello.py -> [hello.py](hello.py)
  result = result.replace(STANDALONE_FILE_REGEX, (match, path) => {
    // 检查是否已经在链接中
    const beforeMatch = result.substring(0, result.indexOf(match));
    if (beforeMatch.endsWith('](') || beforeMatch.endsWith('[')) {
      return match;
    }
    return `[${path}](${path})`;
  });
  
  return result;
}

interface MessageContentProps {
  content: string;
  isStreaming?: boolean;
  isLastMessage?: boolean;
  onFileClick?: (path: string) => void;
}

export function MessageContent({ 
  content, 
  isStreaming = false, 
  isLastMessage = false,
  onFileClick 
}: MessageContentProps) {
  const sections = parseContentSections(content);
  
  const mainContent = sections.filter(s => s.type === 'text').map(s => s.content).join('\n\n');
  const toolSections = mainContent ? splitToolSections(mainContent) : [];
  
  let lastToolIdx = -1;
  for (let i = toolSections.length - 1; i >= 0; i--) {
    if (toolSections[i].type === 'tool') { lastToolIdx = i; break; }
  }
  
  const fileLinkRenderer = (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
    const { href, children } = props;
    if (href && !href.startsWith('http') && !href.startsWith('#')) {
      return (
        <span 
          className="text-blue-600 hover:text-blue-800 underline cursor-pointer font-mono text-xs"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onFileClick?.(href);
          }}
        >
          {children}
        </span>
      );
    }
    return <a {...props} />;
  };
  
  return (
    <>
      {sections.map((section, sIdx) => {
        if (section.type === 'thinking') {
          const isThinkingStreaming = isStreaming && isLastMessage && !section.isComplete;
          return (
            <ThinkingBlock 
              key={`think-${sIdx}`}
              thinking={section.content} 
              isStreaming={isThinkingStreaming}
              isComplete={section.isComplete}
            />
          );
        }
        
        const textSections = splitToolSections(section.content);
        if (textSections.length === 0) return null;
        
        return (
          <div key={`text-${sIdx}`} className="prose prose-sm max-w-none">
            {textSections.map((ts, tIdx) => {
              if (ts.type === 'normal') {
                // 将文件路径转换为链接
                const linkedText = linkifyFilePaths(ts.text);
                return (
                  <ReactMarkdown key={tIdx} remarkPlugins={[remarkGfm]} components={{ a: fileLinkRenderer }}>
                    {linkedText}
                  </ReactMarkdown>
                );
              }
              const isLastTool = tIdx === lastToolIdx;
              const defaultOpen = isStreaming && isLastMessage && isLastTool;
              // 将文件路径转换为链接
              const linkedText = linkifyFilePaths(ts.text);
              return (
                <ToolCollapsible key={tIdx} label={ts.label || 'Tool'} icon={ts.icon || '🔧'} defaultOpen={defaultOpen}>
                  {linkedText ? (
                    <div className="prose prose-xs max-w-none">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: fileLinkRenderer }}>
                        {linkedText}
                      </ReactMarkdown>
                    </div>
                  ) : null}
                </ToolCollapsible>
              );
            })}
          </div>
        );
      })}
      
      {isStreaming && isLastMessage && !mainContent && sections.length === 0 && (
        <div className="flex items-center gap-2 text-gray-400 text-sm">
          <div className="flex gap-1">
            <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" />
            <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" style={{ animationDelay: '0.2s' }} />
            <div className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-pulse" style={{ animationDelay: '0.4s' }} />
          </div>
          <span>Thinking...</span>
        </div>
      )}
    </>
  );
}

export default MessageContent;

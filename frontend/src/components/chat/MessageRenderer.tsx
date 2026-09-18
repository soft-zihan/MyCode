import React, { useMemo } from 'react';
import ReactMarkdown, { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './CodeBlock';
import { Brain } from 'lucide-react';

const FILE_EXTENSIONS = [
  'py', 'ts', 'tsx', 'js', 'jsx', 'json', 'md', 'txt', 'html', 'css', 'scss', 'sass', 'less',
  'yaml', 'yml', 'toml', 'xml', 'sh', 'bash', 'zsh', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'hpp',
  'rb', 'php', 'swift', 'kt', 'scala', 'r', 'sql', 'graphql', 'proto', 'dockerfile', 'env',
  'gitignore', 'lock', 'cfg', 'ini', 'conf', 'log', 'csv', 'xlsx', 'pdf', 'doc', 'docx'
];

const BACKTICK_FILE_REGEX = new RegExp(
  '`([^`]+\\.(' + FILE_EXTENSIONS.join('|') + '))`',
  'gi'
);

const STANDALONE_FILE_REGEX = new RegExp(
  '(?<![`\\[\\/\\w])' +
  '([\\w.-]+(?:\\/[\\w.-]+)*\\.(' + FILE_EXTENSIONS.join('|') + '))' +
  '(?![`\\]\\w])',
  'gi'
);

function linkifyFilePaths(text: string): string {
  if (!text) return text;
  
  let result = text.replace(BACKTICK_FILE_REGEX, (match, path) => {
    if (match.includes('](')) return match;
    return `[${path}](${path})`;
  });
  
  result = result.replace(STANDALONE_FILE_REGEX, (match, path) => {
    const beforeMatch = result.substring(0, result.indexOf(match));
    if (beforeMatch.endsWith('](') || beforeMatch.endsWith('[')) {
      return match;
    }
    return `[${path}](${path})`;
  });
  
  return result;
}

interface MessageRendererProps {
  content: string;
  isStreaming?: boolean;
  onFileClick?: (path: string) => void;
  thinking?: string;
  showThinking?: boolean;
}

interface ThinkingBlockProps {
  content: string;
  isStreaming?: boolean;
}

const ThinkingBlock: React.FC<ThinkingBlockProps> = ({ content, isStreaming }) => {
  const [expanded, setExpanded] = React.useState(false);
  
  if (!content) return null;

  return (
    <div className="mb-3 rounded-lg border border-purple-200 dark:border-purple-800 overflow-hidden">
      <div
        className="flex items-center gap-2 px-3 py-2 bg-purple-50 dark:bg-purple-900/20 cursor-pointer hover:opacity-80 transition-opacity"
        onClick={() => setExpanded(!expanded)}
      >
        <Brain className="w-4 h-4 text-purple-500" />
        <span className="text-sm font-medium text-purple-700 dark:text-purple-300">
          Thinking
        </span>
        {isStreaming && (
          <span className="flex items-center gap-1">
            <span className="w-1.5 h-1.5 bg-purple-500 rounded-full animate-pulse" />
            <span className="text-xs text-purple-500">Processing...</span>
          </span>
        )}
        <span className="ml-auto text-xs text-purple-400">
          {expanded ? 'Hide' : 'Show'}
        </span>
      </div>
      {expanded && (
        <div className="px-3 py-2 border-t border-purple-200 dark:border-purple-800 bg-white dark:bg-gray-900">
          <div className="text-sm text-gray-600 dark:text-gray-400 whitespace-pre-wrap">
            {content}
          </div>
        </div>
      )}
    </div>
  );
};

export const MessageRenderer: React.FC<MessageRendererProps> = ({ 
  content, 
  isStreaming = false,
  onFileClick,
  thinking,
  showThinking = true,
}) => {
  const components: Components = useMemo(() => ({
    code({ node, className, children, ...props }) {
      const match = /language-(\w+)/.exec(className || '');
      const codeString = String(children).replace(/\n$/, '');
      const isBlock = match || node?.position;
      
      // Inline code (no language class and not in pre)
      if (!isBlock) {
        return (
          <code 
            className="px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 rounded text-sm font-mono text-pink-600 dark:text-pink-400"
            {...props}
          >
            {children}
          </code>
        );
      }
      
      // Code block - render as block element
      return (
        <div className="my-3">
          <CodeBlock
            code={codeString}
            language={match ? match[1] : undefined}
            className={className}
          />
        </div>
      );
    },
    a({ node, href, children, ...props }) {
      if (!href) {
        return <a {...props}>{children}</a>;
      }
      
      // Intercept file links like [filename](file:///path/to/file) or relative paths like [file.py](file.py)
      if (href.startsWith('file://') || (!href.startsWith('http') && !href.startsWith('#') && !href.startsWith('mailto:'))) {
        const filePath = href.startsWith('file://') ? href.replace('file://', '') : href;
        return (
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              onFileClick?.(filePath);
            }}
            className="text-blue-600 hover:text-blue-800 underline font-mono text-sm"
            title={filePath}
          >
            {children}
          </a>
        );
      }
      return (
        <a 
          href={href} 
          target="_blank" 
          rel="noopener noreferrer"
          className="text-blue-600 dark:text-blue-400 hover:underline"
          {...props}
        >
          {children}
        </a>
      );
    },
    pre({ children }) {
      // Let code component handle the rendering
      return <>{children}</>;
    },
    p({ children }) {
      // Check if paragraph contains only a block code element
      const childArray = React.Children.toArray(children);
      const hasOnlyBlockCode = childArray.length === 1 && 
        React.isValidElement(childArray[0]) && 
        childArray[0].type === 'div';
      
      // If it's just a code block, don't wrap in p
      if (hasOnlyBlockCode) {
        return <>{children}</>;
      }
      
      return <p>{children}</p>;
    },
    blockquote({ children }) {
      return (
        <blockquote className="border-l-4 border-gray-300 dark:border-gray-600 pl-4 my-2 text-gray-600 dark:text-gray-400 italic">
          {children}
        </blockquote>
      );
    },
    table({ children }) {
      return (
        <div className="overflow-x-auto my-3">
          <table className="min-w-full border-collapse border border-gray-300 dark:border-gray-600">
            {children}
          </table>
        </div>
      );
    },
    th({ children }) {
      return (
        <th className="border border-gray-300 dark:border-gray-600 px-3 py-1.5 bg-gray-100 dark:bg-gray-800 text-left text-sm font-medium">
          {children}
        </th>
      );
    },
    td({ children }) {
      return (
        <td className="border border-gray-300 dark:border-gray-600 px-3 py-1.5 text-sm">
          {children}
        </td>
      );
    },
  }), [onFileClick]);

  return (
    <div className="message-content">
      {/* Thinking block */}
      {showThinking && thinking && (
        <ThinkingBlock content={thinking} isStreaming={isStreaming} />
      )}
      
      {/* Main content */}
      <div className="prose prose-sm max-w-none text-gray-900 dark:text-gray-100 prose-headings:font-semibold prose-headings:text-gray-900 dark:prose-headings:text-gray-100 prose-p:leading-relaxed prose-a:text-blue-600 dark:prose-a:text-blue-400 prose-code:text-pink-600 dark:prose-code:text-pink-400">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={components}
        >
          {linkifyFilePaths(content)}
        </ReactMarkdown>
        {isStreaming && (
          <span className="inline-flex items-center ml-1">
            <span className="w-2 h-4 bg-blue-500 animate-pulse" />
          </span>
        )}
      </div>
    </div>
  );
};

export default MessageRenderer;

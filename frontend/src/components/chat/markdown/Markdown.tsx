import React, { useMemo } from 'react';
import ReactMarkdown, { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './CodeBlock';
import { linkifyFilePaths, isFileHref, normalizeFileHref } from './linkify';
import { getToken } from '../../../api/auth';

/** U9：artifacts 图片经 <img> 加载无法带 Authorization header → query token 回退 */
function resolveImageSrc(src: string | undefined): string | undefined {
  if (!src || !src.startsWith('/api/artifacts/')) return src;
  const token = getToken();
  if (!token) return src;
  return `${src}${src.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
}

export interface MarkdownProps {
  content: string;
  /** 点击文件链接回调；未提供时文件路径渲染为普通 inline code */
  onFileClick?: (path: string) => void;
  /** 流式输出中（显示光标） */
  isStreaming?: boolean;
  /** 是否将文本中的文件路径自动链接化（默认 true） */
  linkifyFiles?: boolean;
  /** 紧凑模式（子 Agent 输出等次要内容，字号更小） */
  compact?: boolean;
}

/**
 * 全站唯一 Markdown 渲染组件。
 *
 * 统一：文件路径链接化、代码块高亮（CodeBlock）、表格/引用样式、流式光标。
 * 消费方：AssistantNodeView、SubAgentNodeView、PlanApprovalDialog 等。
 */
export function Markdown({
  content,
  onFileClick,
  isStreaming = false,
  linkifyFiles = true,
  compact = false,
}: MarkdownProps) {
  const components = useMemo<Components>(() => ({
    code({ node, className, children, ...props }) {
      const match = /language-(\w+)/.exec(className || '');
      const codeString = String(children).replace(/\n$/, '');
      const isBlock = !!match || codeString.includes('\n');

      if (!isBlock) {
        return (
          <code
            className="px-1.5 py-0.5 bg-gray-100 rounded text-[0.85em] font-mono text-rose-600"
            {...props}
          >
            {children}
          </code>
        );
      }
      return <CodeBlock code={codeString} language={match ? match[1] : undefined} />;
    },
    pre({ children }) {
      // 代码块由 code 组件接管渲染
      return <>{children}</>;
    },
    a({ href, children, ...props }) {
      if (isFileHref(href)) {
        const filePath = normalizeFileHref(href!);
        if (onFileClick) {
          return (
            <span
              role="link"
              tabIndex={0}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onFileClick(filePath);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onFileClick(filePath);
              }}
              className="text-indigo-600 hover:text-indigo-800 underline decoration-indigo-300 underline-offset-2 cursor-pointer font-mono text-[0.85em]"
              title={filePath}
            >
              {children}
            </span>
          );
        }
        return (
          <code className="px-1.5 py-0.5 bg-gray-100 rounded text-[0.85em] font-mono text-gray-700">
            {filePath}
          </code>
        );
      }
      return (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="text-indigo-600 hover:underline"
          {...props}
        >
          {children}
        </a>
      );
    },
    img({ src, alt, ...props }) {
      const resolved = resolveImageSrc(typeof src === 'string' ? src : undefined);
      return (
        <img
          src={resolved}
          alt={alt ?? ''}
          className="max-w-full max-h-96 my-3 rounded-lg border border-gray-200 cursor-zoom-in"
          onClick={() => resolved && window.open(resolved, '_blank', 'noopener,noreferrer')}
          loading="lazy"
          {...props}
        />
      );
    },
    p({ children }) {
      const childArray = React.Children.toArray(children);
      const hasOnlyBlockCode =
        childArray.length === 1 &&
        React.isValidElement(childArray[0]) &&
        (childArray[0].type === CodeBlock || (childArray[0].type as React.ComponentType)?.name === 'CodeBlock');
      if (hasOnlyBlockCode) return <>{children}</>;
      return <p>{children}</p>;
    },
    blockquote({ children }) {
      return (
        <blockquote className="border-l-4 border-gray-200 pl-4 my-3 text-gray-500 not-italic">
          {children}
        </blockquote>
      );
    },
    table({ children }) {
      return (
        <div className="overflow-x-auto my-3 rounded-lg border border-gray-200">
          <table className="min-w-full border-collapse text-sm">{children}</table>
        </div>
      );
    },
    th({ children }) {
      return (
        <th className="border-b border-gray-200 px-3 py-2 bg-gray-50 text-left text-xs font-semibold text-gray-600">
          {children}
        </th>
      );
    },
    td({ children }) {
      return (
        <td className="border-b border-gray-100 px-3 py-2 text-gray-700">{children}</td>
      );
    },
  }), [onFileClick]);

  const text = linkifyFiles ? linkifyFilePaths(content) : content;

  return (
    <div
      className={
        'prose prose-sm max-w-none ' +
        'prose-headings:font-semibold prose-headings:text-gray-900 ' +
        'prose-p:leading-relaxed prose-p:my-2.5 ' +
        'prose-a:no-underline ' +
        'prose-strong:text-gray-900 ' +
        'prose-li:my-0.5 ' +
        'prose-code:before:content-none prose-code:after:content-none ' +
        (compact ? 'prose-sm text-[13px] ' : '')
      }
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
      {isStreaming && (
        <span className="inline-block w-2 h-4 ml-0.5 bg-indigo-500 animate-pulse align-text-bottom" />
      )}
    </div>
  );
}

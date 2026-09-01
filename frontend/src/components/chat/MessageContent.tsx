import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ThinkingBlock } from './ThinkingBlock';
import { ToolCollapsible } from './ToolCollapsible';
import { parseContentSections, splitToolSections } from './messageParser';

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
                return (
                  <ReactMarkdown key={tIdx} remarkPlugins={[remarkGfm]} components={{ a: fileLinkRenderer }}>
                    {ts.text}
                  </ReactMarkdown>
                );
              }
              const isLastTool = tIdx === lastToolIdx;
              const defaultOpen = isStreaming && isLastMessage && isLastTool;
              return (
                <ToolCollapsible key={tIdx} label={ts.label || 'Tool'} icon={ts.icon || '🔧'} defaultOpen={defaultOpen}>
                  {ts.text ? (
                    <div className="prose prose-xs max-w-none">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: fileLinkRenderer }}>
                        {ts.text}
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

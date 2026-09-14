export interface ContentSection {
  type: 'thinking' | 'text';
  content: string;
  isComplete: boolean;
}

export interface ToolSection {
  type: 'normal' | 'tool';
  text: string;
  label?: string;
  icon?: string;
}

export function parseContentSections(content: string): ContentSection[] {
  if (!content || typeof content !== 'string') {
    return [{ type: 'text', content: content || '', isComplete: true }];
  }
  
  const sections: ContentSection[] = [];
  let remaining = content;
  
  while (remaining.length > 0) {
    const openIdx = remaining.indexOf('<thinking>');
    if (openIdx === -1) {
      const text = remaining.trim();
      if (text) sections.push({ type: 'text', content: text, isComplete: true });
      break;
    }
    
    const beforeText = remaining.substring(0, openIdx).trim();
    if (beforeText) sections.push({ type: 'text', content: beforeText, isComplete: true });
    
    const closeIdx = remaining.indexOf('</thinking>', openIdx + 10);
    let thinkingContent: string;
    
    if (closeIdx === -1) {
      thinkingContent = remaining.substring(openIdx + 10).trim();
      sections.push({ type: 'thinking', content: thinkingContent, isComplete: false });
      remaining = '';
    } else {
      thinkingContent = remaining.substring(openIdx + 10, closeIdx).trim();
      sections.push({ type: 'thinking', content: thinkingContent, isComplete: true });
      remaining = remaining.substring(closeIdx + 11).trimStart();
    }
  }
  
  return sections.length > 0 ? sections : [{ type: 'text', content: '', isComplete: true }];
}

export function splitToolSections(content: string): ToolSection[] {
  const sections: ToolSection[] = [];

  const toolBlockRegex = /(?:^|\n\n):::tool-block\n([\s\S]*?)\n:::(?:\n\n|$)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = toolBlockRegex.exec(content)) !== null) {
    const normalText = content.substring(lastIndex, match.index).trim();
    if (normalText) {
      sections.push({ type: 'normal', text: normalText });
    }
    const blockContent = match[1].trim();
    const firstLine = blockContent.split('\n')[0] || '';
    const toolName = firstLine.replace(/^[📄📝✏️▶📂🔍🌐⏳🔧]\s*/, '').split(' ')[0] || 'tool';
    sections.push({ type: 'tool', text: blockContent, label: toolName, icon: '🔧' });
    lastIndex = match.index + match[0].length;
  }

  const toolRegex = /(?:^|\n\n)(\*\*(?:🔧|✅|[🤖]|✓|ℹ️)[^*]*\*\*(?:\s*\n(?:```\w*\n[\s\S]*?```|[\s\S]*?))?(?=\n\n|\n*$))/gu;

  while ((match = toolRegex.exec(content)) !== null) {
    const normalText = content.substring(lastIndex, match.index).trim();
    if (normalText) {
      sections.push({ type: 'normal', text: normalText });
    }

    const block = match[0].trim();
    const toolCallMatch = block.match(/^\*\*🔧[^*]*\*[^`]*`([^`]+)`/u);
    if (toolCallMatch) {
      const codeBlockMatch = block.match(/```[\s\S]*?```/);
      sections.push({ type: 'tool', text: codeBlockMatch ? codeBlockMatch[0] : '', label: `调用工具: ${toolCallMatch[1]}`, icon: '🔧' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    const toolResultMatch = block.match(/^\*\*✅[^*]*\*[^`]*`([^`]+)`/u);
    if (toolResultMatch) {
      const codeBlockMatch = block.match(/```[\s\S]*?```/);
      const result = codeBlockMatch ? codeBlockMatch[0] : '';
      const preview = result.length > 120 ? result.substring(0, 120) + '...' : result;
      sections.push({ type: 'tool', text: result, label: `工具结果: ${toolResultMatch[1]} — ${preview}`, icon: '✅' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    const subAgentStartMatch = block.match(/^\*\*🤖[^*]*\*[^`]*`([^`]+)`\s*-\s*(.*)/u);
    if (subAgentStartMatch) {
      sections.push({ type: 'tool', text: '', label: `启动子Agent: ${subAgentStartMatch[1]} — ${subAgentStartMatch[2]}`, icon: '🤖' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    const subAgentEndMatch = block.match(/^\*\*✓[^*]*\*[^`]*`([^`]+)`/u);
    if (subAgentEndMatch) {
      sections.push({ type: 'tool', text: '', label: `子Agent完成: ${subAgentEndMatch[1]}`, icon: '✓' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    const infoMatch = block.match(/^\*\*ℹ️[^*]*\*\*\s*(.*)/u);
    if (infoMatch) {
      sections.push({ type: 'tool', text: infoMatch[1], label: '信息', icon: 'ℹ️' });
      lastIndex = match.index + match[0].length;
      continue;
    }

    sections.push({ type: 'normal', text: block });
    lastIndex = match.index + match[0].length;
  }

  const remaining = content.substring(lastIndex).trim();
  if (remaining) {
    if (sections.length > 0 && sections[sections.length - 1].type === 'normal') {
      sections[sections.length - 1].text += '\n\n' + remaining;
    } else {
      sections.push({ type: 'normal', text: remaining });
    }
  }

  return sections;
}

export function simplifyToolResult(toolName: string, rawResult: string): string {
  if (!rawResult) return `**✅ 工具结果:** \`${toolName}\` — (empty)`;
  
  if (toolName === 'write_file') {
    const pathMatch = rawResult.match(/Successfully wrote to (.+?) \((\d+) lines\)/);
    if (pathMatch) {
      const filePath = pathMatch[1];
      const lineCount = pathMatch[2];
      const fileName = filePath.split('/').pop() || filePath;
      return `**✅ 工具结果:** 新建了 [${fileName}](${filePath}) (+${lineCount} lines)`;
    }
    return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${rawResult}\n\`\`\``;
  }
  
  if (toolName === 'edit_file') {
    const pathMatch = rawResult.match(/Successfully edited (.+?)(?:\s*\(matched via|$)/m);
    if (pathMatch) {
      const filePath = pathMatch[1].trim();
      const fileName = filePath.split('/').pop() || filePath;
      const addedLines = (rawResult.match(/^\+.*$/gm) || []).length;
      const removedLines = (rawResult.match(/^-.*$/gm) || []).length;
      const diffInfo = [];
      if (addedLines > 0) diffInfo.push(`+${addedLines}`);
      if (removedLines > 0) diffInfo.push(`-${removedLines}`);
      const summary = diffInfo.length > 0 ? ` (${diffInfo.join(' ')})` : '';
      return `**✅ 工具结果:** 编辑了 [${fileName}](${filePath})${summary}`;
    }
    return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${rawResult}\n\`\`\``;
  }
  
  if (toolName === 'read_file') {
    const lineMatch = rawResult.match(/\[Lines (\d+)-(\d+) of (\d+) total\]/);
    const numberedMatch = rawResult.match(/^(\d+)\s*\|/m);
    
    let lineInfo = '';
    if (lineMatch) {
      lineInfo = `(${lineMatch[1]}-${lineMatch[2]}/${lineMatch[3]} lines)`;
    } else if (numberedMatch) {
      const lines = rawResult.split('\n').filter(l => l.match(/^\d+\s*\|/));
      lineInfo = `(${lines.length} lines)`;
    }
    
    return `**✅ 工具结果:** \`${toolName}\` — 文件内容 ${lineInfo}\n\n<details><summary>点击查看完整内容</summary>\n\n\`\`\`\n${rawResult}\n\`\`\`\n\n</details>`;
  }
  
  if (toolName === 'outline_file') {
    const entries = rawResult.split('\n').filter(l => /^L\d+-\d+/.test(l));
    if (entries.length === 0) {
      return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${rawResult}\n\`\`\``;
    }
    return `**✅ 工具结果:** \`${toolName}\` — ${entries.length} 个结构元素\n\n\`\`\`\n${rawResult}\n\`\`\``;
  }

  if (toolName === 'grep_search') {
    const lines = rawResult.split('\n').filter(l => l.trim());
    if (rawResult === 'No matches found.') {
      return `**✅ 工具结果:** \`${toolName}\` — 无匹配结果`;
    }
    const matchCount = lines.length;
    const preview = lines.slice(0, 10).join('\n');
    const truncated = matchCount > 10 ? `\n\n... (共 ${matchCount} 条结果，显示前 10 条)` : '';
    return `**✅ 工具结果:** \`${toolName}\` — ${matchCount} 条匹配\n\n\`\`\`\n${preview}${truncated}\n\`\`\``;
  }
  
  if (toolName === 'list_files') {
    const files = rawResult.split('\n').filter(f => f.trim());
    return `**✅ 工具结果:** \`${toolName}\` — ${files.length} 个文件\n\n\`\`\`\n${rawResult}\n\`\`\``;
  }
  
  if (toolName === 'run_shell') {
    const preview = rawResult.length > 300 ? rawResult.substring(0, 300) + '...' : rawResult;
    return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${preview}\n\`\`\``;
  }
  
  const preview = rawResult.length > 500 ? rawResult.substring(0, 500) + '...' : rawResult;
  return `**✅ 工具结果:** \`${toolName}\`\n\n\`\`\`\n${preview}\n\`\`\``;
}

export function simplifyToolCall(toolName: string, input: Record<string, unknown>): string {
  if (toolName === 'read_file') {
    const p = String(input.file_path || '');
    const name = p.split('/').pop() || p;
    const offset = input.offset ? ` [${input.offset}+]` : '';
    return `📄 Read [${name}](${p})${offset}`;
  }
  if (toolName === 'outline_file') {
    const p = String(input.file_path || '');
    const name = p.split('/').pop() || p;
    return `🗂 Outline [${name}](${p})`;
  }
  if (toolName === 'write_file') {
    const p = String(input.file_path || '');
    const name = p.split('/').pop() || p;
    return `📝 Write [${name}](${p})`;
  }
  if (toolName === 'edit_file') {
    const p = String(input.file_path || '');
    const name = p.split('/').pop() || p;
    const replaceAll = input.replaceAll ? ' (all)' : '';
    return `✏️ Edit [${name}](${p})${replaceAll}`;
  }
  if (toolName === 'run_shell') {
    const cmd = String(input.command || '');
    const display = cmd.length > 60 ? cmd.substring(0, 60) + '...' : cmd;
    return `▶️ \`${display}\``;
  }
  if (toolName === 'list_files') {
    const p = input.path || '.';
    return `📂 List ${p}`;
  }
  if (toolName === 'grep_search') {
    return `🔍 Search \`${input.pattern}\``;
  }
  if (toolName === 'webfetch') {
    return `🌐 Fetch ${input.url}`;
  }
  
  return `🔧 ${toolName}`;
}

export function buildDisplayContent(
  contentParts: Array<{type: 'thinking' | 'text', content: string, complete?: boolean}>,
  accumulatedContent: string,
  currentThinking: string | null
): string {
  let result = '';
  
  for (const part of contentParts) {
    if (part.type === 'thinking') {
      result += `<thinking>${part.content}</thinking>\n\n`;
    } else {
      result += part.content;
    }
  }
  
  if (currentThinking !== null) {
    result += `<thinking>${currentThinking}`;
  }
  
  if (accumulatedContent) {
    if (currentThinking !== null) {
      result += `\n\n${accumulatedContent}`;
    } else {
      result += accumulatedContent;
    }
  }
  
  return result.trim();
}

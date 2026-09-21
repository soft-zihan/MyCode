/**
 * 文件路径链接化 — 全前端唯一实现。
 *
 * 把消息文本中的文件路径（`src/foo.py` 或裸路径）转换为 markdown 链接，
 * 供 Markdown 组件的 fileLink 渲染器拦截为可点击元素。
 */

export const FILE_EXTENSIONS = [
  'py', 'ts', 'tsx', 'js', 'jsx', 'json', 'md', 'txt', 'html', 'css', 'scss', 'sass', 'less',
  'yaml', 'yml', 'toml', 'xml', 'sh', 'bash', 'zsh', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'hpp',
  'rb', 'php', 'swift', 'kt', 'scala', 'r', 'sql', 'graphql', 'proto', 'dockerfile', 'env',
  'gitignore', 'lock', 'cfg', 'ini', 'conf', 'log', 'csv', 'xlsx', 'pdf', 'doc', 'docx',
];

const EXT_PATTERN = FILE_EXTENSIONS.join('|');

const BACKTICK_FILE_REGEX = new RegExp('`([^`]+\\.(' + EXT_PATTERN + '))`', 'gi');

const STANDALONE_FILE_REGEX = new RegExp(
  '(?<![`\\[\\/\\w])' +
  '([\\w.-]+(?:\\/[\\w.-]+)*\\.(' + EXT_PATTERN + '))' +
  '(?![`\\]\\w])',
  'gi'
);

export function linkifyFilePaths(text: string): string {
  if (!text) return text;

  let result = text.replace(BACKTICK_FILE_REGEX, (match, path) => {
    if (match.includes('](')) return match;
    return `[\`${path}\`](${path})`;
  });

  result = result.replace(STANDALONE_FILE_REGEX, (match, _path, _ext, offset: number, full: string) => {
    const before = full.substring(0, offset);
    if (before.endsWith('](') || before.endsWith('[')) return match;
    return `[${match}](${match})`;
  });

  return result;
}

/** 判断 href 是否为本地文件引用（非 http/mailto/anchor）。 */
export function isFileHref(href?: string): boolean {
  if (!href) return false;
  return href.startsWith('file://') ||
    (!href.startsWith('http') && !href.startsWith('#') && !href.startsWith('mailto:'));
}

export function normalizeFileHref(href: string): string {
  return href.startsWith('file://') ? href.replace('file://', '') : href;
}

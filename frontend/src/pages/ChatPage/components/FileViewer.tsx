import { useState, useEffect, useCallback } from 'react';
import { X, Save, Edit3, Eye, GitCommit } from 'lucide-react';
import Editor from '@monaco-editor/react';
import { fetchWorkspaceFile, writeWorkspaceFile } from '../../../api/client';

interface FileViewerProps {
  filePath: string;
  cwd?: string;
  onClose: () => void;
  readOnly?: boolean;
  editable?: boolean;
}

export function FileViewer({ filePath, cwd, onClose, readOnly = true, editable = false }: FileViewerProps) {
  const [content, setContent] = useState<string>('');
  const [editedContent, setEditedContent] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [isEditing, setIsEditing] = useState(!readOnly);
  const [hasChanges, setHasChanges] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [frontmatter, setFrontmatter] = useState<Record<string, any> | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    setFrontmatter(null);
    fetchWorkspaceFile(filePath, cwd)
      .then(data => {
        setContent(data.content);
        setEditedContent(data.content);
        if (data.frontmatter) {
          setFrontmatter(data.frontmatter);
        }
      })
      .catch(err => {
        // 如果文件不存在，显示空编辑器（允许创建新文件）
        if (err.message.includes('not found') || err.message.includes('404')) {
          setContent('');
          setEditedContent('');
          setError(null);
        } else {
          setError(err.message);
        }
      })
      .finally(() => setLoading(false));
  }, [filePath, cwd]);

  const handleSave = useCallback(async () => {
    if (!hasChanges) return;
    setSaving(true);
    setError(null);
    try {
      await writeWorkspaceFile(filePath, editedContent, cwd);
      setContent(editedContent);
      setHasChanges(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  }, [filePath, editedContent, cwd, hasChanges]);

  const handleEditorChange = useCallback((value: string | undefined) => {
    if (value !== undefined) {
      setEditedContent(value);
      setHasChanges(value !== content);
    }
  }, [content]);

  const handleCancel = useCallback(() => {
    setEditedContent(content);
    setHasChanges(false);
  }, [content]);

  const getLanguage = (path: string) => {
    const ext = path.split('.').pop()?.toLowerCase();
    const langMap: Record<string, string> = {
      'py': 'python',
      'js': 'javascript',
      'ts': 'typescript',
      'tsx': 'typescript',
      'jsx': 'javascript',
      'json': 'json',
      'md': 'markdown',
      'html': 'html',
      'css': 'css',
      'yaml': 'yaml',
      'yml': 'yaml',
      'sh': 'shell',
      'bash': 'shell',
    };
    return langMap[ext || ''] || 'plaintext';
  };

  const canEdit = editable && !loading;

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-gray-200 bg-gray-50">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <span className="text-sm font-medium text-gray-700 truncate">{filePath}</span>
          {hasChanges && (
            <span className="text-[10px] text-orange-500 bg-orange-50 px-1.5 py-0.5 rounded">
              未保存
            </span>
          )}
          {frontmatter?.checkpoint_id && (
            <span className="flex items-center gap-1 text-[10px] text-blue-600 bg-blue-50 px-1.5 py-0.5 rounded" title={`Checkpoint: ${frontmatter.checkpoint_id}`}>
              <GitCommit className="w-3 h-3" />
              {frontmatter.checkpoint_id.slice(-8)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {canEdit && (
            <>
              <button
                onClick={() => setIsEditing(!isEditing)}
                className={`p-1.5 rounded transition-colors ${
                  isEditing ? 'bg-blue-100 text-blue-600' : 'hover:bg-gray-200 text-gray-500'
                }`}
                title={isEditing ? '预览模式' : '编辑模式'}
              >
                {isEditing ? <Edit3 className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
              </button>
              {isEditing && (
                <>
                  {hasChanges && (
                    <button
                      onClick={handleCancel}
                      className="px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 rounded transition-colors"
                    >
                      取消
                    </button>
                  )}
                  <button
                    onClick={handleSave}
                    disabled={!hasChanges || saving}
                    className={`flex items-center gap-1 px-2 py-1 text-xs rounded transition-colors ${
                      hasChanges && !saving
                        ? 'bg-blue-500 text-white hover:bg-blue-600'
                        : 'bg-gray-200 text-gray-400 cursor-not-allowed'
                    }`}
                  >
                    <Save className="w-3 h-3" />
                    {saving ? '保存中...' : '保存'}
                  </button>
                </>
              )}
            </>
          )}
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-gray-200 rounded transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="px-4 py-2 bg-red-50 border-b border-red-100 text-xs text-red-600">
          {error}
        </div>
      )}

      {/* Editor */}
      <div className="flex-1 overflow-hidden">
        {loading ? (
          <div className="p-4 text-sm text-gray-500">Loading...</div>
        ) : (
          <Editor
            height="100%"
            language={getLanguage(filePath)}
            value={isEditing ? editedContent : content}
            onChange={isEditing ? handleEditorChange : undefined}
            theme="vs-light"
            options={{
              readOnly: !isEditing,
              minimap: { enabled: false },
              fontSize: 13,
              lineNumbers: 'on',
              scrollBeyondLastLine: false,
              wordWrap: 'on',
            }}
          />
        )}
      </div>
    </div>
  );
}

import React, { useEffect, useRef, useState, useImperativeHandle, forwardRef } from 'react';
import { useEditor, EditorContent, ReactRenderer } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import Placeholder from '@tiptap/extension-placeholder';
import Mention from '@tiptap/extension-mention';
import { File, Folder, Terminal } from 'lucide-react';
import './composer.css';

interface SuggestionItem {
  id: string;
  label: string;
  type: 'file' | 'folder' | 'command';
  description?: string;
}

interface ListProps {
  items: SuggestionItem[];
  command: (item: SuggestionItem) => void;
}

const SuggestionList = forwardRef<{ onKeyDown: (props: { event: KeyboardEvent }) => boolean }, ListProps>(
  ({ items, command }, ref) => {
    const [selectedIndex, setSelectedIndex] = useState(0);
    const listRef = useRef<HTMLDivElement>(null);

    useEffect(() => setSelectedIndex(0), [items]);

    useEffect(() => {
      const el = listRef.current?.querySelector(`[data-index="${selectedIndex}"]`);
      el?.scrollIntoView({ block: 'nearest' });
    }, [selectedIndex]);

    useImperativeHandle(ref, () => ({
      onKeyDown: ({ event }: { event: KeyboardEvent }) => {
        switch (event.key) {
          case 'ArrowUp':
            event.preventDefault();
            setSelectedIndex(i => Math.max(i - 1, 0));
            return true;
          case 'ArrowDown':
            event.preventDefault();
            setSelectedIndex(i => Math.min(i + 1, items.length - 1));
            return true;
          case 'Enter':
          case 'Tab':
            event.preventDefault();
            if (items[selectedIndex]) command(items[selectedIndex]);
            return true;
          case 'Escape':
            event.preventDefault();
            return true;
        }
        return false;
      },
    }));

    if (items.length === 0) return null;

    const getIcon = (item: SuggestionItem) => {
      switch (item.type) {
        case 'file': return <File className="w-3.5 h-3.5 text-blue-500 flex-shrink-0" />;
        case 'folder': return <Folder className="w-3.5 h-3.5 text-yellow-500 flex-shrink-0" />;
        case 'command': return <Terminal className="w-3.5 h-3.5 text-green-500 flex-shrink-0" />;
      }
    };

    return (
      <div ref={listRef} className="composer-suggestion-popup" role="listbox">
        {items.map((item, index) => (
          <button
            key={item.id}
            data-index={index}
            onClick={() => command(item)}
            className={`composer-suggestion-item ${index === selectedIndex ? 'selected' : ''}`}
            role="option"
            aria-selected={index === selectedIndex}
          >
            {getIcon(item)}
            <div className="composer-suggestion-content">
              <span className="composer-suggestion-label font-mono text-xs">{item.label}</span>
              {item.description && (
                <span className="composer-suggestion-desc text-[10px] text-gray-400">{item.description}</span>
              )}
            </div>
          </button>
        ))}
      </div>
    );
  }
);

SuggestionList.displayName = 'SuggestionList';

const SLASH_COMMANDS: SuggestionItem[] = [
  // Session commands
  { id: 'sessions', label: 'sessions', type: 'command', description: 'List, delete, or clean sessions' },
  { id: 'switch', label: 'switch', type: 'command', description: 'Switch to another session' },
  { id: 'resume', label: 'resume', type: 'command', description: 'Resume a session' },
  { id: 'clear', label: 'clear', type: 'command', description: 'Clear conversation history' },
  { id: 'compact', label: 'compact', type: 'command', description: 'Compact conversation context' },
  { id: 'rewind', label: 'rewind', type: 'command', description: 'Rewind N turns of conversation' },
  { id: 'fork', label: 'fork', type: 'command', description: 'Fork current session' },
  { id: 'subagents', label: 'subagents', type: 'command', description: 'View subagent task progress' },
  { id: 'new', label: 'new', type: 'command', description: 'Start new session' },
  { id: 'undo', label: 'undo', type: 'command', description: 'Undo last turn' },
  { id: 'rename', label: 'rename', type: 'command', description: 'Rename current session' },
  { id: 'export', label: 'export', type: 'command', description: 'Export session transcript' },
  { id: 'quit', label: 'quit', type: 'command', description: 'Exit the session' },
  // Inspect commands
  { id: 'context', label: 'context', type: 'command', description: 'Show context visualization' },
  { id: 'ctx', label: 'ctx', type: 'command', description: 'Delete or keep context messages' },
  { id: 'memory', label: 'memory', type: 'command', description: 'List memories or prune' },
  { id: 'skills', label: 'skills', type: 'command', description: 'List available skills' },
  { id: 'skill-stats', label: 'skill-stats', type: 'command', description: 'Show skill usage statistics' },
  { id: 'trace', label: 'trace', type: 'command', description: 'View or toggle trace logging' },
  // Config commands
  { id: 'cd', label: 'cd', type: 'command', description: 'Change working directory' },
  { id: 'thinking', label: 'thinking', type: 'command', description: 'Toggle thinking display' },
  { id: 'plan', label: 'plan', type: 'command', description: 'Toggle plan mode' },
  { id: 'cost', label: 'cost', type: 'command', description: 'Show cost information' },
  { id: 'help', label: 'help', type: 'command', description: 'Show help message' },
  { id: 'models', label: 'models', type: 'command', description: 'List or switch models' },
  { id: 'status', label: 'status', type: 'command', description: 'Show current status' },
  { id: 'permission', label: 'permission', type: 'command', description: 'Switch permission mode' },
  { id: 'yolo', label: 'yolo', type: 'command', description: 'Toggle bypass permissions' },
  { id: 'palette', label: 'palette', type: 'command', description: 'Open command palette' },
  // Agent commands
  { id: 'goal', label: 'goal', type: 'command', description: 'Autonomous goal mode' },
];

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  files: string[];
  placeholder?: string;
  onSlashCommand?: (command: string) => void;
}

export const Composer: React.FC<ComposerProps> = ({
  value,
  onChange,
  onSubmit,
  files,
  placeholder = 'Type a message... (@ files, / commands, Shift+Enter for newline)',
  onSlashCommand,
}) => {
  const fileItemsRef = useRef<SuggestionItem[]>([]);
  
  useEffect(() => {
    fileItemsRef.current = files.slice(0, 200).map(f => ({
      id: f,
      label: f,
      type: (f.includes('/') ? 'file' : 'folder') as 'file' | 'folder',
    }));
  }, [files]);

  const editor = useEditor({
    extensions: [
      StarterKit,
      Placeholder.configure({ placeholder }),
      Mention.configure({
        HTMLAttributes: { class: 'composer-mention' },
        suggestion: {
          char: '@',
          placement: 'top-start',
          flip: false,
          items: ({ query }) => {
            const q = query.toLowerCase();
            return fileItemsRef.current.filter(f => f.label.toLowerCase().includes(q)).slice(0, 10);
          },
          render: () => {
            let component: ReactRenderer | null = null;
            let unmount: (() => void) | null = null;

            return {
              onStart: (props: any) => {
                component = new ReactRenderer(SuggestionList, {
                  props: { items: props.items, command: (item: SuggestionItem) => props.command(item) },
                  editor: props.editor,
                });
                unmount = props.mount(component.element, {
                  onPosition: (data) => {
                    const el = component?.element as HTMLElement;
                    if (el) {
                      // Position popup so bottom edge aligns with top of composer container
                      const composerEl = el.closest('.composer-container') as HTMLElement;
                      if (composerEl) {
                        const composerRect = composerEl.getBoundingClientRect();
                        const popupHeight = el.offsetHeight || 200;
                        el.style.position = 'fixed';
                        el.style.left = `${composerRect.left}px`;
                        el.style.top = `${composerRect.top - popupHeight - 8}px`;
                        el.style.width = `${composerRect.width}px`;
                      } else {
                        // Fallback to cursor-based positioning
                        const popupHeight = el.offsetHeight || 200;
                        const offsetY = 8;
                        el.style.position = data.strategy;
                        el.style.left = `${data.x}px`;
                        el.style.top = `${data.y - popupHeight - offsetY}px`;
                      }
                    }
                  },
                });
              },
              onUpdate: (props: any) => {
                component?.updateProps({
                  items: props.items,
                  command: (item: SuggestionItem) => props.command(item),
                });
              },
              onKeyDown: (props: any) => {
                if (props.event.key === 'Escape') {
                  unmount?.();
                  component?.destroy();
                  return true;
                }
                return (component?.ref as any)?.onKeyDown(props) ?? false;
              },
              onExit: () => {
                unmount?.();
                component?.destroy();
              },
            };
          },
        },
      }),
      Mention.configure({
        HTMLAttributes: { class: 'composer-command' },
        suggestion: {
          char: '/',
          placement: 'top-start',
          flip: false,
          items: ({ query }) => {
            const q = query.toLowerCase();
            return SLASH_COMMANDS.filter(c => c.label.toLowerCase().includes(q));
          },
          render: () => {
            let component: ReactRenderer | null = null;
            let unmount: (() => void) | null = null;

            return {
              onStart: (props: any) => {
                component = new ReactRenderer(SuggestionList, {
                  props: { items: props.items, command: (item: SuggestionItem) => props.command(item) },
                  editor: props.editor,
                });
                unmount = props.mount(component.element, {
                  onPosition: (data) => {
                    const el = component?.element as HTMLElement;
                    if (el) {
                      // Position popup so bottom edge aligns with top of composer container
                      const composerEl = el.closest('.composer-container') as HTMLElement;
                      if (composerEl) {
                        const composerRect = composerEl.getBoundingClientRect();
                        const popupHeight = el.offsetHeight || 200;
                        el.style.position = 'fixed';
                        el.style.left = `${composerRect.left}px`;
                        el.style.top = `${composerRect.top - popupHeight - 8}px`;
                        el.style.width = `${composerRect.width}px`;
                      } else {
                        // Fallback to cursor-based positioning
                        const popupHeight = el.offsetHeight || 200;
                        const offsetY = 8;
                        el.style.position = data.strategy;
                        el.style.left = `${data.x}px`;
                        el.style.top = `${data.y - popupHeight - offsetY}px`;
                      }
                    }
                  },
                });
              },
              onUpdate: (props: any) => {
                component?.updateProps({
                  items: props.items,
                  command: (item: SuggestionItem) => props.command(item),
                });
              },
              onKeyDown: (props: any) => {
                if (props.event.key === 'Escape') {
                  unmount?.();
                  component?.destroy();
                  return true;
                }
                return (component?.ref as any)?.onKeyDown(props) ?? false;
              },
              onExit: () => {
                unmount?.();
                component?.destroy();
              },
            };
          },
        },
      }),
    ],
    editorProps: {
      attributes: {
        class: 'composer-editor',
      },
      handleKeyDown: (_view, event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          onSubmit();
          editor?.commands.clearContent();
          return true;
        }
        return false;
      },
    },
    onUpdate: ({ editor }) => {
      const text = editor.getText();
      if (text !== value) onChange(text);
    },
  });

  useEffect(() => {
    if (editor && value === '' && editor.getText() !== '') {
      editor.commands.clearContent();
    }
  }, [value]);

  return (
    <div className="composer-container relative">
      <EditorContent editor={editor} />
    </div>
  );
};

export default Composer;

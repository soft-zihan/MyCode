import React, { useState, useEffect, useRef, useMemo } from 'react';
import { File, Folder, Hash } from 'lucide-react';

interface MentionItem {
  id: string;
  label: string;
  type: 'file' | 'folder' | 'symbol';
  path?: string;
}

interface MentionMenuProps {
  items: MentionItem[];
  selectedIndex: number;
  onSelect: (item: MentionItem) => void;
  position: { top: number; left: number };
}

export const MentionMenu: React.FC<MentionMenuProps> = ({
  items,
  selectedIndex,
  onSelect,
  position,
}) => {
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const selectedEl = menuRef.current?.querySelector(`[data-index="${selectedIndex}"]`);
    selectedEl?.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  if (items.length === 0) return null;

  const getIcon = (type: MentionItem['type']) => {
    switch (type) {
      case 'file':
        return <File className="w-4 h-4 text-blue-500" />;
      case 'folder':
        return <Folder className="w-4 h-4 text-yellow-500" />;
      case 'symbol':
        return <Hash className="w-4 h-4 text-purple-500" />;
    }
  };

  return (
    <div
      ref={menuRef}
      className="absolute z-50 bg-white border border-gray-200 rounded-lg shadow-lg max-h-60 overflow-y-auto min-w-[200px]"
      style={{ bottom: '100%', left: position.left, marginBottom: '4px' }}
      role="listbox"
    >
      {items.map((item, index) => (
        <button
          key={item.id}
          data-index={index}
          onClick={() => onSelect(item)}
          className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm transition-colors ${
            index === selectedIndex
              ? 'bg-blue-50 text-blue-700'
              : 'text-gray-700 hover:bg-gray-50'
          }`}
          role="option"
          aria-selected={index === selectedIndex}
        >
          {getIcon(item.type)}
          <span className="truncate font-mono text-xs">{item.label}</span>
        </button>
      ))}
    </div>
  );
};

interface UseMentionOptions {
  files: string[];
  onInsert: (text: string) => void;
}

export const useMention = ({ files, onInsert }: UseMentionOptions) => {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const filteredItems: MentionItem[] = useMemo(() => {
    if (!query) return [];
    
    const lowerQuery = query.toLowerCase();
    return files
      .filter(f => f.toLowerCase().includes(lowerQuery))
      .slice(0, 10)
      .map(f => ({
        id: f,
        label: f,
        type: f.includes('/') ? 'file' : 'folder',
        path: f,
      }));
  }, [files, query]);

  useEffect(() => {
    setSelectedIndex(0);
  }, [filteredItems]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (!isOpen) return;

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        setSelectedIndex(i => Math.min(i + 1, filteredItems.length - 1));
        break;
      case 'ArrowUp':
        e.preventDefault();
        setSelectedIndex(i => Math.max(i - 1, 0));
        break;
      case 'Enter':
      case 'Tab':
        if (filteredItems.length > 0) {
          e.preventDefault();
          handleSelect(filteredItems[selectedIndex]);
        }
        break;
      case 'Escape':
        e.preventDefault();
        setIsOpen(false);
        break;
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    const cursorPos = e.target.selectionStart;
    
    const textBeforeCursor = value.slice(0, cursorPos);
    const atIndex = textBeforeCursor.lastIndexOf('@');
    
    if (atIndex >= 0) {
      const queryText = textBeforeCursor.slice(atIndex + 1);
      if (!queryText.includes(' ') && !queryText.includes('\n')) {
        setQuery(queryText);
        setIsOpen(true);
        
        const textarea = e.target;
        const coords = getCaretCoordinates(textarea, cursorPos);
        setPosition({ top: coords.top, left: coords.left });
        return;
      }
    }
    
    setIsOpen(false);
    setQuery('');
  };

  const handleSelect = (item: MentionItem) => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    
    const cursorPos = textarea.selectionStart;
    const value = textarea.value;
    const textBeforeCursor = value.slice(0, cursorPos);
    const atIndex = textBeforeCursor.lastIndexOf('@');
    
    if (atIndex >= 0) {
      const newValue = value.slice(0, atIndex) + `@${item.label} ` + value.slice(cursorPos);
      onInsert(newValue);
    }
    
    setIsOpen(false);
    setQuery('');
  };

  return {
    isOpen,
    items: filteredItems,
    selectedIndex,
    position,
    handleKeyDown,
    handleChange,
    handleSelect,
    textareaRef,
  };
};

function getCaretCoordinates(element: HTMLTextAreaElement, position: number) {
  const div = document.createElement('div');
  const computedStyle = window.getComputedStyle(element);
  
  const styles = [
    'fontFamily', 'fontSize', 'fontWeight', 'fontStyle',
    'letterSpacing', 'textTransform', 'wordSpacing',
    'paddingLeft', 'paddingRight', 'paddingTop', 'paddingBottom',
    'borderLeftWidth', 'borderRightWidth',
    'lineHeight', 'whiteSpace', 'wordWrap',
    'boxSizing', 'textAlign'
  ] as const;
  
  styles.forEach(prop => {
    (div.style as any)[prop] = (computedStyle as any)[prop];
  });
  
  div.style.position = 'absolute';
  div.style.top = '-9999px';
  div.style.left = '-9999px';
  div.style.width = `${element.offsetWidth}px`;
  div.style.height = 'auto';
  div.style.visibility = 'hidden';
  
  const text = element.value.substring(0, position);
  div.textContent = text;
  
  const span = document.createElement('span');
  span.textContent = element.value.substring(position) || '.';
  div.appendChild(span);
  
  document.body.appendChild(div);
  
  const rect = element.getBoundingClientRect();
  const spanRect = span.getBoundingClientRect();
  
  document.body.removeChild(div);
  
  return {
    top: spanRect.top - rect.top,
    left: spanRect.left - rect.left,
  };
}

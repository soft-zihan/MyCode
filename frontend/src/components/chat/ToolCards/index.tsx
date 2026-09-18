import React from 'react';
import type { ToolCallEvent } from '../../../hooks';
import { BashCard } from './BashCard';
import { FileCard } from './FileCard';
import { SearchCard } from './SearchCard';
import { DefaultCard } from './DefaultCard';
import './toolcards.css';

interface ToolCardRouterProps {
  call: ToolCallEvent;
}

const getToolCategory = (name: string): 'bash' | 'file' | 'search' | 'default' => {
  if (name.includes('shell') || name.includes('bash') || name.includes('run_shell')) return 'bash';
  if (name.includes('read') || name.includes('write') || name.includes('edit') || name === 'file_search') return 'file';
  if (name.includes('search') || name.includes('grep') || name.includes('glob')) return 'search';
  return 'default';
};

export const ToolCardRouter: React.FC<ToolCardRouterProps> = ({ call }) => {
  const category = getToolCategory(call.name);

  switch (category) {
    case 'bash':
      return <BashCard call={call} />;
    case 'file':
      return <FileCard call={call} />;
    case 'search':
      return <SearchCard call={call} />;
    default:
      return <DefaultCard call={call} />;
  }
};

export { BashCard } from './BashCard';
export { FileCard } from './FileCard';
export { SearchCard } from './SearchCard';
export { DefaultCard } from './DefaultCard';

import React from 'react';
import { CheckCircle2, Circle, Clock, XCircle, ListTodo } from 'lucide-react';

export interface TodoItem {
  id: number;
  content: string;
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled';
  priority: 'high' | 'medium' | 'low';
  created_at: string;
  updated_at: string;
}

interface TodoListPanelProps {
  todos: TodoItem[];
}

const getStatusIcon = (status: string) => {
  switch (status) {
    case 'completed':
      return <CheckCircle2 className="w-4 h-4 text-green-500" />;
    case 'in_progress':
      return <Clock className="w-4 h-4 text-blue-500 animate-pulse" />;
    case 'cancelled':
      return <XCircle className="w-4 h-4 text-gray-400" />;
    default:
      return <Circle className="w-4 h-4 text-gray-300" />;
  }
};

const getPriorityColor = (priority: string) => {
  switch (priority) {
    case 'high':
      return 'text-red-500';
    case 'medium':
      return 'text-yellow-500';
    case 'low':
      return 'text-green-500';
    default:
      return 'text-gray-400';
  }
};

const getStatusColor = (status: string) => {
  switch (status) {
    case 'completed':
      return 'text-gray-400 line-through';
    case 'cancelled':
      return 'text-gray-400 line-through';
    case 'in_progress':
      return 'text-blue-700 dark:text-blue-300 font-medium';
    default:
      return 'text-gray-700 dark:text-gray-300';
  }
};

export const TodoListPanel: React.FC<TodoListPanelProps> = ({ todos }) => {
  if (todos.length === 0) {
    return null;
  }

  const pending = todos.filter(t => t.status === 'pending').length;
  const inProgress = todos.filter(t => t.status === 'in_progress').length;
  const completed = todos.filter(t => t.status === 'completed').length;

  return (
    <div className="mx-4 mb-2">
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-sm overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-2 bg-gray-50 dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
          <ListTodo className="w-4 h-4 text-gray-500" />
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
            任务清单
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400 ml-auto">
            {completed}/{todos.length} 完成
            {inProgress > 0 && ` · ${inProgress} 进行中`}
          </span>
        </div>

        <div className="divide-y divide-gray-100 dark:divide-gray-800">
          {todos.map((todo) => (
            <div
              key={todo.id}
              className="flex items-start gap-2 px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-800/50"
            >
              {getStatusIcon(todo.status)}
              <div className="flex-1 min-w-0">
                <p className={`text-sm ${getStatusColor(todo.status)}`}>
                  {todo.content}
                </p>
              </div>
              <span className={`text-xs ${getPriorityColor(todo.priority)}`}>
                {todo.priority === 'high' ? '●' : todo.priority === 'medium' ? '○' : '◌'}
              </span>
            </div>
          ))}
        </div>

        {(pending > 0 || inProgress > 0) && (
          <div className="px-3 py-1.5 bg-gray-50 dark:bg-gray-800 border-t border-gray-100 dark:border-gray-700">
            <div className="h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
              <div
                className="h-full bg-green-500 transition-all duration-300"
                style={{ width: `${(completed / todos.length) * 100}%` }}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default TodoListPanel;

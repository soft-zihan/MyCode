import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAgentEvents, SessionEvent } from '../hooks/useAgentEvents';

function makeEvent(overrides: Partial<SessionEvent>): SessionEvent {
  return {
    seq: 0,
    type: 'turn/start',
    time: Date.now(),
    session_id: 'test-session',
    ...overrides,
  };
}

describe('useAgentEvents', () => {
  it('initializes with default state', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    expect(result.current.state.isStreaming).toBe(false);
    expect(result.current.state.stats).toBeNull();
    expect(result.current.state.toolCalls.size).toBe(0);
    expect(result.current.state.subAgents.size).toBe(0);
    expect(result.current.state.permissionRequest).toBeNull();
  });

  it('turn/start sets isStreaming true', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({ type: 'turn/start' }));
    });
    expect(result.current.state.isStreaming).toBe(true);
  });

  it('turn/end sets isStreaming false', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({ type: 'turn/start' }));
    });
    expect(result.current.state.isStreaming).toBe(true);
    act(() => {
      result.current.handleSessionEvent(makeEvent({ type: 'turn/end' }));
    });
    expect(result.current.state.isStreaming).toBe(false);
  });

  it('tool_call adds to toolCalls map', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'tool_call',
        call_id: 'call-1',
        name: 'read_file',
        input: { file_path: 'test.py' },
      }));
    });
    expect(result.current.state.toolCalls.size).toBe(1);
    const tc = result.current.state.toolCalls.get('call-1');
    expect(tc?.name).toBe('read_file');
    expect(tc?.status).toBe('pending');
  });

  it('tool_result updates existing tool call', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'tool_call',
        call_id: 'call-1',
        name: 'read_file',
        input: {},
      }));
    });
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'tool_result',
        call_id: 'call-1',
        result: 'file contents',
        status: 'success',
      }));
    });
    const tc = result.current.state.toolCalls.get('call-1');
    expect(tc?.status).toBe('success');
    expect(tc?.result).toBe('file contents');
  });

  it('sub_agent/start creates sub-agent entry', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'sub_agent/start',
        agent_id: 'sub-1',
        agent_type: 'explore',
        description: 'exploring codebase',
      }));
    });
    expect(result.current.state.subAgents.size).toBe(1);
    const sa = result.current.state.subAgents.get('sub-1');
    expect(sa?.agent_type).toBe('explore');
    expect(sa?.status).toBe('running');
  });

  it('sub_agent/end updates sub-agent status', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'sub_agent/start',
        agent_id: 'sub-1',
        agent_type: 'explore',
        description: 'exploring',
      }));
    });
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'sub_agent/end',
        agent_id: 'sub-1',
        status: 'completed',
        summary: 'done',
      }));
    });
    const sa = result.current.state.subAgents.get('sub-1');
    expect(sa?.status).toBe('completed');
    expect(sa?.summary).toBe('done');
  });

  it('thinking event appends to sub-agent thinking', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'sub_agent/start',
        agent_id: 'sub-1',
        agent_type: 'explore',
        description: 'exploring',
      }));
    });
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'thinking',
        content: 'analyzing...',
        sub_agent_id: 'sub-1',
      }));
    });
    const sa = result.current.state.subAgents.get('sub-1');
    expect(sa?.thinking).toBe('analyzing...');
  });

  it('text event appends to sub-agent text', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'sub_agent/start',
        agent_id: 'sub-1',
        agent_type: 'general',
        description: 'task',
      }));
    });
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'text',
        content: 'part1 ',
        sub_agent_id: 'sub-1',
      }));
    });
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'text',
        content: 'part2',
        sub_agent_id: 'sub-1',
      }));
    });
    const sa = result.current.state.subAgents.get('sub-1');
    expect(sa?.text).toBe('part1 part2');
  });

  it('tool_call with sub_agent_id goes to sub-agent tool_calls', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'sub_agent/start',
        agent_id: 'sub-1',
        agent_type: 'explore',
        description: 'exploring',
      }));
    });
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'tool_call',
        call_id: 'call-sub-1',
        name: 'grep_search',
        input: { pattern: 'test' },
        sub_agent_id: 'sub-1',
      }));
    });
    const sa = result.current.state.subAgents.get('sub-1');
    expect(sa?.tool_calls.length).toBe(1);
    expect(sa?.tool_calls[0].name).toBe('grep_search');
    expect(result.current.state.toolCalls.size).toBe(0);
  });

  it('stats event updates stats', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'stats',
        input_tokens: 1000,
        output_tokens: 500,
        context_window: 128000,
      }));
    });
    expect(result.current.state.stats?.input_tokens).toBe(1000);
    expect(result.current.state.stats?.output_tokens).toBe(500);
  });

  it('permission/request sets permissionRequest', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({
        type: 'permission/request',
        rpc_id: 'rpc-1',
        request_id: 'req-1',
        command: 'rm -rf /',
        tool_name: 'run_shell',
        message: 'Dangerous command',
      }));
    });
    expect(result.current.state.permissionRequest).not.toBeNull();
    expect(result.current.state.permissionRequest?.command).toBe('rm -rf /');
  });

  it('resetState clears all state', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({ type: 'turn/start' }));
      result.current.handleSessionEvent(makeEvent({
        type: 'tool_call',
        call_id: 'c1',
        name: 'read_file',
        input: {},
      }));
    });
    expect(result.current.state.isStreaming).toBe(true);
    expect(result.current.state.toolCalls.size).toBe(1);

    act(() => {
      result.current.resetState();
    });
    expect(result.current.state.isStreaming).toBe(false);
    expect(result.current.state.toolCalls.size).toBe(0);
  });

  it('lastSeq tracks maximum seq', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    act(() => {
      result.current.handleSessionEvent(makeEvent({ seq: 5, type: 'turn/start' }));
    });
    expect(result.current.state.lastSeq).toBe(5);
    act(() => {
      result.current.handleSessionEvent(makeEvent({ seq: 3, type: 'text', content: 'x' }));
    });
    expect(result.current.state.lastSeq).toBe(5);
    act(() => {
      result.current.handleSessionEvent(makeEvent({ seq: 10, type: 'turn/end' }));
    });
    expect(result.current.state.lastSeq).toBe(10);
  });

  it('handleSSEEvent parses JSON and dispatches', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    const messageEvent = new MessageEvent('message', {
      data: JSON.stringify({ seq: 1, type: 'turn/start', time: Date.now(), session_id: 'test' }),
    });
    act(() => {
      result.current.handleSSEEvent(messageEvent);
    });
    expect(result.current.state.isStreaming).toBe(true);
  });

  it('handleSSEEvent handles invalid JSON gracefully', () => {
    const { result } = renderHook(() => useAgentEvents('test-session'));
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const messageEvent = new MessageEvent('message', { data: 'not json' });
    act(() => {
      result.current.handleSSEEvent(messageEvent);
    });
    expect(consoleSpy).toHaveBeenCalled();
    expect(result.current.state.isStreaming).toBe(false);
    consoleSpy.mockRestore();
  });
});

/**
 * U9：Markdown 内联图片——artifacts 通道 URL 追加 query token（<img> 无法带 header），
 * 非 artifacts 图片不受影响。
 */

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { Markdown } from '../components/chat/markdown/Markdown';
import { setToken, clearToken } from '../api/auth';

describe('Markdown img', () => {
  afterEach(() => {
    clearToken();
    cleanup();
  });

  it('artifacts 图片带 token query（鉴权启用时）', () => {
    setToken('s3cret');
    render(<Markdown content={'![chart](/api/artifacts/s1/chart.png)'} />);
    const img = screen.getByAltText('chart');
    expect(img.getAttribute('src')).toBe('/api/artifacts/s1/chart.png?token=s3cret');
  });

  it('无 token 时保持原始相对 URL', () => {
    render(<Markdown content={'![chart](/api/artifacts/s1/chart.png)'} />);
    const img = screen.getByAltText('chart');
    expect(img.getAttribute('src')).toBe('/api/artifacts/s1/chart.png');
  });

  it('外链图片 URL 不动', () => {
    setToken('s3cret');
    render(<Markdown content={'![logo](https://example.com/logo.png)'} />);
    const img = screen.getByAltText('logo');
    expect(img.getAttribute('src')).toBe('https://example.com/logo.png');
  });
});

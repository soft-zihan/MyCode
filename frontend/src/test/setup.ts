import '@testing-library/jest-dom'

// Node 26 起 globalThis 自带实验性 localStorage 访问器（未提供 --localstorage-file
// 时恒为 undefined），vitest 的 jsdom 环境因此跳过注入真实现——window.localStorage
// 为 undefined。此处仅为测试环境补一个标准 Storage 行为的最小实现（生产代码运行
// 在真实浏览器，不受影响）。
function installTestLocalStorage(target: Record<string | symbol, unknown> & { localStorage?: unknown }) {
  if (target.localStorage !== undefined) return;
  const store = new Map<string, string>();
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: unknown) => { store.set(k, String(v)); },
    removeItem: (k: string) => { store.delete(k); },
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() { return store.size; },
  };
  Object.defineProperty(target, 'localStorage', { value: storage, configurable: true, writable: true });
}

installTestLocalStorage(globalThis as unknown as Record<string | symbol, unknown> & { localStorage?: unknown });
if (typeof window !== 'undefined') {
  installTestLocalStorage(window as unknown as Record<string | symbol, unknown> & { localStorage?: unknown });
}

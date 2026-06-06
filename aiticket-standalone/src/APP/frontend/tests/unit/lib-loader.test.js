import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

describe('lib-loader', () => {
  beforeEach(() => {
    document.head.innerHTML = '';
    document.body.innerHTML = '';
    delete window.FrontendLibLoader;
    delete window.loadLibrary;
    delete window.loadLibraries;
  });

  it('deduplicates repeated library loads', async () => {
    const appended = [];
    const originalAppendChild = document.head.appendChild.bind(document.head);

    vi.spyOn(document.head, 'appendChild').mockImplementation((node) => {
      appended.push(node);
      originalAppendChild(node);
      if (typeof node.onload === 'function') {
        setTimeout(() => node.onload(), 0);
      }
      return node;
    });

    const scriptSource = readFileSync(
      resolve(process.cwd(), 'assets/lib-loader.js'),
      'utf-8',
    );
    window.eval(scriptSource);

    await Promise.all([
      window.loadLibrary('marked'),
      window.loadLibrary('marked'),
    ]);

    expect(appended).toHaveLength(1);
    expect(appended[0].tagName).toBe('SCRIPT');
    expect(appended[0].src).toContain('/assets/vendor/marked.min.js');
  });
});

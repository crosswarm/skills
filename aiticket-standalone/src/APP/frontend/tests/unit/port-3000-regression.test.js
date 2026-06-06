import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

import { describe, expect, it } from 'vitest';

const frontendRoot = process.cwd();
const appRoot = path.resolve(frontendRoot, '..');

function loadCommonModule(filePath, { hostname = 'localhost', port = '' } = {}) {
  const source = fs.readFileSync(filePath, 'utf-8');
  const context = {
    window: {
      location: { hostname, port },
    },
    document: {
      querySelector: () => null,
      createElement: () => ({
        className: '',
        innerHTML: '',
        classList: { add() {}, remove() {} },
        remove() {},
        style: {},
      }),
      body: {
        appendChild() {},
      },
    },
    navigator: {
      onLine: true,
      clipboard: { writeText: async () => {} },
    },
    localStorage: {
      getItem: () => null,
      setItem() {},
    },
    module: { exports: {} },
    exports: {},
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (cb) => cb(),
    console,
  };

  vm.runInNewContext(source, context, { filename: filePath });
  return context.module.exports;
}

describe('local port standard regression', () => {
  it('frontend common.js should default localhost API base to 3000', () => {
    const commonPath = path.join(frontendRoot, 'common.js');
    const common = loadCommonModule(commonPath, { hostname: 'localhost' });
    expect(common.getApiBase()).toBe('http://localhost:3000');
  });

  it('frontend assets/common.js should keep same-origin mode on localhost:3000', () => {
    const commonPath = path.join(frontendRoot, 'assets', 'common.js');
    const common = loadCommonModule(commonPath, { hostname: 'localhost', port: '3000' });
    expect(common.getApiBase()).toBe('');
  });

  it('APP playwright config should default baseURL to localhost:3000', async () => {
    const configPath = path.join(appRoot, 'playwright.config.ts');
    const source = fs.readFileSync(configPath, 'utf-8');
    expect(source).toContain('baseURL: "http://localhost:3000"');
  });

  it('frontend playwright config should default local URLs to localhost:3000', async () => {
    const mod = await import(pathToFileURL(path.join(frontendRoot, 'playwright.config.js')).href);
    expect(mod.default.use.baseURL).toBe('http://localhost:3000');
    expect(mod.default.webServer.url).toBe('http://localhost:3000');
  });

  it('standalone e2e playwright config should default baseURL to localhost:3000', () => {
    const configPath = path.join(appRoot, 'e2e_tests', 'playwright.config.js');
    const source = fs.readFileSync(configPath, 'utf-8');
    expect(source).toContain("baseURL: 'http://localhost:3000'");
  });

  it('backend main entrypoint should default uvicorn port to 3000', () => {
    const mainPath = path.join(appRoot, 'backend', 'main.py');
    const source = fs.readFileSync(mainPath, 'utf-8');
    expect(source).toContain('uvicorn.run(app, host="0.0.0.0", port=3000)');
  });

  it('local backend start script should use port 3000 everywhere', () => {
    const scriptPath = path.join(appRoot, 'backend', 'start_local.sh');
    const source = fs.readFileSync(scriptPath, 'utf-8');
    expect(source).toContain('lsof -ti :3000');
    expect(source).toContain('http://localhost:3000');
    expect(source).toContain('--port 3000');
  });
});

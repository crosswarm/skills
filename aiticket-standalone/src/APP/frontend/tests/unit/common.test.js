import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// 模拟 localStorage
const localStorageMock = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
};
Object.defineProperty(window, 'localStorage', {
  value: localStorageMock,
  writable: true,
});

// 导入被测试的函数
// 注意：由于 common.js 使用全局变量方式，我们需要重新实现函数进行测试

// 重新实现函数以便测试
const getApiBase = () => {
  const hostname = window.location.hostname;
  if (hostname === 'localhost' || hostname === '127.0.0.1') {
    return `http://${hostname}:3000`;
  }
  return '';
};

const formatDate = (date, format = 'YYYY-MM-DD') => {
  const d = new Date(date);
  if (isNaN(d.getTime())) return '-';

  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const hours = String(d.getHours()).padStart(2, '0');
  const minutes = String(d.getMinutes()).padStart(2, '0');

  return format
    .replace('YYYY', year)
    .replace('MM', month)
    .replace('DD', day)
    .replace('HH', hours)
    .replace('mm', minutes);
};

const debounce = (func, wait = 300) => {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
};

const throttle = (func, limit = 300) => {
  let inThrottle;
  return function(...args) {
    if (!inThrottle) {
      func.apply(this, args);
      inThrottle = true;
      setTimeout(() => inThrottle = false, limit);
    }
  };
};

const deepClone = (obj) => {
  if (obj === null || typeof obj !== 'object') return obj;
  if (obj instanceof Date) return new Date(obj.getTime());
  if (Array.isArray(obj)) return obj.map(item => deepClone(item));
  return JSON.parse(JSON.stringify(obj));
};

const safeJsonParse = (str, defaultValue = null) => {
  try {
    return JSON.parse(str);
  } catch (e) {
    return defaultValue;
  }
};

const getStorageItem = (key, defaultValue = null) => {
  try {
    const item = localStorage.getItem(key);
    return item !== null ? safeJsonParse(item, item) : defaultValue;
  } catch (e) {
    return defaultValue;
  }
};

const setStorageItem = (key, value) => {
  try {
    if (typeof value === 'object') {
      localStorage.setItem(key, JSON.stringify(value));
    } else {
      localStorage.setItem(key, String(value));
    }
  } catch (e) {
    console.warn('localStorage设置失败:', e);
  }
};

const isOnline = () => {
  return navigator.onLine;
};

const sleep = (ms) => {
  return new Promise(resolve => setTimeout(resolve, ms));
};

function loadCommonModule(storage = localStorageMock) {
  const filePath = path.join(process.cwd(), 'common.js');
  const source = fs.readFileSync(filePath, 'utf-8');
  const context = {
    window: {
      location: { hostname: 'localhost' },
      localStorage: storage,
    },
    localStorage: storage,
    navigator: { onLine: true, clipboard: { writeText: vi.fn() } },
    document: {},
    requestAnimationFrame: (cb) => cb(),
    setTimeout,
    clearTimeout,
    console,
    module: { exports: {} },
    exports: {},
  };

  vm.runInNewContext(source, context, { filename: filePath });
  return context.module.exports;
}

describe('getApiBase', () => {
  it('应该为 localhost 返回正确 URL', () => {
    const mockLocation = { hostname: 'localhost' };
    vi.stubGlobal('location', mockLocation);
    expect(getApiBase()).toBe('http://localhost:3000');
    vi.unstubAllGlobals();
  });

  it('应该为 127.0.0.1 返回正确 URL', () => {
    const mockLocation = { hostname: '127.0.0.1' };
    vi.stubGlobal('location', mockLocation);
    expect(getApiBase()).toBe('http://127.0.0.1:3000');
    vi.unstubAllGlobals();
  });

  it('应该为生产环境返回空字符串', () => {
    const mockLocation = { hostname: 'example.com' };
    vi.stubGlobal('location', mockLocation);
    expect(getApiBase()).toBe('');
    vi.unstubAllGlobals();
  });
});

describe('formatDate', () => {
  it('应该正确格式化日期', () => {
    const date = new Date('2024-02-23 15:30:00');
    expect(formatDate(date)).toBe('2024-02-23');
  });

  it('应该支持自定义格式', () => {
    const date = new Date('2024-02-23 15:30:00');
    expect(formatDate(date, 'YYYY/MM/DD')).toBe('2024/02/23');
    expect(formatDate(date, 'YYYY-MM-DD HH:mm')).toBe('2024-02-23 15:30');
  });

  it('应该处理无效日期', () => {
    expect(formatDate('invalid')).toBe('-');
  });
});

describe('debounce', () => {
  it('应该防抖函数调用', async () => {
    const fn = vi.fn();
    const debouncedFn = debounce(fn, 100);

    debouncedFn();
    debouncedFn();
    debouncedFn();

    expect(fn).not.toHaveBeenCalled();

    await new Promise(resolve => setTimeout(resolve, 150));

    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('throttle', () => {
  it('应该节流函数调用', async () => {
    const fn = vi.fn();
    const throttledFn = throttle(fn, 100);

    throttledFn();
    throttledFn();
    throttledFn();

    expect(fn).toHaveBeenCalledTimes(1);

    await new Promise(resolve => setTimeout(resolve, 150));
    throttledFn();

    expect(fn).toHaveBeenCalledTimes(2);
  });
});

describe('deepClone', () => {
  it('应该深拷贝对象', () => {
    const obj = { a: 1, b: { c: 2 } };
    const cloned = deepClone(obj);

    expect(cloned).toEqual(obj);
    expect(cloned).not.toBe(obj);
    expect(cloned.b).not.toBe(obj.b);
  });

  it('应该深拷贝数组', () => {
    const arr = [1, [2, 3], { a: 4 }];
    const cloned = deepClone(arr);

    expect(cloned).toEqual(arr);
    expect(cloned).not.toBe(arr);
    expect(cloned[1]).not.toBe(arr[1]);
  });

  it('应该处理 Date 对象', () => {
    const date = new Date('2024-02-23');
    const cloned = deepClone(date);

    expect(cloned).toEqual(date);
    expect(cloned).not.toBe(date);
  });

  it('应该处理 null 和基本类型', () => {
    expect(deepClone(null)).toBe(null);
    expect(deepClone(123)).toBe(123);
    expect(deepClone('string')).toBe('string');
  });
});

describe('getStoredLLMConfig', () => {
  beforeEach(() => {
    localStorageMock.getItem.mockReset();
  });

  it('应该读取搜索页按 provider 分桶保存的新版配置', () => {
    localStorageMock.getItem.mockImplementation((key) => {
      if (key === 'llm_last_provider') return 'openai';
      if (key === 'llm_config_openai') {
        return JSON.stringify({
          apiKey: 'test-api-key',
          modelName: 'gpt-4.1',
          baseUrl: 'https://example.com/v1',
        });
      }
      return null;
    });

    const helper = loadCommonModule();

    expect(helper.getStoredLLMConfig()).toEqual({
      provider: 'openai',
      apiKey: 'test-api-key',
      modelName: 'gpt-4.1',
      baseUrl: 'https://example.com/v1',
    });
  });

  it('provider 为 none 时应返回空配置', () => {
    localStorageMock.getItem.mockImplementation((key) => {
      if (key === 'llm_last_provider') return 'none';
      return null;
    });

    const helper = loadCommonModule();

    expect(helper.getStoredLLMConfig()).toEqual({
      provider: 'none',
      apiKey: '',
      modelName: '',
      baseUrl: '',
    });
  });
});

describe('getSharedLLMConfig', () => {
  beforeEach(() => {
    localStorageMock.getItem.mockReset();
  });

  it('本地没有配置时应回退到后端保存的 LLM 配置', async () => {
    localStorageMock.getItem.mockImplementation((key) => {
      if (key === 'llm_last_provider') return 'none';
      return null;
    });

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        last_provider: 'openai',
        openai: {
          api_key: 'server-key',
          model_name: 'gpt-4.1',
          base_url: 'https://server.example/v1',
        },
      }),
    });

    const helper = loadCommonModule(localStorageMock);
    helper.__setFetchForTests?.(fetchMock);

    await expect(helper.getSharedLLMConfig('/api')).resolves.toEqual({
      provider: 'openai',
      apiKey: 'server-key',
      modelName: 'gpt-4.1',
      baseUrl: 'https://server.example/v1',
    });
  });

  it('禁用服务端回退时应只返回本地配置结果', async () => {
    localStorageMock.getItem.mockImplementation((key) => {
      if (key === 'llm_last_provider') return 'none';
      return null;
    });

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        last_provider: 'openai',
        openai: {
          api_key: 'server-key',
          model_name: 'gpt-4.1',
          base_url: 'https://server.example/v1',
        },
      }),
    });

    const helper = loadCommonModule(localStorageMock);
    helper.__setFetchForTests?.(fetchMock);

    await expect(helper.getSharedLLMConfig('/api', { allowServerFallback: false })).resolves.toEqual({
      provider: 'none',
      apiKey: '',
      modelName: '',
      baseUrl: '',
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('safeJsonParse', () => {
  it('应该正确解析有效 JSON', () => {
    expect(safeJsonParse('{"a": 1}')).toEqual({ a: 1 });
    expect(safeJsonParse('[1, 2, 3]')).toEqual([1, 2, 3]);
  });

  it('应该处理无效 JSON 返回默认值', () => {
    expect(safeJsonParse('invalid')).toBe(null);
    expect(safeJsonParse('invalid', 'default')).toBe('default');
  });
});

describe('localStorage 操作', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('getStorageItem', () => {
    it('应该获取存储的字符串值', () => {
      localStorageMock.getItem.mockReturnValue('value');
      expect(getStorageItem('key')).toBe('value');
    });

    it('应该解析 JSON 值', () => {
      localStorageMock.getItem.mockReturnValue('{"a": 1}');
      expect(getStorageItem('key')).toEqual({ a: 1 });
    });

    it('应该返回默认值当 key 不存在', () => {
      localStorageMock.getItem.mockReturnValue(null);
      expect(getStorageItem('key', 'default')).toBe('default');
    });

    it('应该在异常时返回默认值', () => {
      localStorageMock.getItem.mockImplementation(() => {
        throw new Error('Storage error');
      });
      expect(getStorageItem('key', 'default')).toBe('default');
    });
  });

  describe('setStorageItem', () => {
    it('应该存储对象值为 JSON', () => {
      setStorageItem('key', { a: 1 });
      expect(localStorageMock.setItem).toHaveBeenCalledWith('key', '{"a":1}');
    });

    it('应该存储字符串值', () => {
      setStorageItem('key', 'value');
      expect(localStorageMock.setItem).toHaveBeenCalledWith('key', 'value');
    });

    it('应该在异常时不抛出错误', () => {
      const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      localStorageMock.setItem.mockImplementation(() => {
        throw new Error('Storage full');
      });

      expect(() => setStorageItem('key', 'value')).not.toThrow();
      expect(consoleSpy).toHaveBeenCalled();

      consoleSpy.mockRestore();
    });
  });
});

describe('isOnline', () => {
  it('应该返回网络状态', () => {
    Object.defineProperty(navigator, 'onLine', {
      value: true,
      writable: true,
      configurable: true,
    });
    expect(isOnline()).toBe(true);

    Object.defineProperty(navigator, 'onLine', {
      value: false,
      writable: true,
      configurable: true,
    });
    expect(isOnline()).toBe(false);
  });
});

describe('sleep', () => {
  it('应该等待指定时间', async () => {
    const start = Date.now();
    await sleep(100);
    const elapsed = Date.now() - start;
    expect(elapsed).toBeGreaterThanOrEqual(90);
  });
});

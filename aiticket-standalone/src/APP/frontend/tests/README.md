# 前端测试框架

## 概述

本测试框架使用 Vitest + Playwright 进行前端测试，解决质量评审中提出的 P2-1 问题（缺少自动化测试覆盖）。

## 测试类型

### 1. 单元测试 (Vitest)

测试位置: `tests/unit/`

**测试范围**:
- `common.js` 中的工具函数
- 纯 JavaScript 逻辑
- 数据转换函数

**运行命令**:
```bash
# 运行所有单元测试
npm run test

# 监听模式
npm run test:watch

# 生成覆盖率报告
npm run test:coverage
```

### 2. E2E 测试 (Playwright)

测试位置: `tests/e2e/`

**测试范围**:
- 页面导航和加载
- 用户交互流程
- 跨页面功能
- 多环境适配

**运行命令**:
```bash
# 运行所有 E2E 测试
npm run test:e2e

# UI 模式（调试）
npm run test:e2e:ui

# 调试模式
npm run test:e2e:debug
```

## 安装依赖

```bash
cd APP/frontend
npm install
```

## 测试文件说明

| 文件 | 说明 |
|-----|-----|
| `unit/common.test.js` | common.js 单元测试 |
| `e2e/navigation.spec.js` | 页面导航测试 |
| `e2e/api-base.spec.js` | API_BASE 多环境适配测试 |
| `e2e/index-page.spec.js` | 问题智能分析页面测试 |
| `e2e/board-page.spec.js` | 智能看板页面测试 |

## 添加新测试

### 添加单元测试

在 `tests/unit/` 目录下创建新的 `.test.js` 文件：

```javascript
import { describe, it, expect } from 'vitest';

describe('功能名称', () => {
  it('应该做某事', () => {
    expect(true).toBe(true);
  });
});
```

### 添加 E2E 测试

在 `tests/e2e/` 目录下创建新的 `.spec.js` 文件：

```javascript
import { test, expect } from '@playwright/test';

test.describe('功能描述', () => {
  test('应该做某事', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('text=预期文本')).toBeVisible();
  });
});
```

## 覆盖率目标

- 单元测试覆盖率: >= 80%
- E2E 测试覆盖核心流程: 100%

## CI/CD 集成

测试可以在 CI 环境中运行：

```bash
# 完整测试套件
npm run test:all
```

## 注意事项

1. E2E 测试需要后端服务运行
2. 测试使用真实浏览器环境
3. 测试数据可能影响实际数据，建议在测试环境运行

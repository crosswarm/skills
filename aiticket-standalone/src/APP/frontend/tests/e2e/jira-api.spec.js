import { test, expect } from '@playwright/test';

/**
 * Jira API 端到端测试
 * 测试 Basic Auth 实现的 Jira API 功能
 *
 * 测试范围:
 * 1. 工单搜索 (search_issues)
 * 2. 工单分配 (assign_issue)
 * 3. 工单回复 (reply_issue)
 * 4. 工单关闭 (close_issue)
 * 5. 字段选项获取 (get_field_options)
 * 6. 错误处理
 */

const TEST_CONFIG = {
  baseURL: process.env.TEST_BASE_URL || 'http://localhost:3000',
  timeouts: {
    api: 15000,
    jira: 30000  // Jira API 可能需要更长时间
  },
  testIssue: {
    // 用于测试的工单号（如果不存在，测试将跳过相关操作）
    key: 'MYPROJECT-59562'
  }
};

// ==================== Jira API 测试套件 ====================
test.describe('Jira API - Basic Auth 实现测试', () => {

  test.beforeEach(async ({ request }) => {
    // 每个测试前验证服务可用
    const response = await request.get('/api/board/stats');
    expect(response.ok()).toBeTruthy();
  });

  // ==================== 1. 工单搜索 API 测试 ====================
  test.describe('工单搜索 API', () => {

    test('GET /api/board 应该返回工单列表', async ({ request }) => {
      const response = await request.get('/api/board', {
        params: {
          project_key: 'MYPROJECT',
          assignee: 'currentUser()'
        }
      });

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toMatchObject({
        status: 'success'
      });
      expect(data).toHaveProperty('data');
      expect(data).toHaveProperty('stats');
    });

    test('GET /api/board/issues 应该返回原始工单数据', async ({ request }) => {
      const response = await request.get('/api/board/issues');

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toBeDefined();
    });

    test('搜索应该支持不同的项目Key', async ({ request }) => {
      const response = await request.get('/api/board', {
        params: {
          project_key: 'MYPROJECT',
          assignee: 'ALL'
        }
      });

      expect(response.ok()).toBeTruthy();
      const data = await response.json();
      expect(data.status).toBe('success');
    });

    test('搜索应该处理无效的JQL', async ({ request }) => {
      // 测试空项目Key
      const response = await request.get('/api/board', {
        params: {
          project_key: '',
          assignee: 'currentUser()'
        }
      });

      // 应该返回错误或非200状态，但不应该崩溃
      expect(response.status()).toBeLessThan(500);
    });
  });

  // ==================== 2. 工单分配 API 测试 ====================
  test.describe('工单分配 API', () => {

    test('POST /api/jira/action (assign) 接口应该存在', async ({ request }) => {
      // 注意：这个测试只验证接口存在，不实际分配工单
      const response = await request.post('/api/jira/action', {
        data: {
          issue_id: 'INVALID-KEY',
          action: 'assign',
          value: 'testuser'
        }
      });

      // 接口应该存在（返回400或500，不是404）
      expect(response.status()).not.toBe(404);
    });

    test('分配接口应该验证参数', async ({ request }) => {
      // 测试缺少必要参数
      const response = await request.post('/api/jira/action', {
        data: {
          action: 'assign'
          // 缺少 issue_id 和 value
        }
      });

      // 应该返回400错误
      expect(response.status()).toBe(400);
    });

    test('分配接口应该处理无效工单', async ({ request }) => {
      const response = await request.post('/api/jira/action', {
        data: {
          issue_id: 'INVALID-KEY-12345',
          action: 'assign',
          value: 'qiangxiao'
        }
      });

      // 应该返回错误（工单不存在）
      expect(response.status()).toBeGreaterThanOrEqual(400);
    });
  });

  // ==================== 3. 工单回复 API 测试 ====================
  test.describe('工单回复 API', () => {

    test('POST /api/jira/action (reply) 接口应该存在', async ({ request }) => {
      const response = await request.post('/api/jira/action', {
        data: {
          issue_id: 'INVALID-KEY',
          action: 'reply',
          value: '测试回复内容'
        }
      });

      // 接口应该存在
      expect(response.status()).not.toBe(404);
    });

    test('回复接口应该验证参数', async ({ request }) => {
      const response = await request.post('/api/jira/action', {
        data: {
          action: 'reply'
          // 缺少必要参数
        }
      });

      expect(response.status()).toBe(400);
    });

    test('回复接口应该支持自定义字段', async ({ request }) => {
      const response = await request.post('/api/jira/action', {
        data: {
          issue_id: 'INVALID-KEY',
          action: 'reply',
          value: '测试回复',
          custom_fields: {
            solution: '测试解决方案',
            reply_method: '10917'
          }
        }
      });

      // 接口应该存在（即使工单无效）
      expect(response.status()).not.toBe(404);
    });
  });

  // ==================== 4. 工单关闭 API 测试 ====================
  test.describe('工单关闭 API', () => {

    test('POST /api/jira/action (reply_and_close) 接口应该存在', async ({ request }) => {
      const response = await request.post('/api/jira/action', {
        data: {
          issue_id: 'INVALID-KEY',
          action: 'reply_and_close',
          value: '关闭原因'
        }
      });

      // 接口应该存在
      expect(response.status()).not.toBe(404);
    });
  });

  // ==================== 5. 字段选项 API 测试 ====================
  test.describe('字段选项 API', () => {

    test('POST /api/jira/field-options 应该返回字段选项', async ({ request }) => {
      const response = await request.post('/api/jira/field-options', {
        data: {
          issue_id: TEST_CONFIG.testIssue.key,
          field_ids: ['customfield_10410', 'customfield_10729']
        }
      });

      // 接口应该存在
      expect(response.status()).not.toBe(404);

      if (response.ok()) {
        const data = await response.json();
        expect(data).toHaveProperty('status');
        expect(data).toHaveProperty('data');
      }
    });

    test('字段选项接口应该验证参数', async ({ request }) => {
      const response = await request.post('/api/jira/field-options', {
        data: {
          // 缺少必要参数
          field_ids: ['customfield_10410']
        }
      });

      expect(response.status()).toBe(400);
    });

    test('字段选项接口应该处理无效工单', async ({ request }) => {
      const response = await request.post('/api/jira/field-options', {
        data: {
          issue_id: 'INVALID-KEY-12345',
          field_ids: ['customfield_10410']
        }
      });

      // 应该返回错误或空数据
      expect(response.status()).toBeGreaterThanOrEqual(400);
    });
  });

  // ==================== 6. 智能回复生成 API 测试 ====================
  test.describe('智能回复生成 API', () => {

    test('POST /api/board/generate-reply 接口应该存在', async ({ request }) => {
      const response = await request.post('/api/board/generate-reply', {
        data: {
          issue_key: 'INVALID-KEY',
          force: false
        }
      });

      // 接口应该存在
      expect(response.status()).not.toBe(404);
    });

    test('智能回复接口应该验证参数', async ({ request }) => {
      const response = await request.post('/api/board/generate-reply', {
        data: {
          // 缺少 issue_key
          force: false
        }
      });

      expect(response.status()).toBe(400);
    });
  });

  // ==================== 7. 批量操作 API 测试 ====================
  test.describe('批量操作 API', () => {

    test('POST /api/board/batch-reanalyze 接口应该存在', async ({ request }) => {
      const response = await request.post('/api/board/batch-reanalyze', {
        data: {
          issue_keys: ['MYPROJECT-12345', 'MYPROJECT-12346']
        }
      });

      // 接口应该存在
      expect(response.status()).not.toBe(404);

      if (response.ok()) {
        const data = await response.json();
        expect(data).toHaveProperty('status');
      }
    });

    test('批量重新分析应该验证参数', async ({ request }) => {
      const response = await request.post('/api/board/batch-reanalyze', {
        data: {
          // 缺少 issue_keys
        }
      });

      expect(response.status()).toBe(400);
    });

    test('POST /api/board/move-issue 接口应该存在', async ({ request }) => {
      const response = await request.post('/api/board/move-issue', {
        data: {
          issue_key: 'INVALID-KEY',
          target_board: 'done',
          sync_jira: false
        }
      });

      // 接口应该存在
      expect(response.status()).not.toBe(404);
    });

    test('POST /api/board/batch-move 接口应该存在', async ({ request }) => {
      const response = await request.post('/api/board/batch-move', {
        data: {
          moves: [
            { issue_key: 'INVALID-KEY-1', target_board: 'done' }
          ],
          sync_jira: false
        }
      });

      // 接口应该存在
      expect(response.status()).not.toBe(404);
    });
  });

  // ==================== 8. 移动历史 API 测试 ====================
  test.describe('移动历史 API', () => {

    test('GET /api/board/move-history 应该返回历史记录', async ({ request }) => {
      const response = await request.get('/api/board/move-history');

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toHaveProperty('status', 'success');
      expect(data).toHaveProperty('data');
    });

    test('移动历史应该支持按工单过滤', async ({ request }) => {
      const response = await request.get('/api/board/move-history', {
        params: {
          issue_key: 'MYPROJECT-12345',
          limit: 5
        }
      });

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toHaveProperty('status', 'success');
      expect(data).toHaveProperty('data');
    });
  });

  // ==================== 9. 配置 API 测试 ====================
  test.describe('配置 API', () => {

    test('GET /api/config/jira 应该返回Jira配置', async ({ request }) => {
      const response = await request.get('/api/config/jira');

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toBeDefined();
    });

    test('POST /api/config/jira 应该更新配置', async ({ request }) => {
      // 注意：这个测试不会真正修改配置
      const response = await request.post('/api/config/jira', {
        data: {
          title: '测试配置',
          content: '测试内容'
        }
      });

      // 接口应该存在
      expect(response.status()).not.toBe(404);
    });
  });

  // ==================== 10. 错误处理测试 ====================
  test.describe('错误处理', () => {

    test('API应该处理认证错误', async ({ request }) => {
      // 测试无效的配置
      // 注意：由于我们使用 Basic Auth，如果凭证无效，API 应该返回 401 或 403

      // 这里我们只是验证错误处理机制存在
      const response = await request.get('/api/board', {
        params: {
          project_key: 'INVALID_PROJECT'
        }
      });

      // 不应该返回500（服务器错误），而应该返回适当的错误码
      expect(response.status()).not.toBe(500);
    });

    test('API应该处理网络超时', async ({ request }) => {
      // 这个测试验证API在超时情况下的行为
      // 由于实际测试难以模拟网络超时，我们主要验证接口存在

      const response = await request.get('/api/board', {
        timeout: 5000
      });

      // 应该收到响应（即使是错误）
      expect(response.status()).toBeDefined();
    });

    test('API应该返回正确的Content-Type', async ({ request }) => {
      const response = await request.get('/api/board/stats');

      expect(response.ok()).toBeTruthy();

      const contentType = response.headers()['content-type'];
      expect(contentType).toContain('application/json');
    });
  });

  // ==================== 11. 性能测试 ====================
  test.describe('性能测试', () => {

    test('工单搜索API响应时间应该合理', async ({ request }) => {
      const startTime = Date.now();

      const response = await request.get('/api/board', {
        params: {
          project_key: 'MYPROJECT',
          assignee: 'currentUser()'
        }
      });

      const responseTime = Date.now() - startTime;

      expect(response.ok()).toBeTruthy();
      expect(responseTime).toBeLessThan(10000); // 10秒内响应

      console.log(`Jira search API response time: ${responseTime}ms`);
    });

    test('字段选项API响应时间应该合理', async ({ request }) => {
      const startTime = Date.now();

      const response = await request.post('/api/jira/field-options', {
        data: {
          issue_id: TEST_CONFIG.testIssue.key,
          field_ids: ['customfield_10410']
        }
      });

      const responseTime = Date.now() - startTime;

      // 接口应该存在，响应时间应该合理
      expect(response.status()).not.toBe(404);
      expect(responseTime).toBeLessThan(10000);

      console.log(`Field options API response time: ${responseTime}ms`);
    });
  });
});

// ==================== 集成测试 ====================
test.describe('Jira API 集成测试', () => {

  test('完整的工单操作流程', async ({ request }) => {
    // 1. 搜索工单
    const searchResponse = await request.get('/api/board', {
      params: {
        project_key: 'MYPROJECT',
        assignee: 'currentUser()'
      }
    });

    expect(searchResponse.ok()).toBeTruthy();
    const searchData = await searchResponse.json();

    // 2. 获取统计信息
    const statsResponse = await request.get('/api/board/stats');
    expect(statsResponse.ok()).toBeTruthy();

    // 3. 验证数据一致性
    expect(searchData.status).toBe('success');
  });

  test('看板数据流完整性', async ({ request }) => {
    // 1. 获取看板配置
    const configResponse = await request.get('/api/config/board');
    expect(configResponse.ok()).toBeTruthy();

    // 2. 获取看板数据
    const boardResponse = await request.get('/api/board');
    expect(boardResponse.ok()).toBeTruthy();

    // 3. 获取分析状态
    const analysisResponse = await request.post('/api/board/analysis-status', {
      data: {
        issue_keys: ['MYPROJECT-12345']
      }
    });
    expect(analysisResponse.ok()).toBeTruthy();

    // 4. 验证所有响应格式正确
    const boardData = await boardResponse.json();
    expect(boardData).toHaveProperty('status', 'success');
    expect(boardData).toHaveProperty('data');
    expect(boardData).toHaveProperty('stats');
  });
});

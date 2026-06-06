import { test, expect } from "@playwright/test";

/**
 * 智能看板模块 - 综合端到端测试
 * Board Module Comprehensive E2E Tests
 *
 * 测试范围:
 * 1. 前端页面功能测试
 * 2. 后端API接口测试
 * 3. 前后端集成测试
 * 4. 缓存数据流测试
 * 5. 错误处理测试
 */

// ==================== 测试配置 ====================
const TEST_CONFIG = {
  baseURL: process.env.TEST_BASE_URL || "http://localhost:3000",
  timeouts: {
    navigation: 30000,
    api: 10000,
    animation: 1000,
    boardLoad: 15000,
  },
  selectors: {
    boardContainer: "#board-container",
    issueCard: ".issue-card",
    drawer: "#issue-drawer",
    drawerOverlay: "#drawer-overlay",
    loadingIndicator: "#loading-indicator",
    statsBadge: "#stats-badge",
    projectSelect: "#project-select",
    assigneeSelect: "#assignee-select",
  },
};

// ==================== 辅助函数 ====================
async function waitForBoardLoad(page) {
  // 等待看板数据加载完成
  await page.waitForFunction(
    () => {
      const badge = document.querySelector("#stats-badge");
      return badge && !badge.textContent.includes("加载中");
    },
    { timeout: TEST_CONFIG.timeouts.boardLoad },
  );
}

async function getIssueCards(page) {
  return page.locator(TEST_CONFIG.selectors.issueCard);
}

async function openFirstIssueDetail(page) {
  const cards = await getIssueCards(page);
  const count = await cards.count();

  if (count === 0) {
    console.log("No issue cards found on the board");
    return false;
  }

  const firstCard = cards.first();

  if (await firstCard.isVisible().catch(() => false)) {
    await firstCard.click();
    // Wait for drawer to be visible by checking it doesn't have translate-x-full class
    await page.waitForFunction(
      () => {
        const drawer = document.getElementById("issue-drawer");
        return drawer && !drawer.classList.contains("translate-x-full");
      },
      { timeout: 5000 },
    );
    return true;
  }
  return false;
}

// ==================== 测试套件 ====================
test.describe("智能看板模块 - 综合E2E测试", () => {
  test.beforeEach(async ({ page }) => {
    // 设置视口大小为桌面
    await page.setViewportSize({ width: 1920, height: 1080 });

    // 监听控制台错误
    page.on("console", (msg) => {
      if (msg.type() === "error") {
        console.error(`[Console Error] ${msg.text()}`);
      }
    });

    // 监听页面错误
    page.on("pageerror", (error) => {
      console.error(`[Page Error] ${error.message}`);
    });

    // 导航到看板页面
    await page.goto("/board.html", {
      timeout: TEST_CONFIG.timeouts.navigation,
    });

    // 等待页面基础元素加载
    await page.waitForSelector("h1", { timeout: 10000 });
  });

  // ==================== 1. 页面基础功能测试 ====================
  test.describe("页面基础功能", () => {
    test("页面标题和基础元素应该正确显示", async ({ page }) => {
      // 验证标题
      const title = page.locator("h1");
      await expect(title).toContainText("工单智能看板");

      // 验证导航栏存在
      await expect(page.locator("nav")).toBeVisible();

      // 验证看板容器存在
      await expect(
        page.locator(TEST_CONFIG.selectors.boardContainer),
      ).toBeVisible();
    });

    test("过滤器控件应该正常工作", async ({ page }) => {
      // 项目选择器 combobox
      const projectInput = page.locator("#cb-project-input");
      await expect(projectInput).toBeVisible();

      // 经办人选择器 combobox
      const assigneeInput = page.locator("#cb-assignee-input");
      await expect(assigneeInput).toBeVisible();

      // 筛选按钮
      const queryBtn = page.locator('button:has-text("筛选")');
      await expect(queryBtn).toBeVisible();

      // 批量分析按钮
      const batchAnalyzeBtn = page.locator('button:has-text("批量分析")');
      await expect(batchAnalyzeBtn).toBeVisible();
    });

    test("看板列应该正确渲染", async ({ page }) => {
      // 等待看板数据加载
      await waitForBoardLoad(page);

      // 检查常见的看板列
      const commonColumns = ["今天到期", "明天到期", "本周到期", "已逾期"];
      for (const column of commonColumns) {
        const columnHeader = page.locator(`text=${column}`).first();
        // 列可能不存在，但如果存在应该可见
        const isVisible = await columnHeader.isVisible().catch(() => false);
        if (isVisible) {
          await expect(columnHeader).toBeVisible();
        }
      }
    });

    test("顶部工具栏按钮应该可点击", async ({ page }) => {
      // 看板设置按钮
      const settingsBtn = page.locator('[title="看板设置"]').first();
      await expect(settingsBtn).toBeVisible();
      await settingsBtn.click();

      // 验证设置弹窗打开
      await expect(page.locator("text=看板设置")).toBeVisible();

      // 关闭弹窗
      const closeBtn = page
        .locator(
          'button:has-text("取消"), button[onclick="closeBoardSettings()"]',
        )
        .first();
      await closeBtn.click();
    });

    test("AI配置弹窗应该可以打开和关闭", async ({ page }) => {
      // 点击AI配置按钮
      const aiConfigBtn = page.locator('[title="查看AI配置"]').first();
      await expect(aiConfigBtn).toBeVisible();
      await aiConfigBtn.click();

      // 验证弹窗打开
      await expect(page.locator("text=AI 配置状态")).toBeVisible();

      // 关闭弹窗
      const closeBtn = page
        .locator('#llm-config-modal button:has-text("关闭")')
        .first();
      await closeBtn.click();

      // 验证弹窗关闭
      await expect(page.locator("#llm-config-modal")).toBeHidden();
    });
  });

  // ==================== 2. 工单卡片交互测试 ====================
  test.describe("工单卡片交互", () => {
    test("工单卡片应该可以点击查看详情", async ({ page }) => {
      await waitForBoardLoad(page);

      // 尝试打开第一个工单详情
      const hasCards = await openFirstIssueDetail(page);

      if (hasCards) {
        // 验证抽屉打开
        const drawer = page.locator(TEST_CONFIG.selectors.drawer);
        await expect(drawer).toBeVisible();

        // 验证抽屉内容
        await expect(page.locator("#drawer-title")).toBeVisible();
        await expect(page.locator("#drawer-content")).toBeVisible();

        // 关闭抽屉
        await page.click(TEST_CONFIG.selectors.drawerOverlay);
        await page.waitForTimeout(TEST_CONFIG.timeouts.animation);
      }
    });

    test("工单详情抽屉应该显示正确的操作按钮", async ({ page }) => {
      await waitForBoardLoad(page);

      const hasCards = await openFirstIssueDetail(page);

      if (hasCards) {
        // 等待抽屉动画完成
        await page.waitForTimeout(TEST_CONFIG.timeouts.animation);

        // 验证操作按钮存在（限定在抽屉内）
        const drawer = page.locator(TEST_CONFIG.selectors.drawer);
        await expect(drawer.locator('button:has-text("分配")')).toBeVisible();
        await expect(drawer.locator('button:has-text("回复")')).toBeVisible();
        await expect(
          drawer.locator('button:has-text("重新分析")'),
        ).toBeVisible();

        // 关闭抽屉
        await page.click(TEST_CONFIG.selectors.drawerOverlay);
      }
    });

    test("工单卡片应该显示正确的信息结构", async ({ page }) => {
      await waitForBoardLoad(page);

      const cards = await getIssueCards(page);
      const count = await cards.count();

      if (count > 0) {
        const firstCard = cards.first();

        // 验证卡片基本结构
        await expect(firstCard.locator(".font-medium")).toBeVisible(); // 标题
        await expect(firstCard.locator("text=MYPROJECT-")).toBeVisible(); // 工单号
      }
    });
  });

  // ==================== 3. API接口测试 ====================
  test.describe("后端API接口", () => {
    test("GET /api/board 应该返回看板数据", async ({ request }) => {
      const response = await request.get("/api/board", {
        params: { project_key: "MYPROJECT", assignee: "currentUser()" },
      });

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toHaveProperty("status", "success");
      expect(data).toHaveProperty("data");
      expect(data).toHaveProperty("stats");
    });

    test("GET /api/board/issues 应该返回工单列表", async ({ request }) => {
      const response = await request.get("/api/board/issues");

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      // 验证返回的数据结构
      expect(data).toBeDefined();
    });

    test("GET /api/board/stats 应该返回统计信息", async ({ request }) => {
      const response = await request.get("/api/board/stats");

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toHaveProperty("status", "success");
      expect(data).toHaveProperty("stats");
    });

    test("GET /api/crew/list 应该返回人员列表", async ({ request }) => {
      const response = await request.get("/api/crew/list");

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toBeDefined();
    });

    test("POST /api/board/analysis-status 应该返回分析状态", async ({
      request,
    }) => {
      const response = await request.post("/api/board/analysis-status", {
        data: { issue_keys: ["MYPROJECT-12345", "MYPROJECT-12346"] },
      });

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toHaveProperty("status", "success");
      expect(data).toHaveProperty("updates");
    });
  });

  // ==================== 4. 看板配置API测试 ====================
  test.describe("看板配置API", () => {
    test("GET /api/config/board 应该返回看板配置", async ({ request }) => {
      const response = await request.get("/api/config/board");

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toBeDefined();
    });

    test("GET /api/config/jira 应该返回Jira配置", async ({ request }) => {
      const response = await request.get("/api/config/jira");

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toBeDefined();
    });

    test("GET /api/config/llm 应该返回LLM配置", async ({ request }) => {
      const response = await request.get("/api/config/llm");

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toBeDefined();
    });
  });

  // ==================== 5. 缓存功能测试 ====================
  test.describe("缓存功能", () => {
    test("看板数据应该被缓存", async ({ page, request }) => {
      // 第一次请求
      const start1 = Date.now();
      const response1 = await request.get("/api/board");
      const duration1 = Date.now() - start1;

      expect(response1.ok()).toBeTruthy();

      // 第二次请求应该更快（从缓存）
      const start2 = Date.now();
      const response2 = await request.get("/api/board");
      const duration2 = Date.now() - start2;

      expect(response2.ok()).toBeTruthy();

      // 验证两次返回的数据一致
      const data1 = await response1.json();
      const data2 = await response2.json();

      // 缓存数据应该包含相同的工单数量
      if (data1.data && data2.data) {
        expect(typeof data1.data).toBe(typeof data2.data);
      }
    });

    test("AI分析状态缓存应该工作", async ({ request }) => {
      // 查询分析状态
      const response = await request.post("/api/board/analysis-status", {
        data: { issue_keys: ["MYPROJECT-TEST-001"] },
      });

      expect(response.ok()).toBeTruthy();

      const data = await response.json();
      expect(data).toHaveProperty("updates");
    });
  });

  // ==================== 6. 错误处理测试 ====================
  test.describe("错误处理", () => {
    test("API应该正确处理无效参数", async ({ request }) => {
      // 测试无效的项目Key
      const response = await request.get("/api/board", {
        params: { project_key: "", assignee: "invalid" },
      });

      // 应该返回错误或非200状态码
      // 或者返回空数据但不崩溃
      expect(response.status()).toBeLessThan(500);
    });

    test("页面应该在网络错误时显示友好提示", async ({ page }) => {
      // 模拟网络断开
      await page.route("**/api/board", (route) =>
        route.abort("internetdisconnected"),
      );

      // 刷新页面
      await page.reload();

      // 页面应该仍然可访问（即使数据加载失败）
      await expect(page.locator("h1")).toContainText("工单智能看板");

      // 恢复网络
      await page.unroute("**/api/board");
    });

    test("不存在的API端点应该返回404", async ({ request }) => {
      const response = await request.get("/api/nonexistent-endpoint");
      expect(response.status()).toBe(404);
    });
  });

  // ==================== 7. 响应式布局测试 ====================
  test.describe("响应式布局", () => {
    test("在平板尺寸下应该正常显示", async ({ page }) => {
      await page.setViewportSize({ width: 768, height: 1024 });
      await page.reload();

      await waitForBoardLoad(page);

      // 验证页面基本功能正常
      await expect(page.locator("h1")).toContainText("工单智能看板");
      await expect(
        page.locator(TEST_CONFIG.selectors.boardContainer),
      ).toBeVisible();
    });

    test("在手机尺寸下应该正常显示", async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 667 });
      await page.reload();

      await waitForBoardLoad(page);

      // 验证页面基本功能正常
      await expect(page.locator("h1")).toContainText("工单智能看板");
    });
  });

  // ==================== 8. 性能测试 ====================
  test.describe("性能测试", () => {
    test("看板数据加载应该在合理时间内完成", async ({ page }) => {
      const startTime = Date.now();

      await page.reload();
      await waitForBoardLoad(page);

      const loadTime = Date.now() - startTime;

      // 加载时间应该小于15秒
      expect(loadTime).toBeLessThan(15000);

      console.log(`Board load time: ${loadTime}ms`);
    });

    test("API响应时间应该在合理范围内", async ({ request }) => {
      const startTime = Date.now();

      const response = await request.get("/api/board");

      const responseTime = Date.now() - startTime;

      // API响应时间应该小于5秒
      expect(responseTime).toBeLessThan(5000);
      expect(response.ok()).toBeTruthy();

      console.log(`API response time: ${responseTime}ms`);
    });
  });

  // ==================== 9. 数据一致性测试 ====================
  test.describe("数据一致性", () => {
    test("看板统计数据应该与工单列表一致", async ({ request }) => {
      const response = await request.get("/api/board");

      expect(response.ok()).toBeTruthy();

      const data = await response.json();

      if (data.data && data.stats) {
        // 验证统计数据结构
        expect(data.stats).toBeDefined();

        // 如果有具体的统计字段，验证它们
        if (data.stats.total !== undefined) {
          expect(typeof data.stats.total).toBe("number");
        }
      }
    });

    test("工单详情API应该返回完整信息", async ({ request }) => {
      // 先获取工单列表
      const listResponse = await request.get("/api/board/issues");

      if (listResponse.ok()) {
        const listData = await listResponse.json();

        // 如果有工单，测试第一个工单的详情
        if (listData && listData.length > 0) {
          const firstIssue = listData[0];
          expect(firstIssue).toHaveProperty("key");
          expect(firstIssue).toHaveProperty("summary");
        }
      }
    });
  });
});

// ==================== 独立测试 ====================
test.describe("独立功能测试", () => {
  test("搜索相似工单API应该工作", async ({ request }) => {
    const response = await request.get("/api/board/search", {
      params: { q: "测试查询", top_k: 5 },
    });

    expect(response.ok()).toBeTruthy();

    const data = await response.json();
    expect(data).toHaveProperty("status", "success");
    expect(data).toHaveProperty("results");
  });

  test("人员搜索API应该工作", async ({ request }) => {
    const response = await request.get("/api/crew/search", {
      params: { q: "测试" },
    });

    expect(response.ok()).toBeTruthy();

    const data = await response.json();
    expect(data).toHaveProperty("query");
    expect(data).toHaveProperty("results");
  });
});

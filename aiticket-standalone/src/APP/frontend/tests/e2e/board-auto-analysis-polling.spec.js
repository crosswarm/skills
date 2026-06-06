import { test, expect } from "@playwright/test";

const mockColumns = [
  {
    key: "today",
    title: "今天到期",
    color: "orange",
    bg: "bg-orange-50",
    visible: true,
  },
];

const analyzingIssue = {
  key: "MYPROJECT-AUTO-1001",
  summary: "自动分析工单应从分析中更新为已完成",
  status: "处理中",
  priority: "高",
  assignee: "tester",
  reporter: "reporter",
  due_date: "2026-03-20T09:00:00",
  created: "2026-03-19T10:00:00",
  description: "用于验证首屏自动分析状态会被前端轮询更新。",
  ai_status: "analyzing",
  ai_analysis: null,
};

const completedAnalysis = {
  recommended_team: "云平台-流程中心",
  recommended_role: "产品经理",
  functionality_impact: "审批流配置体验",
  solution_suggestion: "应在轮询完成后更新看板卡片状态。",
  confidence: 0.91,
  model_used: "glm-5",
  is_reused: false,
  similar_issues: [],
};

test.describe("智能看板自动分析轮询", () => {
  test("首屏自动分析中的工单应自动进入轮询并更新为完成态", async ({ page }) => {
    let analysisStatusPollCount = 0;

    await page.addInitScript(() => {
      const originalSetInterval = window.setInterval.bind(window);
      window.setInterval = (handler, timeout, ...args) =>
        originalSetInterval(handler, timeout >= 5000 ? 50 : timeout, ...args);

      localStorage.setItem("llm_last_provider", "gemini");
      localStorage.setItem(
        "llm_config_gemini",
        JSON.stringify({
          apiKey: "test-key",
          modelName: "gemini-test",
          baseUrl: "https://example.test",
        }),
      );
    });

    await page.route("**/api/config/board", async (route) => {
      await route.fulfill({
        json: { columns: mockColumns },
      });
    });

    await page.route("**/api/config/llm", async (route) => {
      await route.fulfill({
        json: { status: "success" },
      });
    });

    await page.route("**/api/board/analysis-status", async (route) => {
      analysisStatusPollCount += 1;
      await route.fulfill({
        json: {
          status: "success",
          updates: {
            [analyzingIssue.key]: {
              status: "completed",
              analysis: completedAnalysis,
            },
          },
        },
      });
    });

    await page.route("**/api/board?**", async (route) => {
      await route.fulfill({
        json: {
          status: "success",
          data: {
            today: [analyzingIssue],
          },
          stats: {
            vector_stats: {
              issues_count: 1,
              analysis_count: 0,
            },
          },
        },
      });
    });

    await page.goto("/board.html");

    const card = page.locator(".issue-card").first();
    await expect(card).toContainText("分析中");

    await expect
      .poll(() => analysisStatusPollCount, {
        timeout: 2000,
        message: "自动分析中的工单没有被加入轮询队列",
      })
      .toBeGreaterThan(0);

    await expect(card).toContainText("云平台-流程中心");
    await expect(card).toContainText("产品经理");
  });
});

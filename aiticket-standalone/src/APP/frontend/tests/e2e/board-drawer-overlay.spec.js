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

const mockIssue = {
  key: "MYPROJECT-TEST-1001",
  summary: "抽屉关闭后遮罩应消失",
  status: "处理中",
  priority: "高",
  assignee: "tester",
  reporter: "reporter",
  due_date: "2026-03-19T09:00:00",
  created: "2026-03-18T10:00:00",
  description: "用于验证智能看板详情抽屉关闭后的遮罩状态。",
  ai_status: "completed",
  ai_analysis: {
    recommended_team: "产品支持",
    recommended_role: "实施",
    functionality_impact: "影响页面交互",
    solution_suggestion: "关闭抽屉时隐藏 overlay",
    confidence: 0.92,
  },
};

test.describe("智能看板抽屉遮罩", () => {
  test("关闭详情抽屉后应移除 overlay 并恢复页面交互", async ({ page }) => {
    await page.addInitScript(() => {
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

    await page.route("**/api/board**", async (route) => {
      await route.fulfill({
        json: {
          status: "success",
          data: {
            today: [mockIssue],
          },
          stats: {
            vector_stats: {
              issues_count: 1,
              analysis_count: 1,
            },
          },
        },
      });
    });

    await page.goto("/board.html");
    await expect(page.locator(".issue-card")).toHaveCount(1);

    await page.locator(".issue-card").first().click();
    await page.waitForFunction(() => {
      const drawer = document.getElementById("issue-drawer");
      const overlay = document.getElementById("drawer-overlay");
      return (
        drawer &&
        overlay &&
        !drawer.classList.contains("translate-x-full") &&
        !overlay.classList.contains("hidden")
      );
    });

    await page.locator('#issue-drawer button[onclick="closeDrawer()"]').click();
    await page.waitForTimeout(350);

    await expect(page.locator("#drawer-overlay")).toHaveClass(/hidden/);

    await page.locator('[title="看板设置"]').click();
    await expect(page.locator("#board-settings-modal")).toBeVisible();
  });
});

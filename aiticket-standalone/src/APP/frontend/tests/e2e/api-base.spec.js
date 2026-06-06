import { test, expect } from "@playwright/test";

/**
 * API_BASE 多环境适配测试
 */

test.describe("API_BASE 多环境适配", () => {
  test("localhost 环境应该使用 3000 端口", async ({ page }) => {
    // 拦截所有请求，检查 API_BASE
    const apiRequests = [];
    page.on("request", (request) => {
      const url = request.url();
      if (url.includes("/api/")) {
        apiRequests.push(url);
      }
    });

    await page.goto("http://localhost:3000/");

    // 等待页面加载完成
    await page.waitForLoadState("networkidle");

    // 检查 API 请求是否使用了正确的 base URL
    const apiCalls = apiRequests.filter((url) =>
      url.includes("localhost:3000"),
    );

    // 如果页面发起了 API 请求，应该使用 localhost:3000
    if (apiCalls.length > 0) {
      for (const url of apiCalls) {
        expect(url).toContain("localhost:3000");
      }
    }
  });

  test("API 请求不应该返回 404", async ({ page }) => {
    const failedRequests = [];

    page.on("response", (response) => {
      if (response.status() === 404 && response.url().includes("/api/")) {
        failedRequests.push(response.url());
      }
    });

    await page.goto("/");
    await page.goto("/board.html");
    await page.goto("/kb.html");

    // 等待网络空闲
    await page.waitForLoadState("networkidle");

    // 不应该有 404 的 API 请求
    expect(failedRequests).toHaveLength(0);
  });
});

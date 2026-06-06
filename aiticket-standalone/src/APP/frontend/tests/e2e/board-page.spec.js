import { test, expect } from "@playwright/test";

/**
 * 智能看板页面测试
 */

test.describe("智能看板页面", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/board.html");
  });

  test("页面基本元素应该存在", async ({ page }) => {
    // 标题
    await expect(page.locator("h1")).toContainText("工单智能看板");
    await expect(page.locator('link[href*="board.css"]')).toHaveCount(1);
    await expect(page.locator("#board-container")).toBeVisible();

    const navLink = page.locator("nav a").first();
    const navTextDecoration = await navLink.evaluate(
      (node) => window.getComputedStyle(node).textDecorationLine,
    );
    expect(navTextDecoration).toBe("none");
  });

  test("AI 配置按钮应该存在", async ({ page }) => {
    await expect(page.locator('[title="查看AI配置"]')).toBeVisible();
  });

  test("看板配置按钮应该存在", async ({ page }) => {
    await expect(page.locator('[title="看板设置"]')).toBeVisible();
  });

  test("刷新按钮应该存在", async ({ page }) => {
    await expect(page.locator('[title="刷新"]')).toBeVisible();
  });

  test("搜索框应该可以输入", async ({ page }) => {
    const searchInput = page.locator(
      'input[placeholder*="搜索"], input[type="search"]',
    );

    if (await searchInput.isVisible().catch(() => false)) {
      await searchInput.fill("MYPROJECT-12345");
      await expect(searchInput).toHaveValue("MYPROJECT-12345");
    }
  });

  test("工单卡片应该可以点击查看详情", async ({ page }) => {
    // 等待看板数据加载
    await page.waitForTimeout(2000);

    // 查找第一个工单卡片
    const card = page.locator(".issue-card, [data-issue-key]").first();

    if (await card.isVisible().catch(() => false)) {
      await card.click();

      // 检查详情抽屉是否打开
      const drawer = page
        .locator('.drawer, [role="dialog"], .fixed.right-0')
        .first();
      await expect(drawer).toBeVisible();
    }
  });

  test("看板配置弹窗应该可以打开", async ({ page }) => {
    // 点击配置按钮
    const configBtn = page.locator('[title="看板设置"]').first();
    await configBtn.click();

    // 检查弹窗是否打开
    const modal = page.locator("#board-settings-modal");
    await expect(modal).toBeVisible();
    await expect(modal).toContainText("看板设置");
    const overlayBg = await modal.evaluate(
      (node) => window.getComputedStyle(node).backgroundColor,
    );
    expect(overlayBg).not.toBe("rgba(0, 0, 0, 0)");
  });

  test("主要操作按钮不应出现浏览器默认黑框", async ({ page }) => {
    const queryButton = page.getByRole("button", { name: "筛选" });
    await expect(queryButton).toBeVisible();
    const style = await queryButton.evaluate((node) => {
      const computed = window.getComputedStyle(node);
      return {
        borderTopColor: computed.borderTopColor,
        borderTopWidth: computed.borderTopWidth,
        appearance:
          computed.getPropertyValue("appearance") ||
          computed.getPropertyValue("-webkit-appearance"),
      };
    });

    expect(style.borderTopColor).not.toBe("rgb(0, 0, 0)");
    expect(style.appearance).not.toBe("auto");
  });
});

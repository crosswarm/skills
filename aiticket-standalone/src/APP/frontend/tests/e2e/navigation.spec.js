import { test, expect } from "@playwright/test";

/**
 * 导航和页面加载测试
 * compact 版裁剪后可见导航：问题分析(index.html) / 智能看板(board.html) / 知识库(kb.html)。
 * 已移除：周报分析(report.html) / PM任务看板(pm_board.html)。guide.html 为 hidden 不计入主导航。
 */

const NAV_ITEMS = [
  { label: "问题分析", href: "index.html" },
  { label: "智能看板", href: "board.html" },
  { label: "知识库", href: "kb.html" },
];

const DESKTOP_CASES = [
  { path: "/index.html", active: "问题分析" },
  { path: "/board.html", active: "智能看板" },
  { path: "/kb.html", active: "知识库" },
];

const MOBILE_CASES = [
  { path: "/index.html", active: "问题分析" },
  { path: "/board.html", active: "智能看板" },
  { path: "/kb.html", active: "知识库" },
];

async function readNavItems(locator) {
  return await locator.locator("a").evaluateAll((nodes) =>
    nodes.map((node) => ({
      label: node.textContent.replace(/\s+/g, " ").trim(),
      href: node.getAttribute("href"),
      className: node.className,
    })),
  );
}

test.describe("页面导航", () => {
  test("首页应该正确加载", async ({ page }) => {
    await page.goto("/");
    await expect(
      page.locator("nav.desktop-nav").getByRole("link", { name: "智能看板" }),
    ).toBeVisible();
  });

  test("导航链接应该正常工作", async ({ page }) => {
    await page.goto("/");

    // 导航到看板页面
    await page.click("text=智能看板");
    await expect(page).toHaveURL(/board.html/);

    // 导航到知识库
    await page.goto("/");
    await page.click("text=知识库");
    await expect(page).toHaveURL(/kb.html/);
  });

  test("桌面导航应在所有主页面保持统一顺序、链接和当前态", async ({ page }) => {
    for (const { path, active } of DESKTOP_CASES) {
      await page.goto(path);

      const nav = page.locator("nav.desktop-nav").first();
      await expect(nav, `${path} should render a shared nav`).toBeVisible();

      const items = await readNavItems(nav);
      expect(
        items.map((item) => item.label),
        `${path} nav labels`,
      ).toEqual(NAV_ITEMS.map((item) => item.label));
      expect(
        items.map((item) => item.href),
        `${path} nav hrefs`,
      ).toEqual(NAV_ITEMS.map((item) => item.href));

      const activeItems = items
        .filter(
          (item) =>
            item.className.includes("bg-indigo-100") ||
            item.className.includes("bg-indigo-600"),
        )
        .map((item) => item.label);

      expect(activeItems, `${path} active nav item`).toEqual([active]);
    }
  });

  test("问题分析页别名都应正确高亮导航", async ({ page }) => {
    for (const path of ["/index.html", "/"]) {
      await page.goto(path);

      const nav = page.locator("nav.desktop-nav").first();
      const items = await readNavItems(nav);
      const activeItems = items
        .filter((item) => item.className.includes("bg-indigo-100"))
        .map((item) => item.label);

      expect(activeItems, `${path} active nav item`).toEqual(["问题分析"]);
    }
  });

  test("移动端导航菜单应保持统一顺序、链接和当前态", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });

    for (const { path, active } of MOBILE_CASES) {
      await page.goto(path);
      await page.click("#hamburger-btn");

      const overlay = page.locator("#mobile-nav-overlay");
      await expect(overlay, `${path} mobile nav overlay`).toHaveClass(/open/);

      const items = await readNavItems(overlay.locator("nav").first());
      expect(
        items.map((item) => item.label),
        `${path} mobile labels`,
      ).toEqual(NAV_ITEMS.map((item) => item.label));
      expect(
        items.map((item) => item.href),
        `${path} mobile hrefs`,
      ).toEqual(NAV_ITEMS.map((item) => item.href));

      const activeItems = items
        .filter((item) => item.className.includes("active"))
        .map((item) => item.label);

      expect(activeItems, `${path} mobile active nav item`).toEqual([active]);
    }
  });

  test("核心页面首屏不应依赖远端 CDN 脚本", async ({ page }) => {
    for (const target of ["/board.html", "/kb.html"]) {
      await page.goto(target);
      const remoteScripts = await page
        .locator('script[src^="http"]')
        .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("src")));
      expect(
        remoteScripts,
        `${target} should not include remote scripts`,
      ).toEqual([]);
    }
  });
});

import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright 配置 - 智能看板模块 E2E 测试
 * @see https://playwright.dev/docs/test-configuration
 */
export default defineConfig({
  testDir: "./tests/e2e",

  /* 每个测试的超时时间 */
  timeout: 60 * 1000,

  /* 期望的超时时间 */
  expect: {
    timeout: 10000,
  },

  /* 并发运行测试 */
  fullyParallel: false, // 改为false以避免测试间干扰

  /* 失败时禁止并行 */
  forbidOnly: !!process.env.CI,

  /* 重试次数 */
  retries: process.env.CI ? 2 : 1,

  /* 并行工作线程数 */
  workers: 1, // 串行执行以确保稳定性

  /* 报告器配置 */
  reporter: [
    ["html", { open: "never", outputFolder: "playwright-report" }],
    ["json", { outputFile: "test-results/test-results.json" }],
    ["list"],
  ],

  /* 共享项目配置 */
  use: {
    /* 基础URL */
    baseURL: process.env.TEST_BASE_URL || "http://localhost:3008",

    /* 收集所有跟踪 */
    trace: "on-first-retry",

    /* 截图 */
    screenshot: "only-on-failure",

    /* 视频 */
    video: "on-first-retry",

    /* 视口大小 */
    viewport: { width: 1920, height: 1080 },

    /* 动作超时 */
    actionTimeout: 15000,

    /* 导航超时 */
    navigationTimeout: 30000,
  },

  /* 针对不同浏览器的项目配置 */
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "firefox",
      use: { ...devices["Desktop Firefox"] },
    },
    {
      name: "webkit",
      use: { ...devices["Desktop Safari"] },
    },
    /* 移动端测试 */
    {
      name: "Mobile Chrome",
      use: { ...devices["Pixel 5"] },
    },
    {
      name: "Mobile Safari",
      use: { ...devices["iPhone 12"] },
    },
  ],

  /* 服务器由外部管理（nohup uvicorn ... --port 3008），测试前需手动启动 */

  /* 输出目录 */
  outputDir: "test-results/",
});

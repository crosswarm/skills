import { test, expect } from '@playwright/test';

/**
 * 问题智能分析页面测试
 */

test.describe('问题智能分析页面', () => {

  test.beforeEach(async ({ page }) => {
    await page.goto('/search.html');
  });

  test('页面基本元素应该存在', async ({ page }) => {
    // 标题
    await expect(page.locator('h1')).toContainText('工单智能分析');

    // 搜索框
    await expect(page.locator('#searchInput')).toBeVisible();

    // 设置按钮
    await expect(page.locator('button[title="Settings"]')).toBeVisible();

    // 分析按钮
    await expect(page.getByRole('button', { name: '搜索' })).toBeVisible();
  });

  test('页面首屏不应依赖远端 CDN 脚本', async ({ page }) => {
    const remoteScripts = await page.locator('script[src^="http"]').evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute('src'))
    );

    expect(remoteScripts).toEqual([]);
  });

  test('设置弹窗应该可以打开和关闭', async ({ page }) => {
    // 打开设置
    await page.locator('button[title="Settings"]').click();
    await expect(page.locator('text=模型服务商')).toBeVisible();

    // 关闭设置
    await page.keyboard.press('Escape');
    // 或者点击关闭按钮
    // await page.click('text=取消');
  });

  test('LLM 配置应该可以保存', async ({ page }) => {
    // 打开设置
    await page.locator('button[title="Settings"]').click();

    // 选择 provider
    await page.selectOption('#providerSelect', 'gemini');

    // 输入 API Key
    await page.fill('#apiKeyInput', 'test-api-key');

    // 保存
    await page.getByRole('button', { name: '保存' }).click();

    // 检查 toast 提示
    await expect(page.locator('.toast, .toast-notification')).toContainText(/保存|成功/);
  });

  test('搜索输入应该可以正常工作', async ({ page }) => {
    const searchInput = page.locator('#searchInput');

    await searchInput.fill('这是一个测试问题');
    await expect(searchInput).toHaveValue('这是一个测试问题');

    // 清空
    await searchInput.clear();
    await expect(searchInput).toHaveValue('');
  });

  test('图片上传区域应该存在', async ({ page }) => {
    await expect(page.locator('button[title="Upload Image"]')).toBeVisible();
  });
});

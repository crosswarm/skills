#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

async function main() {
  const payloadPath = process.argv[2];
  if (!payloadPath) {
    throw new Error('payload path is required');
  }

  const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf-8'));
  const outputDir = payload.output_dir || path.dirname(payloadPath);
  fs.mkdirSync(outputDir, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const contextOptions = {};
  if (payload.storage_state_path) {
    contextOptions.storageState = payload.storage_state_path;
  }
  const context = await browser.newContext(contextOptions);
  const page = await context.newPage();

  try {
    await page.goto(payload.target_url, { waitUntil: 'networkidle', timeout: 60000 });
    await page.waitForTimeout(1200);

    const captures = [];
    const pageCapturePath = path.join(outputDir, `${payload.task_id}-page.png`);
    await page.screenshot({ path: pageCapturePath, fullPage: true });
    captures.push({
      id: `${payload.task_id}-page`,
      title: payload.capture_mode === 'authenticated_capture' ? '登录后整页截图' : '公开页面整页截图',
      file_path: pageCapturePath,
      capture_type: 'page',
    });

    if (payload.focus_hint) {
      const clipCapturePath = path.join(outputDir, `${payload.task_id}-viewport.png`);
      await page.screenshot({ path: clipCapturePath, fullPage: false });
      captures.push({
        id: `${payload.task_id}-viewport`,
        title: `${payload.focus_hint} 聚焦截图`,
        file_path: clipCapturePath,
        capture_type: 'viewport',
      });
    }

    process.stdout.write(JSON.stringify({
      captures,
      notes: payload.focus_hint ? [`已按提示补抓：${payload.focus_hint}`] : ['页面已稳定并截图'],
    }));
  } finally {
    await context.close();
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});

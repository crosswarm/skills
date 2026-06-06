import { test, expect } from '@playwright/test';

test.describe('知识库问答与搜索页集成', () => {
  test('kb 页面首屏不应依赖远端 CDN 脚本', async ({ page }) => {
    await page.goto('/kb.html');

    const remoteScripts = await page.locator('script[src^="http"]').evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute('src'))
    );

    expect(remoteScripts).toEqual([]);
  });

  test('kb 页面应支持问答、正文与元数据切换', async ({ page }) => {
    await page.goto('/kb.html');

    await expect(page.locator('h1')).toContainText('知识库工作台');
    await expect(page.getByRole('heading', { name: /知识库问答/ })).toBeVisible();

    await page.fill('#searchInput', '流程监控如何查询未来审批人');
    await page.getByRole('button', { name: '提问' }).click();

    await expect(page.locator('#resultCount')).not.toHaveText('0 条', { timeout: 20000 });
    await expect(page.locator('#detailPanel')).toContainText('知识库回答', { timeout: 20000 });
    await expect(page.locator('#detailPanel')).not.toContainText("Hello! I'm here to help", { timeout: 20000 });

    const detailText = await page.locator('#detailPanel').innerText();
    expect(detailText.length).toBeGreaterThan(150);

    const firstTopicChip = page.locator('#resultList .bg-indigo-50').first();
    await expect(firstTopicChip).toBeVisible();

    await page.getByRole('button', { name: '查看元数据' }).first().click();
    await expect(page.locator('#kbDrawer')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('#kbDrawerTitle')).toContainText('证据详情', { timeout: 10000 });
    await expect(page.locator('#kbDrawerBody')).toContainText('元数据', { timeout: 10000 });
    await expect(page.locator('#kbDrawerBody')).toContainText('content_id', { timeout: 10000 });
    await expect(page.locator('#kbDrawerBody button').filter({ hasText: '查看元数据' }).first()).toHaveClass(/bg-indigo-600/);

    await page.getByRole('button', { name: '查看正文' }).first().click();
    await expect(page.locator('#kbDrawerBody')).toContainText('正文预览', { timeout: 10000 });
    await expect(page.locator('#kbDrawerBody button').filter({ hasText: '查看正文' }).first()).toHaveClass(/bg-indigo-600/);

    const cards = page.locator('#resultList [data-content-id]');
    const cardCount = await cards.count();
    if (cardCount > 1) {
      const secondCard = cards.nth(1);
      const secondTitle = (await secondCard.locator('.font-medium').first().innerText()).trim();
      if (test.info().project.name.includes('Mobile')) {
        await page.locator('#kbDrawerClose').click();
        await expect(page.locator('#kbDrawer')).toBeHidden({ timeout: 10000 });
      }
      await secondCard.click();
      await page.getByRole('button', { name: '证据详情' }).click();
      await expect(page.locator('#kbDrawerBody')).toContainText(secondTitle, { timeout: 10000 });
      await expect(page.locator('#kbDrawerBody')).toContainText('文章摘要', { timeout: 10000 });
    }
  });

  test('ticket_case 结果应只展示工单编号和元数据入口，不直接展示正文', async ({ page }) => {
    await page.route('**/api/kb/qa', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          answer_text: '命中 1 条知识库资料和 1 条历史工单，可优先查看元数据确认工单范围。',
          answer_html: '<p>命中 1 条知识库资料和 1 条历史工单，可优先查看元数据确认工单范围。</p>',
          used_llm: false,
          fallback_used: true,
          topics: ['工作流设计'],
          source_counts: {
            kb_local: { count: 1 },
            apcom_docs: { count: 0 },
            ticket_case: { count: 1 },
          },
          source_groups: {
            kb_local: [],
            apcom_docs: [],
            ticket_case: [
              {
                content_id: 'TICKET-MYPROJECT-1001',
                source_kind: 'ticket_case',
                name: 'MYPROJECT-1001 连岗审批未生效',
                summary: '客户反馈连岗审批配置后未自动审批。',
                citation_label: '[TICKET] MYPROJECT-1001',
              },
            ],
          },
          sources: [
            {
              content_id: 'TICKET-MYPROJECT-1001',
              source_kind: 'ticket_case',
              name: 'MYPROJECT-1001 连岗审批未生效',
              summary: '客户反馈连岗审批配置后未自动审批。',
              citation_label: '[TICKET] MYPROJECT-1001',
            },
          ],
        }),
      });
    });

    await page.route('**/api/kb/content/TICKET-MYPROJECT-1001', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          content_id: 'TICKET-MYPROJECT-1001',
          source_kind: 'ticket_case',
          display_mode: 'metadata_only',
          name: 'MYPROJECT-1001 连岗审批未生效',
          summary: '客户反馈连岗审批配置后未自动审批。',
          raw_content: '',
          ticket_metadata: {
            issue_key: 'MYPROJECT-1001',
            module: '工作流设计',
          },
          metadata_url: '/api/kb/metadata/TICKET-MYPROJECT-1001',
        }),
      });
    });

    await page.route('**/api/kb/metadata/TICKET-MYPROJECT-1001', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          content_id: 'TICKET-MYPROJECT-1001',
          source_kind: 'ticket_case',
          issue_key: 'MYPROJECT-1001',
          module: '工作流设计',
        }),
      });
    });

    await page.goto('/kb.html');
    await page.fill('#searchInput', '连岗审批该怎么设置');
    await page.getByRole('button', { name: '提问' }).click();

    await expect(page.locator('#resultList')).toContainText('MYPROJECT-1001');
    await page.getByRole('button', { name: '查看元数据' }).first().click();
    await expect(page.locator('#kbDrawer')).toBeVisible();
    await expect(page.locator('#kbDrawerBody')).toContainText('MYPROJECT-1001');
    await expect(page.locator('#kbDrawerBody')).not.toContainText('不应该直接暴露');
    await expect(page.locator('#kbDrawerBody')).toContainText('issue_key');
  });

  test('kb 页面应将主资料与工单侧证分层展示', async ({ page }) => {
    await page.route('**/api/kb/qa', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          answer_text: '流程监控支持人工干预，可在监控界面执行人工调整、结束审批和流程终止。',
          answer_html: '<p>流程监控支持人工干预，可在监控界面执行人工调整、结束审批和流程终止。</p>',
          used_llm: true,
          fallback_used: false,
          topics: ['流程监控', '审批干预'],
          query_profile: {
            query_intent: 'operation',
            source_weight_strategy: '操作题优先使用 kb_local，apcom_docs 补充原理说明，ticket_case 仅作侧证。',
          },
          relevance_summary: {
            high_count: 1,
            medium_count: 1,
            low_count: 0,
          },
          ticket_summary: {
            related_count: 1,
            top_issue_keys: ['MYPROJECT-1001'],
            items: [
              {
                content_id: 'TICKET-MYPROJECT-1001',
                source_kind: 'ticket_case',
                name: 'MYPROJECT-1001 流程监控人工干预案例',
                summary: '客户通过流程监控人工调整审批人。',
                citation_label: '[TICKET] MYPROJECT-1001',
              },
            ],
          },
          source_counts: {
            kb_local: { count: 1 },
            apcom_docs: { count: 0 },
            ticket_case: { count: 1 },
          },
          primary_materials: [
            {
              content_id: 'DOC-FLOW-1',
              source_kind: 'kb_local',
              name: 'CNT-0005 帮助文档-流程监控',
              summary: '覆盖人工调整、结束审批、流程终止和解除超时挂起。',
              citation_label: '[KB] CNT-0005',
              topic_names: ['流程监控'],
              relevance_level: 'high',
              relevance_reason: '标题或摘要直接覆盖流程监控、干预，与问题高度相关。',
            },
          ],
          sources: [
            {
              content_id: 'DOC-FLOW-1',
              source_kind: 'kb_local',
              name: 'CNT-0005 帮助文档-流程监控',
              summary: '覆盖人工调整、结束审批、流程终止和解除超时挂起。',
              citation_label: '[KB] CNT-0005',
              topic_names: ['流程监控'],
              relevance_level: 'high',
              relevance_reason: '标题或摘要直接覆盖流程监控、干预，与问题高度相关。',
            },
            {
              content_id: 'TICKET-MYPROJECT-1001',
              source_kind: 'ticket_case',
              name: 'MYPROJECT-1001 流程监控人工干预案例',
              summary: '客户通过流程监控人工调整审批人。',
              citation_label: '[TICKET] MYPROJECT-1001',
              relevance_level: 'medium',
              relevance_reason: '工单侧证命中流程监控，适合补充案例与风险。',
            },
          ],
          source_groups: {
            kb_local: [
              {
                content_id: 'DOC-FLOW-1',
                source_kind: 'kb_local',
                name: 'CNT-0005 帮助文档-流程监控',
                summary: '覆盖人工调整、结束审批、流程终止和解除超时挂起。',
                citation_label: '[KB] CNT-0005',
              },
            ],
            apcom_docs: [],
            ticket_case: [
              {
                content_id: 'TICKET-MYPROJECT-1001',
                source_kind: 'ticket_case',
                name: 'MYPROJECT-1001 流程监控人工干预案例',
                summary: '客户通过流程监控人工调整审批人。',
                citation_label: '[TICKET] MYPROJECT-1001',
              },
            ],
          },
        }),
      });
    });

    await page.route('**/api/kb/content/DOC-FLOW-1', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          content_id: 'DOC-FLOW-1',
          source_kind: 'kb_local',
          name: 'CNT-0005 帮助文档-流程监控',
          summary: '覆盖人工调整、结束审批、流程终止和解除超时挂起。',
          raw_content: '流程监控支持人工调整、结束审批、流程终止和解除超时挂起。',
          citation_label: '[KB] CNT-0005',
          topic_names: ['流程监控'],
          keywords: ['流程监控', '人工调整', '结束审批'],
        }),
      });
    });

    await page.goto('/kb.html');
    await page.fill('#searchInput', '流程监控中如何干预流程');
    await page.getByRole('button', { name: '提问' }).click();

    await expect(page.locator('#overviewPanel')).toContainText('operation');
    await expect(page.locator('#overviewPanel')).toContainText('ticket_case 仅作侧证');
    await expect(page.locator('#primaryMaterialsPanel')).toContainText('CNT-0005 帮助文档-流程监控');
    await expect(page.locator('#primaryMaterialsPanel')).not.toContainText('MYPROJECT-1001');

    await expect(page.locator('#kbDrawer')).toBeHidden();
    await page.getByRole('button', { name: '深层证据' }).click();
    await expect(page.locator('#kbDrawer')).toBeVisible();
    await expect(page.locator('#kbDrawerTitle')).toContainText('深层证据');
    await expect(page.locator('#kbDrawerBody')).toContainText('MYPROJECT-1001');
  });

  test('kb 页面应通过抽屉承载第四层内容', async ({ page }) => {
    await page.route('**/api/kb/qa', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          answer_text: '流程监控支持人工干预，可在监控界面执行人工调整、结束审批和流程终止。',
          answer_html: '<p>流程监控支持人工干预，可在监控界面执行人工调整、结束审批和流程终止。</p>',
          used_llm: true,
          fallback_used: false,
          topics: ['流程监控', '审批干预'],
          query_profile: {
            query_intent: 'operation',
            source_weight_strategy: '操作题优先使用 kb_local，apcom_docs 补充原理说明，ticket_case 仅作侧证。',
          },
          relevance_summary: {
            high_count: 1,
            medium_count: 1,
            low_count: 0,
          },
          ticket_summary: {
            related_count: 1,
            top_issue_keys: ['MYPROJECT-1001'],
            items: [
              {
                content_id: 'TICKET-MYPROJECT-1001',
                source_kind: 'ticket_case',
                name: 'MYPROJECT-1001 流程监控人工干预案例',
                summary: '客户通过流程监控人工调整审批人。',
                citation_label: '[TICKET] MYPROJECT-1001',
              },
            ],
          },
          primary_materials: [
            {
              content_id: 'DOC-FLOW-1',
              source_kind: 'kb_local',
              name: 'CNT-0005 帮助文档-流程监控',
              summary: '覆盖人工调整、结束审批、流程终止和解除超时挂起。',
              citation_label: '[KB] CNT-0005',
            },
          ],
          sources: [
            {
              content_id: 'DOC-FLOW-1',
              source_kind: 'kb_local',
              name: 'CNT-0005 帮助文档-流程监控',
              summary: '覆盖人工调整、结束审批、流程终止和解除超时挂起。',
              citation_label: '[KB] CNT-0005',
            },
            {
              content_id: 'TICKET-MYPROJECT-1001',
              source_kind: 'ticket_case',
              name: 'MYPROJECT-1001 流程监控人工干预案例',
              summary: '客户通过流程监控人工调整审批人。',
              citation_label: '[TICKET] MYPROJECT-1001',
            },
          ],
          source_groups: {
            kb_local: [
              {
                content_id: 'DOC-FLOW-1',
                source_kind: 'kb_local',
                name: 'CNT-0005 帮助文档-流程监控',
                summary: '覆盖人工调整、结束审批、流程终止和解除超时挂起。',
                citation_label: '[KB] CNT-0005',
              },
            ],
            apcom_docs: [],
            ticket_case: [
              {
                content_id: 'TICKET-MYPROJECT-1001',
                source_kind: 'ticket_case',
                name: 'MYPROJECT-1001 流程监控人工干预案例',
                summary: '客户通过流程监控人工调整审批人。',
                citation_label: '[TICKET] MYPROJECT-1001',
              },
            ],
          },
        }),
      });
    });

    await page.route('**/api/kb/content/DOC-FLOW-1', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          content_id: 'DOC-FLOW-1',
          source_kind: 'kb_local',
          name: 'CNT-0005 帮助文档-流程监控',
          summary: '覆盖人工调整、结束审批、流程终止和解除超时挂起。',
          raw_content: '流程监控支持人工调整、结束审批、流程终止和解除超时挂起。',
          citation_label: '[KB] CNT-0005',
          topic_names: ['流程监控'],
          keywords: ['流程监控', '人工调整', '结束审批'],
        }),
      });
    });

    await page.goto('/kb.html');
    await page.fill('#searchInput', '流程监控中如何干预流程');
    await page.getByRole('button', { name: '提问' }).click();

    await expect(page.locator('#kbDrawer')).toBeHidden();
    await page.getByRole('button', { name: '证据详情' }).click();
    await expect(page.locator('#kbDrawer')).toBeVisible();
    await expect(page.locator('#kbDrawerTitle')).toContainText('证据详情');
    await expect(page.locator('#kbDrawerBody')).toContainText('CNT-0005 帮助文档-流程监控');

    await page.getByRole('button', { name: '深层证据' }).click();
    await expect(page.locator('#kbDrawerTitle')).toContainText('深层证据');
    await expect(page.locator('#kbDrawerBody')).toContainText('MYPROJECT-1001');

    await page.locator('#kbDrawerClose').click();
    await expect(page.locator('#kbDrawer')).toBeHidden();
  });

  test('搜索页应展示知识库解答卡片并位于相关工单上方', async ({ page }) => {
    await page.goto('/search.html');

    await expect(page.locator('#searchInput')).toBeVisible();
    await page.fill('#searchInput', '流程监控如何查询未来审批人');
    await page.getByRole('button', { name: '搜索' }).click();

    const kbCard = page.locator('#kbSummaryCard');
    await expect(kbCard).toBeVisible({ timeout: 20000 });
    await expect(kbCard).toContainText('知识库解答');
    await expect(page.locator('#kbSummaryContent')).not.toHaveText('', { timeout: 10000 });

    const sourceLink = page.locator('#kbSummarySources a').first();
    await expect(sourceLink).toBeVisible();
    await expect(sourceLink).toHaveAttribute('href', 'kb.html');

    const order = await page.evaluate(() => {
      const kb = document.getElementById('kbSummaryCard');
      const resultsHeader = document.getElementById('resultTitle')?.closest('.flex.items-center.justify-between');
      if (!kb || !resultsHeader) return 'missing';
      return kb.compareDocumentPosition(resultsHeader);
    });

    expect(order & 4).toBeTruthy();
  });
});

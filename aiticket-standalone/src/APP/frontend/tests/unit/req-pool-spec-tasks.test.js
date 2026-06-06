import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

import { describe, expect, it } from 'vitest';

const frontendRoot = process.cwd();

function loadHelperModule(filePath) {
  const source = fs.readFileSync(filePath, 'utf-8');
  const context = {
    window: {},
    module: { exports: {} },
    exports: {},
    console,
  };

  vm.runInNewContext(source, context, { filename: filePath });
  return context.module.exports;
}

describe('req pool spec task helpers', () => {
  it('should group retryable failed sections by document', () => {
    const helperPath = path.join(frontendRoot, 'req-pool-spec.js');
    const helper = loadHelperModule(helperPath);

    const task = {
      documents: {
        summary: {
          sections: [
            { id: 'background_and_goal', status: 'completed' },
            { id: 'business_flow', status: 'failed' },
          ],
        },
        detail: {
          sections: [
            { id: 'feature_overview', status: 'failed' },
            { id: 'api_and_data_model', status: 'completed' },
          ],
        },
      },
    };

    expect(helper.getRetryableSections(task)).toEqual({
      summary: ['business_flow'],
      detail: ['feature_overview'],
    });
  });

  it('should render document cards with section status and errors', () => {
    const helperPath = path.join(frontendRoot, 'req-pool-spec.js');
    const helper = loadHelperModule(helperPath);

    const task = {
      status: 'partial',
      error_summary: 'summary:business_flow:bad_output',
      documents: {
        summary: {
          status: 'failed',
          sections: [
            {
              id: 'business_flow',
              title: '业务流程',
              status: 'failed',
              error: 'bad_output',
              attempts: 2,
            },
          ],
        },
        detail: {
          status: 'running',
          sections: [
            {
              id: 'feature_overview',
              title: '功能概述',
              status: 'running',
              error: '',
              attempts: 1,
            },
          ],
        },
      },
      artifacts: {
        output_files: ['V_Next-REQ-MYPROJECT-59346-test-概要需求.md'],
      },
    };

    const html = helper.renderTaskStatusPanel(task);

    expect(html).toContain('概要需求');
    expect(html).toContain('详细需求');
    expect(html).toContain('业务流程');
    expect(html).toContain('bad_output');
    expect(html).toContain('功能概述');
    expect(html).toContain('V_Next-REQ-MYPROJECT-59346-test-概要需求.md');
  });
});

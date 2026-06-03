#!/usr/bin/env node

const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);

const getArgValue = (name, fallback = '') => {
  const index = args.indexOf(name);
  if (index === -1) {
    return fallback;
  }
  return args[index + 1] || fallback;
};

const splitList = (value) => value
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean);

const projectRoot = path.resolve(getArgValue('--project', process.cwd()));
const requirementDirs = splitList(getArgValue('--requirement-dirs', 'docs/spec,docs/design'));
const excludeDirs = splitList(getArgValue('--exclude-dirs', 'docs/spec/revision,docs/design/assets'));
const agentFiles = splitList(getArgValue(
  '--agent-files',
  'AGENTS.md,CLAUDE.md,GEMINI.md,.cursor/rules/requirement-revision-guard.mdc,.windsurfrules'
));
const revisionDir = path.join(projectRoot, 'docs/spec/revision');
const scriptsDir = path.join(projectRoot, 'scripts');
const guardPath = path.join(scriptsDir, 'revision-guard.js');
const packagePath = path.join(projectRoot, 'package.json');

const normalizePath = (filePath) => filePath.split(path.sep).join('/');

const writeFile = (filePath, content) => {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, content, 'utf8');
};

const revisionGuardSource = (options) => `#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const rootDir = path.resolve(__dirname, '..');
const revisionDir = path.join(rootDir, 'docs/spec/revision');
const args = process.argv.slice(2);
const requirementDirs = ${JSON.stringify(options.requirementDirs)};
const excludeDirs = ${JSON.stringify(options.excludeDirs)};

const requiredHeadings = [
  '## 变更内容',
  '## 变更原因',
  '## 已完成任务',
  '## 未完成后续计划'
];

const execGit = (gitArgs) => {
  try {
    return execFileSync('git', ['-c', 'core.quotepath=false', ...gitArgs], {
      cwd: rootDir,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe']
    }).trim();
  } catch (error) {
    const message = error.stderr || error.message;
    throw new Error(\`执行 git 命令失败：\${message}\`);
  }
};

const hasArg = (name) => args.includes(name);

const getArgValue = (name) => {
  const index = args.indexOf(name);
  if (index === -1) {
    return '';
  }
  return args[index + 1] || '';
};

const normalizePath = (filePath) => filePath.split(path.sep).join('/');

const getDateParts = () => {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  return {
    date: \`\${now.getFullYear()}\${pad(now.getMonth() + 1)}\${pad(now.getDate())}\`,
    time: \`\${pad(now.getHours())}\${pad(now.getMinutes())}\${pad(now.getSeconds())}\`,
    display: \`\${now.getFullYear()}-\${pad(now.getMonth() + 1)}-\${pad(now.getDate())} \${pad(now.getHours())}:\${pad(now.getMinutes())}:\${pad(now.getSeconds())}\`
  };
};

const createSlug = (title) => {
  const slug = title
    .trim()
    .replace(/[\\\\/?%*:|"<>]/g, '')
    .replace(/\\s+/g, '-')
    .replace(/-+/g, '-')
    .slice(0, 48);

  return slug || '需求变更';
};

const parseNameStatus = (output) => {
  if (!output) {
    return [];
  }

  return output.split(/\\r?\\n/).filter(Boolean).map((line) => {
    const parts = line.split('\\t');
    const status = parts[0];
    const filePath = parts[parts.length - 1];
    return {
      status,
      path: normalizePath(filePath)
    };
  });
};

const hasGitHead = () => {
  try {
    execGit(['rev-parse', '--verify', 'HEAD']);
    return true;
  } catch {
    return false;
  }
};

const getChangedEntries = ({ staged }) => {
  if (staged) {
    return parseNameStatus(execGit(['diff', '--cached', '--name-status', '--']));
  }

  const tracked = hasGitHead()
    ? parseNameStatus(execGit(['diff', '--name-status', 'HEAD', '--']))
    : [];
  const untracked = execGit(['ls-files', '--others', '--exclude-standard'])
    .split(/\\r?\\n/)
    .filter(Boolean)
    .map((filePath) => ({
      status: 'A',
      path: normalizePath(filePath)
    }));

  const byPath = new Map();
  [...tracked, ...untracked].forEach((entry) => {
    byPath.set(entry.path, entry);
  });
  return [...byPath.values()];
};

const isRequirementDoc = (filePath) => {
  if (!filePath.endsWith('.md')) {
    return false;
  }
  if (excludeDirs.some((dir) => filePath === dir || filePath.startsWith(\`\${dir}/\`))) {
    return false;
  }
  return requirementDirs.some((dir) => filePath === dir || filePath.startsWith(\`\${dir}/\`));
};

const isRevisionRecord = (filePath) => {
  const basename = path.basename(filePath);
  return /^docs\\/spec\\/revision\\/\\d{8}-\\d{6}-.+\\.md$/.test(filePath)
    && basename !== 'README.md'
    && basename !== '_TEMPLATE.md';
};

const getSectionContent = (content, heading) => {
  const start = content.indexOf(heading);
  if (start === -1) {
    return '';
  }

  const sectionStart = start + heading.length;
  const rest = content.slice(sectionStart);
  const nextHeading = rest.search(/\\n## /);
  return nextHeading === -1 ? rest : rest.slice(0, nextHeading);
};

const validateRevisionFile = (filePath) => {
  const fullPath = path.join(rootDir, filePath);
  const errors = [];

  if (!fs.existsSync(fullPath)) {
    return [\`变更记录不存在或已被删除：\${filePath}\`];
  }

  const content = fs.readFileSync(fullPath, 'utf8');
  requiredHeadings.forEach((heading) => {
    if (!content.includes(heading)) {
      errors.push(\`缺少必填章节「\${heading}」：\${filePath}\`);
      return;
    }

    const sectionContent = getSectionContent(content, heading).trim();
    if (!sectionContent) {
      errors.push(\`必填章节「\${heading}」内容为空：\${filePath}\`);
    }
  });

  if (/待补充|请补充|TODO|TBD/i.test(content)) {
    errors.push(\`变更记录仍包含占位内容：\${filePath}\`);
  }

  return errors;
};

const createRevision = () => {
  const title = getArgValue('--title');
  if (!title) {
    console.error('创建变更记录失败：请通过 --title 指定变更标题。');
    process.exit(1);
  }

  const reason = getArgValue('--reason') || '请补充本次变更原因。';
  const related = getArgValue('--related') || '请补充关联需求文档路径。';
  const source = getArgValue('--source') || 'AI Agent 会话';
  const { date, time, display } = getDateParts();
  const fileName = \`\${date}-\${time}-\${createSlug(title)}.md\`;
  const fullPath = path.join(revisionDir, fileName);

  fs.mkdirSync(revisionDir, { recursive: true });
  fs.writeFileSync(fullPath, \`# 需求变更记录：\${title}

- 记录时间：\${display}
- 触发来源：\${source}
- 关联文档：\${related}
- 变更类型：需求变更
- 完成状态：部分完成

## 变更内容

- 请补充本次新增、删除、调整的需求点。

## 变更原因

- \${reason}

## 已完成任务

- 请补充本次已经完成的文档或实现任务。

## 未完成后续计划

- 请补充仍需继续推进的任务；如无后续任务，写明“暂无”及原因。

## 验证结果

- 请补充已执行的校验命令或人工检查结果。
\`, 'utf8');

  console.log(\`已创建需求变更记录：\${normalizePath(path.relative(rootDir, fullPath))}\`);
};

const checkRevision = () => {
  const entries = getChangedEntries({ staged: hasArg('--staged') });
  const requirementEntries = entries.filter((entry) => isRequirementDoc(entry.path));
  const revisionEntries = entries.filter((entry) => isRevisionRecord(entry.path));
  const addedRevisionEntries = revisionEntries.filter((entry) => entry.status.startsWith('A'));
  const errors = [];

  revisionEntries.forEach((entry) => {
    if (entry.status.startsWith('D')) {
      errors.push(\`禁止删除需求变更记录：\${entry.path}\`);
      return;
    }
    errors.push(...validateRevisionFile(entry.path));
  });

  if (requirementEntries.length > 0 && addedRevisionEntries.length === 0) {
    errors.push([
      '检测到需求文档变更，但没有新增 docs/spec/revision 变更记录。',
      \`需求文档：\${requirementEntries.map((entry) => entry.path).join(', ')}\`,
      '请运行 npm run revision:new -- --title "变更标题" --reason "变更原因"，补齐记录后再检查。'
    ].join('\\n'));
  }

  if (errors.length > 0) {
    console.error(errors.join('\\n\\n'));
    process.exit(1);
  }

  console.log('需求变更记录检查通过。');
};

if (hasArg('--create')) {
  createRevision();
} else if (hasArg('--check')) {
  checkRevision();
} else {
  console.log('用法：node scripts/revision-guard.js --create --title "标题" 或 node scripts/revision-guard.js --check');
}
`;

const readmeSource = `# 需求变更记录机制

本目录用于保存每次需求新增、需求变更、需求文档新建、需求文档修改产生的变更记录。任何 AI Agent 在会话中识别到需求变化时，必须在本目录新增一份记录，并在完成前运行 \`npm run revision:check\`。

## 触发条件

以下任一情况都必须新增变更记录：

- 用户提出新的业务需求、交互需求、接口需求、验收标准或限制条件。
- 用户调整、删除、澄清已有需求，导致需求文档或实现计划发生变化。
- 配置的需求目录下 Markdown 文档发生新增、修改或删除。
- Agent 因实现、评审或测试发现需求口径需要被补充或修正。

## 文件命名

变更记录文件必须使用以下格式：

\`\`\`text
YYYYMMDD-HHMMSS-变更标题.md
\`\`\`

## 必填内容

每份记录必须包含以下章节：

- \`## 变更内容\`：说明本次新增、删除、调整的需求点。
- \`## 变更原因\`：说明为什么发生这次变化，包括用户触发、业务背景或技术约束。
- \`## 已完成任务\`：列出本次已经完成的文档、设计、实现、测试或校验任务。
- \`## 未完成后续计划\`：列出仍需继续推进的任务；如无后续任务，必须写明暂无及原因。

## 推荐流程

1. 在识别到需求变化后，先运行 \`npm run revision:new -- --title "变更标题" --reason "变更原因"\` 生成记录草稿。
2. 完成需求文档或实现变更。
3. 回填本次变更内容、完成任务、后续计划和验证结果。
4. 运行 \`npm run revision:check\`，确保需求文档变更与新增记录成对出现。
5. 在最终回复中说明新增的变更记录路径和检查结果。

校验脚本会拒绝必填章节为空、仍包含 \`待补充\`、\`请补充\`、\`TODO\`、\`TBD\` 等占位内容的正式变更记录。
`;

const templateSource = `# 需求变更记录：变更标题

- 记录时间：YYYY-MM-DD HH:mm:ss
- 触发来源：AI Agent 会话 / 用户需求 / 评审反馈 / 测试发现
- 关联文档：docs/spec/xxx.md
- 变更类型：新需求 / 需求变更 / 需求文档新建 / 需求文档变更 / 需求治理
- 完成状态：已完成 / 部分完成 / 未开始

## 变更内容

- 说明本次新增、删除、调整的需求点。

## 变更原因

- 说明为什么发生这次变化，包括用户触发、业务背景或技术约束。

## 已完成任务

- 列出本次已经完成的文档、设计、实现、测试或校验任务。

## 未完成后续计划

- 列出仍需继续推进的任务；如无后续任务，写明暂无及原因。

## 验证结果

- 记录已执行的校验命令或人工检查结果。
`;

const agentRuleBlock = `<!-- requirement-revision-guard:start -->
## 需求变更记录强制机制

任何 AI Agent 在会话中识别到新的需求、需求变更、需求澄清、验收标准变化、限制条件变化时，必须把本次变化记录到 \`docs/spec/revision/\`。该规则适用于 Codex、Claude Code、Cursor、Gemini CLI、Windsurf 以及其他读取项目规则文件的 Agent 产品。

### 触发条件

- 用户明确提出新的功能、交互、接口、数据、权限、验收或限制要求。
- 用户对已有需求做补充、删除、改口径或优先级调整。
- Agent 在实现、评审或测试中发现需求描述需要补充或修正。
- 配置的需求目录下 Markdown 文档发生新增、修改或删除。

### Agent 必执行流程

1. 识别到需求变化后，先运行 \`npm run revision:new -- --title "变更标题" --reason "变更原因"\` 创建变更记录草稿。
2. 完成需求文档、设计文档或代码实现变更后，回填本次记录。
3. 记录中必须写清楚：变更内容、变更原因、已完成任务、未完成后续计划。
4. 若没有未完成任务，必须在“未完成后续计划”中写明暂无及原因，禁止留空。
5. 结束会话前必须运行 \`npm run revision:check\`；若准备提交暂存区内容，必须运行 \`npm run revision:check:staged\`。
6. 最终回复必须说明新增的变更记录文件路径和检查结果。
<!-- requirement-revision-guard:end -->
`;

const cursorRuleSource = `---
description: 需求变更记录强制机制
alwaysApply: true
---

${agentRuleBlock}`;

const upsertBlock = (filePath, block) => {
  const start = '<!-- requirement-revision-guard:start -->';
  const end = '<!-- requirement-revision-guard:end -->';
  const current = fs.existsSync(filePath) ? fs.readFileSync(filePath, 'utf8') : '# 项目 AI Agent 规则\n';

  if (current.includes(start) && current.includes(end)) {
    const next = current.replace(new RegExp(`${start}[\\s\\S]*?${end}`), block.trim());
    writeFile(filePath, `${next.trim()}\n`);
    return;
  }

  writeFile(filePath, `${current.trim()}\n\n${block}`);
};

const updateAgentRules = () => {
  agentFiles.forEach((relativePath) => {
    const filePath = path.join(projectRoot, relativePath);
    if (relativePath.endsWith('.mdc')) {
      writeFile(filePath, cursorRuleSource);
      return;
    }
    upsertBlock(filePath, agentRuleBlock);
  });
};

const updatePackageScripts = () => {
  if (!fs.existsSync(packagePath)) {
    return false;
  }

  const pkg = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
  pkg.scripts = pkg.scripts || {};
  pkg.scripts['revision:new'] = 'node scripts/revision-guard.js --create';
  pkg.scripts['revision:check'] = 'node scripts/revision-guard.js --check';
  pkg.scripts['revision:check:staged'] = 'node scripts/revision-guard.js --check --staged';
  writeFile(packagePath, `${JSON.stringify(pkg, null, 2)}\n`);
  return true;
};

if (!fs.existsSync(projectRoot)) {
  console.error(`目标项目不存在：${projectRoot}`);
  process.exit(1);
}

writeFile(guardPath, revisionGuardSource({ requirementDirs, excludeDirs }));
writeFile(path.join(revisionDir, 'README.md'), readmeSource);
writeFile(path.join(revisionDir, '_TEMPLATE.md'), templateSource);
updateAgentRules();
const hasPackageJson = updatePackageScripts();

console.log(`已安装通用 AI Agent 需求变更记录机制：${normalizePath(projectRoot)}`);
console.log(`- ${normalizePath(path.relative(projectRoot, guardPath))}`);
console.log(`- ${normalizePath(path.relative(projectRoot, path.join(revisionDir, 'README.md')))}`);
console.log(`- ${normalizePath(path.relative(projectRoot, path.join(revisionDir, '_TEMPLATE.md')))}`);
agentFiles.forEach((relativePath) => {
  console.log(`- ${normalizePath(relativePath)}`);
});
if (hasPackageJson) {
  console.log('- package.json scripts: revision:new, revision:check, revision:check:staged');
} else {
  console.log('- 未发现 package.json，请直接使用 node scripts/revision-guard.js --check');
}

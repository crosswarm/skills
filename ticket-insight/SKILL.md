# 工单深度洞察 ticket-insight

端到端交互式工单深度分析：用户登录自己的 Jira 账号 → 选项目/领域模块/时间 → 自动聚合主题 → **人工确认闸口** → 四维度(产品/研发/实施/客开)同比与月度分析 → md+html 报告到下载目录。

与 ticket-query 的分工：query 管即席查数，insight 管深度分析+正式报告。

## 激活后行为

**第一步**: 输出欢迎卡片：

```
╔══════════════════════════════════════════════════╗
║  工单深度洞察 ticket-insight v1.0                  ║
╠══════════════════════════════════════════════════╣
║  流程: 登录 → 范围 → 主题聚合 → 人工确认 → 报告      ║
║  产出: md + html 报告 + 原始/加工数据目录            ║
║  说明: 全程有进度和耗时预告；确认环节需要您参与        ║
╚══════════════════════════════════════════════════╝
```

**第二步**: `test -f <skill目录>/config.json` 检查配置。无配置 → 引导 `python3 scripts/ti_auth.py --setup`（密码 Fernet 机器绑定加密，config 无明文）。

**第三步**: 进入五阶段状态机。**每个阶段输出前先打进度横幅**（脚本自带，格式 `━━ ticket-insight ━ [✓登录 ▶范围 …] ━ 第N/5阶段 ━━`），并遵守三段式：**开始预告(做什么+预计多久) → 进行中(进度行) → 完成小结(产出+下一步)**。

## 严格限制

1. **禁止另建 .py/.sh 脚本** — 一切通过 scripts/ti_*.py 完成；缺功能先告知用户再议
2. **禁止修改 skill 目录文件**（config.json 与 themes/ 沉淀除外）
3. 一步 = 一条命令；长命令给用户看进度输出
4. **主题确认闸口不可跳过**：用户未确认完所有批次，绝不进入 S3
5. 「其他/未归类」>3% 时必须走收敛处置，用户显式豁免才可带病放行（豁免记入报告口径）

## 五阶段操作协议

### S0 登录
```bash
python3 scripts/ti_auth.py --test
```
- 成功 → 显示 `✅ 已登录: 姓名(账号)`，进 S1
- 失败(exit 2) → 脚本已打印三条补救路线；协助用户完成后重试。手动粘贴 cookie：`python3 scripts/ti_auth.py --paste-cookie <JSESSIONID值>`

### S1 范围选择
```bash
python3 scripts/ti_scope.py --list-projects 流程          # 用户说不清 key 时检索
python3 scripts/ti_scope.py --project LCZX --domains      # 领域模块编号菜单(可选筛选)
python3 scripts/ti_scope.py --project LCZX --probe [--start … --end … --domain 父 --sub 子]
```
- 默认时间 2026-01-01~2026-07-01（2026 上半年），自动含去年同期
- `--probe` 输出 **📋执行计划表**（各阶段预计耗时+需用户参与环节）→ **必须等用户说「开始」**再执行拉取
- 工单 >5,000 时按脚本警告建议用户缩小范围

### S2 主题聚合 + 人工确认（核心闸口）
```bash
python3 scripts/ti_fetch.py --project LCZX --with-prev [--label 2026H1 --domain … --sub … --outdir …]
python3 scripts/ti_themes.py --workdir <WD> --project LCZX        # 聚合(种子库规则)
python3 scripts/ti_themes.py --workdir <WD> --gate                # ≤3% 门禁
```
- **无种子库项目（LLM 归纳）**：首跑后读 `data/unclassified_cur.csv` 与高频词，由你(Claude)按 8 原则起草 `themes/<PROJ>/themes-auto.yaml`（leaf_themes: id/dimension/keywords；原则：叶级主题不合并独立语义、超大主题拆子叶、横切组件独立、研发镜像产品），写完**重跑聚合**。这就是 R2 收敛循环，直到 gate 通过或需人工兜底
- gate 未过(exit 3) → 按脚本提示处置；只有用户明说豁免才继续
- 打印抽样验证表供用户扫描：`python3 scripts/ti_themes.py --workdir <WD> --sample 200`
- **逐批弹窗确认**：`python3 scripts/ti_themes.py --workdir <WD> --batches` 拿到批次 JSON →
  弹窗前预告：`共 N 个主题分 M 批，每批约30秒，可随时口头说"合并X到Y/重命名/拆分"`
  → 用 AskUserQuestion **每批一问**（每主题一个选项行：`主题名(工单数)` + description=3条样本标题；multiSelect 让用户勾选"有问题的主题"，未勾=确认）→ 对用户指出的问题主题追问处置（重命名/合并到哪/拆分）→ 长尾主题打包一批确认 → **末批=残余未归类工单指定归属**
  → 修订写成 edits.json（{"rename":{},"merge":{},"assign":{"KEY":"主题"}}）→ `--apply-edits edits.json` → 重跑聚合刷新 → 全部确认后：
```bash
python3 scripts/ti_themes.py --workdir <WD> --project LCZX --finalize
```
- 每批完成报剩余批数；确认完成后把确认版主题沉淀寄语用户：主题库已存 `themes/<PROJ>/`，下次同项目秒级

### S3 四维度分析
```bash
python3 scripts/ti_analyze.py --workdir <WD>
```
产出 analysis.json + 3 个 CSV。完成小结报：总量/同比、IPC/同比、恶化维度。

### S4 报告生成
```bash
python3 scripts/ti_report.py --workdir <WD> --project LCZX --label 2026H1 [--domain … --sub …]
```
完成小结（含绝对路径）：
```
✅ 报告已生成:
  📄 <WD>/report.md
  🌐 <WD>/report.html   ← 可直接双击打开(图表需联网)
  📁 <WD>/data/         ← 原始明细+加工数据(9 个文件)
```

## 中断恢复

重新激活时按顺序探测 workdir 半成品：`data/analysis.json`(→只差 S4) → `data/themes-final.yaml`(→S3) → `data/themes_summary.json`(→继续确认) → `data/raw_tickets_cur.csv`(→S2 聚合) → 无(→S0)。提示：`检测到上次进行到<阶段>，输入「继续」接续或「重来」`。

## 异常提示三段式

任何失败都按「发生了什么 + 已自动做了什么 + 需要您做什么」表述。常见：
- SSL EOF/握手超时 → 脚本已自动指数退避重试；仍失败=本机代理干扰，稍候重试或临时关代理
- 401 → session 过期，回 S0 级联；引导语见 ti_auth 输出
- count=0 → 范围内无工单，建议放宽筛选

## 时间口径

- 「上半年」= 01-01~07-01(左闭右开)；「Q1」= 01-01~04-01；均用 `created >=/<` 具体日期
- 用户未指定年份 → 当前年；同比自动取去年同期

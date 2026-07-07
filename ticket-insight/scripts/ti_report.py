#!/usr/bin/env python3
"""ticket-insight S4 报告生成: report.md + report.html(单文件, ECharts CDN, 断网降级表格)
输入: workdir/data/analysis.json (+themes_summary.json)
用法: python3 ti_report.py --workdir DIR --project LCZX --label 2026H1 [--domain 父 --sub 子]
"""
import argparse, html, json, sys
from datetime import date
from pathlib import Path
from ti_common import clear_active, project_cn, read_state, stage_at_least, write_state

ARROW = lambda v: ('—' if v is None else (f'🔴▲{v}%' if v > 0 else f'🟢▼{abs(v)}%' if v < 0 else '0%'))

def md_table(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join(['---'] * len(headers)) + '|']
    out += ['| ' + ' | '.join(str(c) for c in r) + ' |' for r in rows]
    return '\n'.join(out)

INSIGHTS_PLACEHOLDER = ('> （本次未生成智能总结——可要求 Claude 基于 data/analysis.json 按 '
                        'references/insights-template.md 协议撰写 data/insights.md 后重跑报告。）')

def disp(t: dict) -> str:
    """主题显示名: Agent 命名的 label 优先, 回退机械 id"""
    return t.get('label') or t.get('theme') or ''

def validate_insights(text: str, themes_doc: dict, analysis: dict) -> tuple[list, list]:
    """防幻觉校验: 返回(未知主题名列表→阻断, 未核对数字列表→警告)。合法名=主题 id ∪ Agent label"""
    import re
    real = {t['theme'] for t in themes_doc.get('themes', [])}
    for d in analysis.get('dimensions', []):        # 叠加 Agent 命名的 label(一级+二级)
        for t in d.get('top_themes', []):
            if t.get('label'):
                real.add(t['label'])
            for s in (t.get('sub') or []):
                real.add(s['label'])
    stop = '\\s，。;；:：()（）"\'`*【】\\[\\]|,.'
    mentioned = set(re.findall(rf'[PRIK]-[^{stop}]+', text)) | set(re.findall(r'其他-[一-鿿]+', text))
    unknown = sorted({m.strip('*`') for m in mentioned} - real)
    valid_nums = set()
    for t in themes_doc.get('themes', []):
        valid_nums.update([t['n'], t['cust']])
    o = analysis.get('overall', {})
    for s in (o.get('cur', {}), o.get('prev', {})):
        valid_nums.update([s.get('n'), s.get('cust')])
    for d in analysis.get('dimensions', []):
        valid_nums.update([d['cur']['n'], d['cur']['cust'], d['prev']['n'], d['prev']['cust']])
    for m in analysis.get('monthly', []):
        valid_nums.update([m['n'], m['cust'], m['prev_n']])
    for c in analysis.get('key_customers', []):
        valid_nums.add(c.get('n'))
    for d in analysis.get('dimensions', []):
        valid_nums.add((d.get('tail') or {}).get('n'))   # 长尾合并聚合计数(Agent 合并主题用)
        for t in d.get('top_themes', []):                # 二级子主题计数(暂禁, sub 恒空; 保留兼容)
            for s in (t.get('sub') or []):
                valid_nums.add(s.get('n'))
    nums = {int(n.replace(',', '')) for n in re.findall(r'(\d[\d,]*)\s*单', text)}
    unverified = sorted(n for n in nums if n not in valid_nums and n >= 10)
    return unknown, unverified

def coverage_check(insights_text: str, analysis: dict) -> list:
    """软校验: 各业务维度智能建议覆盖率(段内『占本维度 %』之和)应≥75%; 返回低于阈值的 [(维度名, 覆盖%)]"""
    import re
    low = []
    dim_names = {d['name'] for d in analysis.get('dimensions', []) if d['dim'] != 'X'}
    for p in re.split(r'\n(?=##\s)', insights_text):
        m = re.match(r'##\s*(.+?)维度总结', p.strip())
        if not m or m.group(1) not in dim_names:
            continue
        pct = sum(int(x) for x in re.findall(r'占本维度\s*(\d+)\s*%', p))
        if pct < 75:
            low.append((m.group(1), pct))
    return low

def load_insights(wd: Path, force: bool, themes_doc: dict, analysis: dict) -> str:
    p = wd / 'data' / 'insights.md'
    if not p.exists():
        print('ℹ️ 未找到 data/insights.md，报告的智能总结章将显示占位提示')
        return INSIGHTS_PLACEHOLDER
    text = p.read_text(encoding='utf-8').strip()
    unknown, unverified = validate_insights(text, themes_doc, analysis)
    if unverified:
        print(f'⚠️ insights 中 {len(unverified)} 个数字未能在数据中核对到(可能为估算值, 请确认已标注): {unverified[:8]}')
    low_cov = coverage_check(text, analysis)
    if low_cov:
        print('⚠️ 以下业务维度智能建议覆盖率 <75%（请补足主要问题或加一条业务命名的"长尾合并"主题）: '
              + '、'.join(f'{n}维度 {p}%' for n, p in low_cov))
    if unknown:
        print(f'❌ insights 引用了不存在的主题名: {unknown}')
        if not force:
            print('   已阻断报告生成(防幻觉)。请修正 data/insights.md 后重跑, 或 --force 越过。')
            sys.exit(6)
        print('   --force 已越过(风险自担)')
    return text

def parse_theme_actions(insights_text: str) -> dict:
    """从按维度结构的 insights.md 解析每个主题的动作建议:
    {theme: {'action': 解决措施, 'effect': 解决效果预估}}"""
    import re
    if not insights_text:
        return {}
    out = {}
    for it in re.split(r'\n(?=\s*\d+\.\s*\*\*)', insights_text):
        h = re.match(r'\s*\d+\.\s*\*\*(.+?)\*\*', it)
        if not h:
            continue
        act = re.search(r'解决措施\**[:：]\s*(.+)', it)
        eff = re.search(r'解决效果预估\**[:：]\s*(.+)', it)
        out[h.group(1).strip()] = {'action': (act.group(1).strip('*` ') if act else ''),
                                   'effect': (eff.group(1).strip('*` ') if eff else '')}
    return out

def dim_summary(d, theme_actions, md=True):
    """维度小结: 该维度主要问题(top主题) + 指引到各主题动作建议与智能总结章"""
    probs = [disp(t) for t in d.get('top_themes', [])[:3]]
    prob_txt = '、'.join(probs) if probs else '无显著集中主题'
    direction = '恶化' if (d['yoy_n'] or 0) > 10 else ('收敛' if (d['yoy_n'] or 0) < -10 else '基本持平')
    has_act = any(disp(t) in theme_actions for t in d.get('top_themes', []))
    tail = '各主题动作建议见下表；整体措施与效果预估见「智能总结与改进建议」章。' if has_act \
        else '整体措施与效果预估见「智能总结与改进建议」章。'
    body = (f"本维度 {d['cur']['n']:,} 单（同比 {ARROW(d['yoy_n'])}，{direction}）。"
            f"**主要问题**集中在 {prob_txt}。{tail}")
    return f"> **维度小结**：{body}" if md else body

def build_md(a, meta):
    o, dims = a['overall'], [d for d in a['dimensions'] if d['dim'] != 'X']
    x = next((d for d in a['dimensions'] if d['dim'] == 'X'), None)
    top_theme = max((t for d in dims for t in d['top_themes']), key=lambda t: t['n'], default=None)
    worst = max(dims, key=lambda d: (d['yoy_n'] if d['yoy_n'] is not None else -999), default=None)
    L = [f"# {meta['project_cn']}（{meta['project']}）工单深度分析报告 · {meta['label']}",
         '',
         f"> 生成: {date.today()} · 范围: {meta['scope']} · 生成者: ticket-insight skill",
         f"> 同期对比: {', '.join(o['prev_months'][:1])}~{o['prev_months'][-1] if o['prev_months'] else '—'}"
         f" · 维度口径: 研发确认问题类型(cf10729)→产品/研发/实施/客开",
         '', '## 〇、总体概况', '',
         f"| 工单总量 | 去重客户 | IPC | 工单同比 | IPC同比 |", '|---|---|---|---|---|',
         f"| **{o['cur']['n']:,}** | {o['cur']['cust']:,} | **{o['cur']['ipc']}** "
         f"| {ARROW(o['yoy_n'])} | {ARROW(o['yoy_ipc'])} |", '']
    if top_theme:
        L.append(f"- **最大主题**: {top_theme['theme']}（{top_theme['n']} 单 / {top_theme['cust']} 客户）")
    if worst and worst['yoy_n'] is not None and worst['yoy_n'] > 0:
        L.append(f"- **最需关注维度**: {worst['name']}（工单同比 {ARROW(worst['yoy_n'])}）")
    L += ['', '## 一、核心 KPI', '',
          md_table(['指标', '本期', '同期', '同比'],
                   [['工单量', f"{o['cur']['n']:,}", f"{o['prev']['n']:,}", ARROW(o['yoy_n'])],
                    ['去重客户', f"{o['cur']['cust']:,}", f"{o['prev']['cust']:,}", ARROW(o['yoy_cust'])],
                    ['IPC(每客户问题数)', o['cur']['ipc'], o['prev']['ipc'], ARROW(o['yoy_ipc'])]]),
          '', '## 二、月度趋势', '',
          md_table(['月份', '工单', '客户', 'IPC', '同期工单', '同比', '环比'],
                   [[m['month'], m['n'], m['cust'], m['ipc'], m['prev_n'],
                     ARROW(m['yoy_n']), ARROW(m['mom_n'])] for m in a['monthly']]),
          '', '## 三、主题分布（人工确认后）', '']
    all_tops = sorted((t for d in dims for t in d['top_themes']), key=lambda t: -t['score'])[:10]
    L.append(md_table(['主题', '维度', '工单', '客户', 'IPC', '复合权重'],
                      [[disp(t), next(d['name'] for d in dims if t in d['top_themes']),
                        t['n'], t['cust'], t['ipc'], t['score']] for t in all_tops]))
    L += ['', '> 复合权重 = 工单量(×1.0) + IPC(×1.5) 归一化得分，奖励覆盖面广的问题。全部主题见 data/themes-final.yaml',
          '', '## 四、智能总结与改进建议', '', meta.get('insights') or INSIGHTS_PLACEHOLDER, '',
          '## 五、四维度专项', '']
    theme_actions = parse_theme_actions(meta.get('insights'))
    for d in dims:
        L += [f"### {d['name']}维度（{d['cur']['n']:,} 单 · 同比 {ARROW(d['yoy_n'])}）", '',
              dim_summary(d, theme_actions, md=True), '',
              md_table(['指标', '本期', '同期', '同比'],
                       [['工单量', d['cur']['n'], d['prev']['n'], ARROW(d['yoy_n'])],
                        ['客户数', d['cur']['cust'], d['prev']['cust'], ARROW(d['yoy_cust'])],
                        ['IPC', d['cur']['ipc'], d['prev']['ipc'], ARROW(d['yoy_ipc'])]]), '',
              '**月度走势**: ' + ' → '.join(f"{m['month'][-2:]}月{m['n']}" for m in d['monthly']), '']
        if d['top_themes']:
            L.append('**主要问题（主题 Top）+ 动作建议**：')
            for t in d['top_themes']:
                L.append(f"- **{disp(t)}**（{t['n']} 单 / {t['cust']} 客户 / IPC {t['ipc']}）")
                # [二级子主题下钻 2026-07-07 暂禁——保留 t['sub'] 结构待启用]
                act = theme_actions.get(disp(t), {}).get('action')
                if act:
                    L.append(f"  - 💡 动作建议: {act}")
                for tk in t['typical'][:2]:
                    L.append(f"  - 典型: `{tk['key']}` {tk['summary']}（{tk['customer']}）")
            tail = d.get('tail') or {}
            if tail.get('n_themes'):
                L.append(f"- └ 其余 {tail['n_themes']} 个长尾主题合计 {tail['n']} 单"
                         f"（占本维度 {tail['share']*100:.0f}%，其合并分析见「智能总结与改进建议」章）")
    if x:
        L += [f"### 其他/排除（{x['cur']['n']} 单, 占比 {x['cur']['n']/max(o['cur']['n'],1)*100:.1f}%）",
              '> 含真无效(废弃/无法复现/重复)与无法归类工单；治理目标 ≤3%。', '']
    # 六、重点客户（独立成章, 不再各维度重复分析）
    if a.get('key_customers'):
        L += ['## 六、重点客户 Top10', '',
              md_table(['客户', '工单数', '维度分布', '主要问题'],
                       [[c['customer'], c['n'], ' / '.join(c['dims']), ' / '.join(c['top_themes'])]
                        for c in a['key_customers']]),
              '', '> 跨维度合并统计；维度分布=该客户工单在四维度的分布，主要问题=其 Top3 主题。', '']
    L += ['## 附、口径说明与数据目录', '']
    L.append(f"- 知识库: {meta.get('kb') or '未连接（纯数据分析）'} · 报告语言: {meta.get('lang', 'zh-CN')}")
    for c in a['caveats']:
        L.append(f'- ⚠️ {c}')
    L += ['- IPC = 工单数 ÷ 去重客户数（月度用当月去重）；维度映射基于研发确认问题类型 + 解决方案语义层修正',
          '- 主题结构经人工逐批确认（ticket-insight S2 闸口）',
          '', '**数据目录（正文未展开的全量数据）**：', '```',
          'data/raw_tickets_cur.csv        本期原始明细',
          'data/raw_tickets_prev.csv       同期原始明细',
          'data/theme_ticket_map_*.csv     工单↔主题↔维度映射(加工)',
          'data/themes-final.yaml          确认后主题结构',
          'data/themes_summary.json        主题汇总(含样本)',
          'data/dimension_summary.csv      维度汇总', 'data/dimension_monthly.csv      维度月度',
          'data/key_customers.csv          重点客户', 'data/analysis.json              全量分析结果', '```']
    return '\n'.join(L)

HTML_TPL = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>
<style>
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:1080px;margin:0 auto;padding:24px;color:#222;line-height:1.6}}
h1{{border-bottom:3px solid #2563eb;padding-bottom:8px}} h2{{border-left:4px solid #2563eb;padding-left:10px;margin-top:36px}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left}} th{{background:#f3f6fc}}
.cards{{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0}}
.card{{flex:1;min-width:150px;background:#f3f6fc;border-radius:10px;padding:14px;text-align:center}}
.card .v{{font-size:26px;font-weight:700;color:#2563eb}} .card .l{{font-size:12px;color:#666}}
.up{{color:#dc2626}} .down{{color:#16a34a}} .chart{{width:100%;height:340px;margin:10px 0}}
.note{{background:#fffbeb;border-left:4px solid #f59e0b;padding:8px 12px;font-size:13px;margin:8px 0}}
noscript .chart{{display:none}}</style></head><body>
<h1>{title}</h1>
<p style="color:#666">生成: {today} · 范围: {scope} · ticket-insight skill · 图表需联网加载 ECharts，离线时看表格</p>
<h2>总体概况</h2><div class="cards">{cards}</div>
<h2>月度趋势</h2><div id="c_trend" class="chart"></div>{trend_table}
<h2>维度分布与同比</h2><div style="display:flex;gap:10px;flex-wrap:wrap">
<div id="c_pie" class="chart" style="flex:1;min-width:320px"></div>
<div id="c_dim" class="chart" style="flex:1;min-width:320px"></div></div>{dim_table}
<h2>主题 Top10（复合权重）</h2><div id="c_theme" class="chart" style="height:400px"></div>{theme_table}
<h2>智能总结与改进建议</h2>
<div id="ins" style="background:#f8fafc;border-radius:10px;padding:6px 18px"><pre id="ins_pre" style="white-space:pre-wrap;font-family:inherit">{insights_pre}</pre></div>
<h2>四维度专项</h2>{dim_sections}
<h2>重点客户 Top10</h2>{key_customers}
<h2>风险与口径</h2>{caveats}
<p class="note">正文不含全量工单列表；原始与加工数据见同目录 <b>data/</b> 文件夹。</p>
<script>
var A={data_json};
function E(id){{return echarts.init(document.getElementById(id))}}
try{{
E('c_trend').setOption({{tooltip:{{trigger:'axis'}},legend:{{}},xAxis:{{type:'category',data:A.months}},
 yAxis:[{{type:'value',name:'工单'}},{{type:'value',name:'IPC'}}],
 series:[{{name:'本期工单',type:'line',data:A.m_cur,smooth:true}},
         {{name:'同期工单',type:'line',data:A.m_prev,smooth:true,lineStyle:{{type:'dashed'}}}},
         {{name:'IPC',type:'line',yAxisIndex:1,data:A.m_ipc,smooth:true}}]}});
E('c_pie').setOption({{title:{{text:'维度占比',left:'center'}},tooltip:{{}},
 series:[{{type:'pie',radius:['35%','65%'],data:A.dim_pie,label:{{formatter:'{{b}}: {{c}} ({{d}}%)'}}}}]}});
E('c_dim').setOption({{title:{{text:'维度同比%',left:'center'}},tooltip:{{}},xAxis:{{type:'category',data:A.dim_names}},
 yAxis:{{type:'value'}},series:[{{type:'bar',data:A.dim_yoy.map(function(v){{return{{value:v,itemStyle:{{color:v>0?'#dc2626':'#16a34a'}}}}}})}}]}});
E('c_theme').setOption({{tooltip:{{}},grid:{{left:220}},xAxis:{{type:'value'}},
 yAxis:{{type:'category',data:A.t_names,inverse:true}},series:[{{type:'bar',data:A.t_counts}}]}});
}}catch(e){{console.log('图表加载失败(可能离线):',e)}}
try{{ // 智能总结 md 渲染: marked+DOMPurify 双重处理; 离线/失败保留 <pre> 降级
if(window.marked&&window.DOMPurify){{
  document.getElementById('ins').innerHTML=DOMPurify.sanitize(marked.parse(A.insights_md));
}}}}catch(e){{console.log('insights 渲染降级为纯文本:',e)}}
</script></body></html>"""

def h_table(headers, rows):
    th = ''.join(f'<th>{html.escape(str(h))}</th>' for h in headers)
    trs = ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows)
    return f'<table><tr>{th}</tr>{trs}</table>'

def h_arrow(v):
    if v is None: return '—'
    cls = 'up' if v > 0 else 'down'
    sym = '▲' if v > 0 else '▼'
    return f'<span class="{cls}">{sym}{abs(v)}%</span>'

def build_html(a, meta):
    o = a['overall']; dims = [d for d in a['dimensions'] if d['dim'] != 'X']
    cards = ''.join(f'<div class="card"><div class="v">{v}</div><div class="l">{l}</div></div>' for v, l in [
        (f"{o['cur']['n']:,}", f"工单总量（同比 {o['yoy_n'] if o['yoy_n'] is not None else '—'}%）"),
        (f"{o['cur']['cust']:,}", f"去重客户（同比 {o['yoy_cust'] if o['yoy_cust'] is not None else '—'}%）"),
        (o['cur']['ipc'], f"IPC（同比 {o['yoy_ipc'] if o['yoy_ipc'] is not None else '—'}%）"),
        (len([t for d in dims for t in d['top_themes']]), '重点主题数'),
        (len(a['key_customers']), '重点客户数')])
    trend_table = h_table(['月份', '工单', '客户', 'IPC', '同期', '同比', '环比'],
                          [[m['month'], m['n'], m['cust'], m['ipc'], m['prev_n'],
                            h_arrow(m['yoy_n']), h_arrow(m['mom_n'])] for m in a['monthly']])
    dim_table = h_table(['维度', '本期工单', '客户', 'IPC', '同期工单', '工单同比', 'IPC同比'],
                        [[d['name'], d['cur']['n'], d['cur']['cust'], d['cur']['ipc'], d['prev']['n'],
                          h_arrow(d['yoy_n']), h_arrow(d['yoy_ipc'])] for d in a['dimensions']])
    all_tops = sorted((dict(t, dim=d['name']) for d in dims for t in d['top_themes']),
                      key=lambda t: -t['score'])[:10]
    theme_table = h_table(['主题', '维度', '工单', '客户', 'IPC', '权重'],
                          [[html.escape(disp(t)), t['dim'], t['n'], t['cust'], t['ipc'], t['score']]
                           for t in all_tops])
    theme_actions = parse_theme_actions(meta.get('insights'))
    secs = []
    for d in dims:
        rows = []
        for t in d['top_themes']:
            tk = '；'.join(f"{x['key']} {html.escape(x['summary'][:36])}" for x in t['typical'][:2])
            act = html.escape(theme_actions.get(disp(t), {}).get('action', '') or '—')
            rows.append([html.escape(disp(t)), t['n'], t['cust'], t['ipc'], f'💡 {act}' if act != '—' else '—', tk])
            # [二级子主题 mini 表 2026-07-07 暂禁——保留 t['sub'] 结构待启用]
        tail = d.get('tail') or {}
        tail_html = (f'<p class="note" style="background:#f8fafc;border-color:#94a3b8">└ 其余 {tail["n_themes"]} 个长尾主题'
                     f'合计 {tail["n"]} 单（占本维度 {tail["share"]*100:.0f}%，合并分析见「智能总结与改进建议」章）</p>'
                     if tail.get('n_themes') else '')
        summ = html.escape(dim_summary(d, theme_actions, md=False))
        secs.append(f"<h3>{d['name']}维度（{d['cur']['n']:,} 单 · 同比 {h_arrow(d['yoy_n'])}）</h3>"
                    + f'<p class="note" style="background:#eef4ff;border-color:#2563eb">📌 <b>维度小结</b>：{summ}</p>'
                    + h_table(['主要问题', '工单', '客户', 'IPC', '动作建议', '典型代表'], rows)
                    + tail_html)
    kc_section = (h_table(['客户', '工单数', '维度分布', '主要问题'],
                          [[html.escape(c['customer']), c['n'], html.escape(' / '.join(c['dims'])),
                            html.escape(' / '.join(c['top_themes']))] for c in a['key_customers']])
                  if a.get('key_customers') else '<p>无重点客户数据。</p>')
    caveats = ''.join(f'<p class="note">⚠️ {html.escape(c)}</p>' for c in a['caveats']) or '<p>无特别口径警示。</p>'
    data = {'months': [m['month'] for m in a['monthly']],
            'm_cur': [m['n'] for m in a['monthly']], 'm_prev': [m['prev_n'] for m in a['monthly']],
            'm_ipc': [m['ipc'] for m in a['monthly']],
            'dim_pie': [{'name': d['name'], 'value': d['cur']['n']} for d in a['dimensions']],
            'dim_names': [d['name'] for d in dims],
            'dim_yoy': [d['yoy_n'] or 0 for d in dims],
            't_names': [t['theme'] for t in all_tops], 't_counts': [t['n'] for t in all_tops]}
    ins = meta.get('insights') or INSIGHTS_PLACEHOLDER
    data['insights_md'] = ins
    return HTML_TPL.format(title=f"{meta['project_cn']}（{meta['project']}）工单深度分析 · {meta['label']}",
                           today=date.today(), scope=html.escape(meta['scope']), cards=cards,
                           trend_table=trend_table, dim_table=dim_table, theme_table=theme_table,
                           insights_pre=html.escape(ins),
                           dim_sections=''.join(secs), key_customers=kc_section, caveats=caveats,
                           data_json=json.dumps(data, ensure_ascii=False))

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--project', required=True)
    ap.add_argument('--label', required=True)
    ap.add_argument('--domain'); ap.add_argument('--sub')
    ap.add_argument('--lang', default='zh-CN', help='报告语言(v1.1 模板仅 zh-CN, 前瞻参数)')
    ap.add_argument('--force', action='store_true', help='越过 insights 防幻觉阻断(风险自担)')
    a = ap.parse_args()
    wd = Path(a.workdir).expanduser()
    # v1.1 状态校验: 须已完成分析(同级/更早重入允许)
    st = read_state(wd)
    if not stage_at_least(st.get('stage'), 'analyzed'):
        print(f"❌ 当前阶段={st.get('stage') or '未知'}，尚未完成四维度分析。请先跑 ti_analyze.py。")
        sys.exit(5)
    analysis = json.loads((wd / 'data' / 'analysis.json').read_text(encoding='utf-8'))
    themes_doc = json.loads((wd / 'data' / 'themes_summary.json').read_text(encoding='utf-8'))
    insights = load_insights(wd, a.force, themes_doc, analysis)
    scope = f'{a.project} · {a.label}' + (f' · {a.domain}' + (f'/{a.sub}' if a.sub else '') if a.domain else '')
    kb_files = st.get('kb_files') or []
    theme2dim = {t['theme']: t['dim'] for t in themes_doc.get('themes', [])}
    proj_cn = project_cn(a.project, st.get('project_name'))   # 报告大标题中文名（兜底内建表）
    meta = {'project': a.project, 'project_cn': proj_cn, 'label': a.label, 'scope': scope, 'lang': a.lang,
            'insights': insights, 'kb': ('、'.join(kb_files) if kb_files else None),
            'theme2dim': theme2dim}
    (wd / 'report.md').write_text(build_md(analysis, meta), encoding='utf-8')
    (wd / 'report.html').write_text(build_html(analysis, meta), encoding='utf-8')
    write_state(wd, stage='reported')
    clear_active()          # 分析完成, 释放活跃槽(允许开下一个分析)
    print(f'✓ 报告已生成（html 可直接双击打开）:\n  {wd / "report.md"}\n  {wd / "report.html"}\n  {wd / "data"}/')

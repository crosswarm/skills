#!/usr/bin/env python3
"""ticket-insight S4 报告生成: report.md + report.html(单文件, ECharts CDN, 断网降级表格)
输入: workdir/data/analysis.json (+themes_summary.json)
用法: python3 ti_report.py --workdir DIR --project LCZX --label 2026H1 [--domain 父 --sub 子]
"""
import argparse, html, json
from datetime import date
from pathlib import Path

ARROW = lambda v: ('—' if v is None else (f'🔴▲{v}%' if v > 0 else f'🟢▼{abs(v)}%' if v < 0 else '0%'))

def md_table(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join(['---'] * len(headers)) + '|']
    out += ['| ' + ' | '.join(str(c) for c in r) + ' |' for r in rows]
    return '\n'.join(out)

def build_md(a, meta):
    o, dims = a['overall'], [d for d in a['dimensions'] if d['dim'] != 'X']
    x = next((d for d in a['dimensions'] if d['dim'] == 'X'), None)
    top_theme = max((t for d in dims for t in d['top_themes']), key=lambda t: t['n'], default=None)
    worst = max(dims, key=lambda d: (d['yoy_n'] if d['yoy_n'] is not None else -999), default=None)
    L = [f"# {meta['project']} 工单深度分析报告 · {meta['label']}",
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
                      [[t['theme'], next(d['name'] for d in dims if t in d['top_themes']),
                        t['n'], t['cust'], t['ipc'], t['score']] for t in all_tops]))
    L += ['', '> 复合权重 = 工单量(×1.0) + IPC(×1.5) 归一化得分，奖励覆盖面广的问题。全部主题见 data/themes-final.yaml',
          '', '## 四、四维度专项', '']
    for d in dims:
        L += [f"### {d['name']}维度（{d['cur']['n']:,} 单 · 同比 {ARROW(d['yoy_n'])}）", '',
              md_table(['指标', '本期', '同期', '同比'],
                       [['工单量', d['cur']['n'], d['prev']['n'], ARROW(d['yoy_n'])],
                        ['客户数', d['cur']['cust'], d['prev']['cust'], ARROW(d['yoy_cust'])],
                        ['IPC', d['cur']['ipc'], d['prev']['ipc'], ARROW(d['yoy_ipc'])]]), '',
              '**月度走势**: ' + ' → '.join(f"{m['month'][-2:]}月{m['n']}" for m in d['monthly']), '']
        if d['top_themes']:
            L.append('**主要问题（主题 Top）**：')
            for t in d['top_themes']:
                L.append(f"- **{t['theme']}**（{t['n']} 单 / {t['cust']} 客户 / IPC {t['ipc']}）")
                for tk in t['typical'][:2]:
                    L.append(f"  - 典型: `{tk['key']}` {tk['summary']}（{tk['customer']}）")
        if d['key_customers']:
            L += ['', '**重点客户**：',
                  md_table(['客户', '工单', '主要问题'],
                           [[c['customer'], c['n'], ' / '.join(c['top_themes'])] for c in d['key_customers']]), '']
    if x:
        L += [f"### 其他/排除（{x['cur']['n']} 单, 占比 {x['cur']['n']/max(o['cur']['n'],1)*100:.1f}%）",
              '> 含真无效(废弃/无法复现/重复)与无法归类工单；治理目标 ≤3%。', '']
    L += ['## 五、风险与建议', '']
    risky = [d for d in dims if d['yoy_n'] is not None and d['yoy_n'] > 10]
    if risky:
        for d in risky:
            tt = d['top_themes'][0] if d['top_themes'] else None
            L.append(f"- 🔴 **{d['name']}维度同比 +{d['yoy_n']}%**"
                     + (f"，头部主题「{tt['theme']}」({tt['n']}单) 建议专项治理" if tt else ''))
    else:
        L.append('- 🟢 各维度同比无显著恶化')
    improving = [d for d in dims if d['yoy_n'] is not None and d['yoy_n'] < -10]
    for d in improving:
        L.append(f"- 🟢 {d['name']}维度同比 {d['yoy_n']}%，收敛良好，维持治理节奏")
    if a['key_customers']:
        c0 = a['key_customers'][0]
        L.append(f"- 👥 头部客户「{c0['customer']}」{c0['n']} 单，建议月度复盘+知识库自助化")
    L += ['', '## 附、口径说明与数据目录', '']
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
<h2>四维度专项</h2>{dim_sections}
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
                          [[html.escape(t['theme']), t['dim'], t['n'], t['cust'], t['ipc'], t['score']]
                           for t in all_tops])
    secs = []
    for d in dims:
        rows = []
        for t in d['top_themes']:
            tk = '；'.join(f"{x['key']} {html.escape(x['summary'][:40])}" for x in t['typical'][:2])
            rows.append([html.escape(t['theme']), t['n'], t['cust'], t['ipc'], tk])
        kc = h_table(['客户', '工单', '主要问题'],
                     [[html.escape(c['customer']), c['n'], html.escape(' / '.join(c['top_themes']))]
                      for c in d['key_customers']]) if d['key_customers'] else ''
        secs.append(f"<h3>{d['name']}维度（{d['cur']['n']:,} 单 · 同比 {h_arrow(d['yoy_n'])}）</h3>"
                    + h_table(['主要问题', '工单', '客户', 'IPC', '典型代表'], rows)
                    + (f'<p><b>重点客户</b></p>{kc}' if kc else ''))
    caveats = ''.join(f'<p class="note">⚠️ {html.escape(c)}</p>' for c in a['caveats']) or '<p>无特别口径警示。</p>'
    data = {'months': [m['month'] for m in a['monthly']],
            'm_cur': [m['n'] for m in a['monthly']], 'm_prev': [m['prev_n'] for m in a['monthly']],
            'm_ipc': [m['ipc'] for m in a['monthly']],
            'dim_pie': [{'name': d['name'], 'value': d['cur']['n']} for d in a['dimensions']],
            'dim_names': [d['name'] for d in dims],
            'dim_yoy': [d['yoy_n'] or 0 for d in dims],
            't_names': [t['theme'] for t in all_tops], 't_counts': [t['n'] for t in all_tops]}
    return HTML_TPL.format(title=f"{meta['project']} 工单深度分析 · {meta['label']}",
                           today=date.today(), scope=html.escape(meta['scope']), cards=cards,
                           trend_table=trend_table, dim_table=dim_table, theme_table=theme_table,
                           dim_sections=''.join(secs), caveats=caveats,
                           data_json=json.dumps(data, ensure_ascii=False))

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--project', required=True)
    ap.add_argument('--label', required=True)
    ap.add_argument('--domain'); ap.add_argument('--sub')
    a = ap.parse_args()
    wd = Path(a.workdir).expanduser()
    analysis = json.loads((wd / 'data' / 'analysis.json').read_text(encoding='utf-8'))
    scope = f'{a.project} · {a.label}' + (f' · {a.domain}' + (f'/{a.sub}' if a.sub else '') if a.domain else '')
    meta = {'project': a.project, 'label': a.label, 'scope': scope}
    (wd / 'report.md').write_text(build_md(analysis, meta), encoding='utf-8')
    (wd / 'report.html').write_text(build_html(analysis, meta), encoding='utf-8')
    print(f'✓ 报告已生成（html 可直接双击打开）:\n  {wd / "report.md"}\n  {wd / "report.html"}\n  {wd / "data"}/')

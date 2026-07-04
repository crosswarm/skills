#!/usr/bin/env python3
"""ticket-insight S1 范围选择
用法:
  python3 ti_scope.py --list-projects [关键词]        # 检索项目（key/中文名）
  python3 ti_scope.py --project LCZX --domains        # 列领域模块两级可选值(编号菜单)
  python3 ti_scope.py --project LCZX --probe [--start 2026-01-01 --end 2026-07-01]
                      [--domain 父 [--sub 子]]        # count探针+执行计划表
"""
import argparse, json, sys
from datetime import date
from ti_common import (banner, build_jql, count_only, fmt_secs, get_cookie, jira_get, THEMES_DIR)

DEF_START, DEF_END = '2026-01-01', '2026-07-01'   # 默认 2026 上半年

def prev_year(d: str) -> str:
    y, rest = d.split('-', 1)
    return f'{int(y)-1}-{rest}'

def cmd_list_projects(cookie, kw):
    projs = jira_get('/rest/api/2/project', {}, cookie)
    hits = [p for p in projs if not kw or kw.lower() in p['key'].lower() or kw in p.get('name', '')]
    print(f'共 {len(projs)} 个项目' + (f'，匹配 "{kw}" 的 {len(hits)} 个：' if kw else '：'))
    for p in hits[:40]:
        print(f'  {p["key"]:<10} {p.get("name", "")}')
    if len(hits) > 40:
        print(f'  …还有 {len(hits)-40} 个，请加关键词缩小')

def get_domains(cookie, project):
    d = jira_get('/rest/api/2/issue/createmeta',
                 {'projectKeys': project, 'issuetypeIds': '10400',
                  'expand': 'projects.issuetypes.fields'}, cookie)
    for proj in d.get('projects', []):
        for it in proj.get('issuetypes', []):
            f = it.get('fields', {}).get('customfield_10123')
            if f:
                return [{'value': av.get('value'),
                         'children': [c.get('value') for c in av.get('children', [])]}
                        for av in f.get('allowedValues', [])]
    return []

def cmd_domains(cookie, project):
    opts = get_domains(cookie, project)
    if not opts:
        print(f'ℹ️ {project} 无领域模块(cf10123)可选值，跳过该筛选即可')
        return
    print(f'{project} 领域模块可选值（0=不筛选）：')
    print('  0. （不筛选）')
    for i, o in enumerate(opts, 1):
        kids = f' → 二级: {", ".join(o["children"][:8])}{"…" if len(o["children"])>8 else ""}' if o['children'] else ''
        print(f'  {i}. {o["value"]}{kids}')

def cmd_probe(cookie, project, start, end, domain, sub):
    print(banner(1))
    jql_cur = build_jql(project, start, end, domain, sub)
    jql_prev = build_jql(project, prev_year(start), prev_year(end), domain, sub)
    n_cur = count_only(jql_cur, cookie)
    n_prev = count_only(jql_prev, cookie)
    pages = -(-n_cur // 100) + -(-n_prev // 100)
    t_fetch = pages * 1.2
    has_seed = (THEMES_DIR / project).exists()
    t_theme = 10 if has_seed else max(60, (n_cur / 1000) * 120)
    n_themes_est = max(8, min(40, n_cur // 150))
    scope = f'{project} · {start}~{end}' + (f' · {domain}' + (f'/{sub}' if sub else '') if domain else '')
    print(f'📋 执行计划（{scope} · {n_cur:,}单 + 同期 {n_prev:,}单）')
    print('┌ 阶段          预计耗时     说明')
    print(f'│ 数据拉取       ~{fmt_secs(t_fetch):<8} {pages} 页×100条/页，含网络重试冗余')
    if has_seed:
        print(f'│ 主题聚合       ~10 秒      命中 {project} 主题种子库（纯规则）')
    else:
        print(f'│ 主题聚合       ~{fmt_secs(t_theme):<8} 无种子库，需 LLM 归纳主题草案')
    print(f'│ 人工确认       2~5 分钟    预计约 {n_themes_est} 个主题分批弹窗，需要您逐批确认')
    print('│ 四维度分析     ~15 秒      本地计算')
    print('│ 报告生成       ~10 秒      md+html+data目录')
    total = t_fetch + t_theme + 25 + 180
    print(f'└ 合计          ~{fmt_secs(total)}（其中需您参与: 确认环节）')
    if n_cur > 5000:
        print(f'⚠️ 本期工单量 {n_cur:,} 较大，建议缩小时间范围或按领域模块分批')
    if n_cur == 0:
        print('⚠️ 本期范围内没有工单，请调整筛选条件')
    print(json.dumps({'project': project, 'start': start, 'end': end,
                      'domain': domain, 'sub': sub,
                      'count_cur': n_cur, 'count_prev': n_prev,
                      'jql_cur': jql_cur, 'jql_prev': jql_prev,
                      'has_seed': has_seed}, ensure_ascii=False))

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--list-projects', nargs='?', const='', metavar='关键词')
    ap.add_argument('--project')
    ap.add_argument('--domains', action='store_true')
    ap.add_argument('--probe', action='store_true')
    ap.add_argument('--start', default=DEF_START)
    ap.add_argument('--end', default=DEF_END)
    ap.add_argument('--domain'); ap.add_argument('--sub')
    a = ap.parse_args()
    cookie, _, _ = get_cookie(verbose=False)
    if a.list_projects is not None:
        cmd_list_projects(cookie, a.list_projects)
    elif a.domains:
        if not a.project: sys.exit('--domains 需要 --project')
        cmd_domains(cookie, a.project)
    elif a.probe:
        if not a.project: sys.exit('--probe 需要 --project')
        cmd_probe(cookie, a.project, a.start, a.end, a.domain, a.sub)
    else:
        ap.print_help()

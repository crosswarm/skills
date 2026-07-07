#!/usr/bin/env python3
"""ticket-insight S2a 明细拉取（分页+指数退避+pkl缓存+raw CSV）
用法:
  python3 ti_fetch.py --project LCZX [--start 2026-01-01 --end 2026-07-01]
                      [--domain 父 [--sub 子]] [--label 2026H1]
                      [--with-prev] [--outdir DIR] [--no-cache]
说明:
  --with-prev 同时拉去年同期（同比必需）
  缓存 key = md5(jql+字段版本)，存 skill/.cache/，与输出目录无关
"""
import argparse, csv, hashlib, json, pickle, shutil, sys, time
from pathlib import Path
from ti_common import (CACHE_DIR, banner, build_jql, fmt_secs, get_cookie, jira_get, project_cn,
                       workdir, clear_active, get_active, read_state, set_active, stage_at_least,
                       write_state)

FIELDS_VER = 'v1'
FIELDS = ('summary,created,status,resolution,assignee,'
          'customfield_10725,customfield_10729,customfield_10402,'
          'customfield_10411,customfield_10123')
CSV_COLS = ['key', 'created', 'month', 'summary', 'customer', 'rd_type',
            'cust_type', 'solution', 'domain', 'domain_sub', 'status', 'assignee']

def _rec(issue: dict) -> dict:
    f = issue.get('fields', {})
    cust = f.get('customfield_10725')
    cust = (cust[0] if isinstance(cust, list) and cust else cust) or ''
    if isinstance(cust, dict):
        cust = cust.get('value') or cust.get('name') or ''
    rd = (f.get('customfield_10729') or {}).get('value') or '未填写'
    ct = (f.get('customfield_10402') or {}).get('value') or ''
    dom = f.get('customfield_10123') or {}
    created = (f.get('created') or '')[:10]
    return {
        'key': issue.get('key'), 'created': created, 'month': created[:7],
        'summary': (f.get('summary') or '').replace('\n', ' ').strip(),
        'customer': str(cust).strip(), 'rd_type': rd, 'cust_type': ct,
        'solution': (f.get('customfield_10411') or '').replace('\n', ' ').strip()[:500],
        'domain': (dom.get('value') if isinstance(dom, dict) else '') or '',
        'domain_sub': ((dom.get('child') or {}).get('value') if isinstance(dom, dict) else '') or '',
        'status': (f.get('status') or {}).get('name') or '',
        'assignee': (f.get('assignee') or {}).get('displayName') or '',
    }

def fetch(jql: str, cookie: str, use_cache: bool = True, tag: str = '') -> list[dict]:
    ck = CACHE_DIR / f'fetch_{hashlib.md5((jql + FIELDS_VER).encode()).hexdigest()[:16]}.pkl'
    if use_cache and ck.exists():
        recs = pickle.loads(ck.read_bytes())
        print(f'  · {tag} 命中缓存: {len(recs):,} 条 ({ck.name})')
        return recs
    recs, start_at, total, t0, last_p = [], 0, None, time.time(), 0.0
    while True:
        d = jira_get('/rest/api/2/search',
                     {'jql': jql, 'startAt': start_at, 'maxResults': 100, 'fields': FIELDS}, cookie)
        total = d.get('total', 0)
        recs.extend(_rec(i) for i in d.get('issues', []))
        start_at += 100
        now = time.time()
        if now - last_p > 5 or start_at >= total:          # 进度行(≥5s一次)
            pages, done_p = -(-total // 100), -(-min(start_at, total) // 100)
            rate = (now - t0) / max(done_p, 1)
            eta = rate * (pages - done_p)
            print(f'  · {tag} 拉取中 {done_p}/{pages} 页 · 已 {len(recs):,} 单 · 剩余约 {fmt_secs(eta)}')
            last_p = now
        if start_at >= total:
            break
    ck.write_bytes(pickle.dumps(recs))
    print(f'  · {tag} 完成: {len(recs):,} 条, 用时 {fmt_secs(time.time()-t0)} (已缓存)')
    return recs

def write_csv(recs: list[dict], path: Path):
    with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader(); w.writerows(recs)

def prev_year(d: str) -> str:
    y, rest = d.split('-', 1)
    return f'{int(y)-1}-{rest}'

def fetch_project_name(project: str, cookie: str) -> str | None:
    """/rest/api/2/project/<key> 取中文名并剥离"云平台-"前缀（供报告大标题）；失败回退 None"""
    try:
        d = jira_get(f'/rest/api/2/project/{project}', {}, cookie)
        name = (d.get('name') or '').strip()
        for pre in ('云平台-', '云平台 '):
            if name.startswith(pre):
                name = name[len(pre):].strip()
        return name or None
    except Exception:
        return None

def cmd_abort_active():
    """仅当用户明确说「终止当前分析」时调用（SKILL.md 协议约束）"""
    act = get_active()
    if not act:
        print('ℹ️ 当前没有进行中的分析')
        return 0
    wd = Path(act['workdir'])
    (wd / '.aborted').write_text('aborted by user', encoding='utf-8')
    write_state(wd, stage='aborted')
    clear_active()
    print(f"✅ 已终止分析 {act['project']}（数据保留在 {wd} 供追查；该目录不可复用，同名重开需 --wipe）")
    return 0

def guard_active(wd: Path, project: str):
    """单一活跃分析拦截：存在未完成的【其它】分析 → exit 4；.aborted 目录拒复用"""
    if (wd / '.aborted').exists():
        print(f'❌ 该 workdir 曾被终止（{wd}）。换一个 --label，或加 --wipe 确认清空后重建。')
        sys.exit(4)
    act = get_active()
    if act and act.get('workdir') != str(wd):
        st = read_state(Path(act['workdir'])).get('stage')
        if st not in ('reported', 'aborted', None):
            print(f"❌ 已有进行中的分析：{act['project']}（进行到 {st}，workdir={act['workdir']}）")
            print('   请先完成该分析；或明确说「终止当前分析」（我会执行 --abort-active）后再开新分析。')
            print('   —— 此机制防止多轮会话的数据/分析串用偏差。')
            sys.exit(4)

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--start', default='2026-01-01'); ap.add_argument('--end', default='2026-07-01')
    ap.add_argument('--domain'); ap.add_argument('--sub')
    ap.add_argument('--label', default=None, help='范围标签, 默认由日期生成 如 2026H1')
    ap.add_argument('--with-prev', action='store_true')
    ap.add_argument('--outdir'); ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--abort-active', action='store_true', help='终止当前活跃分析(仅当用户明确要求)')
    ap.add_argument('--wipe', action='store_true', help='清空曾终止的同名 workdir 后重建')
    a = ap.parse_args()
    if a.abort_active:
        sys.exit(cmd_abort_active())
    label = a.label or f"{a.start[:4]}_{a.start[5:7]}-{a.end[5:7]}"
    cookie, _, who = get_cookie(verbose=False)
    print(banner(2))
    print(f'▶ 数据拉取 · 登录者 {who}')
    wd = workdir(a.project, label, a.outdir)
    if a.wipe and (wd / '.aborted').exists():
        shutil.rmtree(wd / 'data', ignore_errors=True)
        (wd / '.aborted').unlink(missing_ok=True)
        (wd / 'data').mkdir(parents=True, exist_ok=True)
        print(f'🧹 已清空曾终止的 workdir data/：{wd}')
    guard_active(wd, a.project)
    out = {}
    cur = fetch(build_jql(a.project, a.start, a.end, a.domain, a.sub), cookie,
                not a.no_cache, tag=f'{a.start[:4]}期')
    p_cur = wd / 'data' / f'raw_tickets_cur.csv'; write_csv(cur, p_cur)
    out['cur'] = {'n': len(cur), 'csv': str(p_cur)}
    if a.with_prev:
        prev = fetch(build_jql(a.project, prev_year(a.start), prev_year(a.end), a.domain, a.sub),
                     cookie, not a.no_cache, tag='同期')
        p_prev = wd / 'data' / f'raw_tickets_prev.csv'; write_csv(prev, p_prev)
        out['prev'] = {'n': len(prev), 'csv': str(p_prev)}
    out['workdir'] = str(wd)
    scope = f'{a.project} · {a.start}~{a.end}' + (f' · {a.domain}' + (f'/{a.sub}' if a.sub else '') if a.domain else '')
    proj_cn = project_cn(a.project, fetch_project_name(a.project, cookie))   # 报告大标题用中文名
    write_state(wd, stage='fetched', project=a.project, project_name=proj_cn, label=label, scope=scope,
                fetch_ts=time.strftime('%Y-%m-%d %H:%M:%S'),
                counts={'cur': out['cur']['n'], 'prev': out.get('prev', {}).get('n')})
    set_active(wd, a.project, 'fetched')
    print(json.dumps(out, ensure_ascii=False))
    print(f'✓ 拉取阶段完成 → 下一步: python3 scripts/ti_themes.py --workdir "{wd}" --project {a.project}')

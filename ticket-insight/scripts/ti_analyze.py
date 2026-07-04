#!/usr/bin/env python3
"""ticket-insight S3 四维度分析
输入: workdir/data/theme_ticket_map_{cur,prev}.csv
输出: analysis.json + dimension_summary.csv + dimension_monthly.csv + key_customers.csv
维度口径: 产品P/研发R/实施I/客开K/其他X (classify_tickets 口径, 环境→实施保持同比可比)
IPC = 工单数 ÷ 去重客户数（月度 IPC 用当月去重客户）
"""
import argparse, csv, json
from collections import Counter, defaultdict
from pathlib import Path

DIM_NAME = {'P': '产品', 'R': '研发', 'I': '实施', 'K': '客开', 'X': '其他'}
DIMS = ['P', 'R', 'I', 'K', 'X']

def load(p: Path):
    if not p.exists():
        return []
    with open(p, newline='', encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))

def stats(rows):
    custs = {r['customer'] for r in rows if r['customer']}
    n = len(rows)
    return {'n': n, 'cust': len(custs), 'ipc': round(n / max(len(custs), 1), 2)}

def yoy(cur, prev):
    return round((cur - prev) / prev * 100, 1) if prev else None

def analyze(wd: Path):
    cur = load(wd / 'data' / 'theme_ticket_map_cur.csv')
    prev = load(wd / 'data' / 'theme_ticket_map_prev.csv')
    months = sorted({r['month'] for r in cur})
    p_months = sorted({r['month'] for r in prev})
    out = {'overall': {}, 'monthly': [], 'dimensions': [], 'key_customers': [], 'caveats': []}

    # 总体
    sc, sp = stats(cur), stats(prev)
    out['overall'] = {'cur': sc, 'prev': sp,
                      'yoy_n': yoy(sc['n'], sp['n']), 'yoy_cust': yoy(sc['cust'], sp['cust']),
                      'yoy_ipc': yoy(sc['ipc'], sp['ipc']),
                      'months': months, 'prev_months': p_months}

    # 月度（总量 + 环比 + 同比）
    by_m = defaultdict(list); by_m_prev = defaultdict(list)
    for r in cur: by_m[r['month']].append(r)
    for r in prev: by_m_prev[r['month']].append(r)
    prev_of = {m: m.replace(m[:4], str(int(m[:4]) - 1), 1) for m in months}
    last_n = None
    for m in months:
        s = stats(by_m[m])
        pn = len(by_m_prev.get(prev_of[m], []))
        row = {'month': m, **s, 'prev_n': pn, 'yoy_n': yoy(s['n'], pn),
               'mom_n': yoy(s['n'], last_n) if last_n else None}
        out['monthly'].append(row); last_n = s['n']

    # 维度
    themes_doc = json.loads((wd / 'data' / 'themes_summary.json').read_text(encoding='utf-8'))
    theme_info = {t['theme']: t for t in themes_doc['themes']}
    for d in DIMS:
        rc = [r for r in cur if r['dim'] == d]
        rp = [r for r in prev if r['dim'] == d]
        if not rc and not rp:
            continue
        sc_d, sp_d = stats(rc), stats(rp)
        dim = {'dim': d, 'name': DIM_NAME[d], 'cur': sc_d, 'prev': sp_d,
               'yoy_n': yoy(sc_d['n'], sp_d['n']), 'yoy_cust': yoy(sc_d['cust'], sp_d['cust']),
               'yoy_ipc': yoy(sc_d['ipc'], sp_d['ipc']),
               'monthly': [{'month': m, 'n': sum(1 for r in by_m[m] if r['dim'] == d)} for m in months]}
        # 主要问题 = 维度内主题 Top5（IPC 复合权重 score 排序）
        tops = sorted((t for t in themes_doc['themes'] if t['dim'] == d and not t['theme'].endswith('未归类')),
                      key=lambda t: -t['score'])[:5]
        dim['top_themes'] = []
        for t in tops:
            trs = [r for r in rc if r['theme'] == t['theme']]
            # 典型代表工单: 解决方案最完整 + 客户不重复（最多3张）
            trs_sorted = sorted(trs, key=lambda r: -len(r.get('solution') or ''))
            typical, seen_c = [], set()
            for r in trs_sorted:
                if r['customer'] in seen_c:
                    continue
                seen_c.add(r['customer'])
                typical.append({'key': r['key'], 'summary': r['summary'][:70],
                                'customer': r['customer'],
                                'solution_brief': (r.get('solution') or '')[:90]})
                if len(typical) == 3:
                    break
            dim['top_themes'].append({'theme': t['theme'], 'n': t['n'], 'cust': t['cust'],
                                      'ipc': t['ipc'], 'score': t['score'], 'typical': typical})
        # 重点客户 Top5（维度内）
        cc = Counter(r['customer'] for r in rc if r['customer'])
        dim['key_customers'] = []
        for cname, cn in cc.most_common(5):
            cthemes = Counter(r['theme'] for r in rc if r['customer'] == cname).most_common(3)
            dim['key_customers'].append({'customer': cname, 'n': cn,
                                         'top_themes': [f'{t}({k})' for t, k in cthemes]})
        out['dimensions'].append(dim)

    # 全局重点客户 Top10
    cc = Counter(r['customer'] for r in cur if r['customer'])
    for cname, cn in cc.most_common(10):
        rs = [r for r in cur if r['customer'] == cname]
        dims_c = Counter(DIM_NAME[r['dim']] for r in rs).most_common()
        themes_c = Counter(r['theme'] for r in rs).most_common(3)
        out['key_customers'].append({'customer': cname, 'n': cn,
                                     'dims': [f'{d}({k})' for d, k in dims_c],
                                     'top_themes': [f'{t}({k})' for t, k in themes_c]})

    # 口径断层自动标注
    if any(m < '2025-04' for m in p_months):
        out['caveats'].append('「环境问题」自 2025-04 起被「运维问题」取代；本分析将环境问题统一归入实施维度以保持同比可比。')
    if '2026-06' in months:
        out['caveats'].append('「研发确认问题类型=未填写」2026-06 起出现填报漏填激增；未填写工单已按主题倾向推断维度，剩余计入"其他"。')
    x = next((dm for dm in out['dimensions'] if dm['dim'] == 'X'), None)
    if x and sc['n'] and x['cur']['n'] / sc['n'] > 0.03:
        out['caveats'].append(f'"其他"维度占比 {x["cur"]["n"]/sc["n"]*100:.1f}% 超过 3% 目标，原因与处置见主题门禁记录。')

    (wd / 'data' / 'analysis.json').write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    # CSV 产物
    with open(wd / 'data' / 'dimension_summary.csv', 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['维度', '本期工单', '本期客户', '本期IPC', '同期工单', '同期客户', '同期IPC',
                    '工单同比%', '客户同比%', 'IPC同比%'])
        for dm in out['dimensions']:
            w.writerow([dm['name'], dm['cur']['n'], dm['cur']['cust'], dm['cur']['ipc'],
                        dm['prev']['n'], dm['prev']['cust'], dm['prev']['ipc'],
                        dm['yoy_n'], dm['yoy_cust'], dm['yoy_ipc']])
    with open(wd / 'data' / 'dimension_monthly.csv', 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['月份'] + [DIM_NAME[dm['dim']] for dm in out['dimensions']] + ['合计'])
        for i, m in enumerate(months):
            vals = [dm['monthly'][i]['n'] for dm in out['dimensions']]
            w.writerow([m] + vals + [sum(vals)])
    with open(wd / 'data' / 'key_customers.csv', 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['客户', '工单数', '维度分布', '主要主题'])
        for c in out['key_customers']:
            w.writerow([c['customer'], c['n'], ' / '.join(c['dims']), ' / '.join(c['top_themes'])])
    print(f"✓ 分析完成: 总量 {sc['n']:,}(同比{out['overall']['yoy_n']}%) · IPC {sc['ipc']}(同比{out['overall']['yoy_ipc']}%)"
          f" · 维度 {len(out['dimensions'])} 个 → analysis.json + 3 个 CSV")

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    a = ap.parse_args()
    analyze(Path(a.workdir).expanduser())

#!/usr/bin/env python3
"""ticket-insight S3 四维度分析
输入: workdir/data/theme_ticket_map_{cur,prev}.csv
输出: analysis.json + dimension_summary.csv + dimension_monthly.csv + key_customers.csv
维度口径(2026-07-07调整): 产品P=需求; 研发R=产品错误/数据错误/设计; 实施I=实施/应用操作;
                       客开K=客开/API; 其他X=UE/效率/安全/运维/升级/环境/无效/未填写
IPC = 工单数 ÷ 去重客户数（月度 IPC 用当月去重客户）
"""
import argparse, csv, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path
import yaml
from ti_common import read_state, stage_at_least, write_state, THEMES_DIR

DIM_NAME = {'P': '产品', 'R': '研发', 'I': '实施', 'K': '客开', 'X': '其他'}
DIMS = ['P', 'R', 'I', 'K', 'X']
# 每维度智能建议须覆盖的最小工单占比: top 主题按 n 贪心取到累计≥此值, 余下并成"长尾合并"
COVER_TARGET = 0.75
COVER_MIN_THEMES = 3   # 至少取 top3 命名主题(哪怕已达标)
COVER_MAX_THEMES = 8   # 至多取 top8(防止逐条列长尾)
# ── [二级主题下钻] 2026-07-07 用户要求暂禁; 常量/函数保留为死代码, 待启用时恢复调用点 ──
SUB_MIN_N = 100        # 触发二级下钻: 主题 ≥100 单 或
SUB_MIN_SHARE = 0.15   #             ≥本维度 15%
SUB_GROUP_MIN = 5      # 二级子群最小体量; 须能分出 ≥2 个才展示
_NOISE = re.compile(r'【[^】]*】|https?://\S+')

def load(p: Path):
    if not p.exists():
        return []
    with open(p, newline='', encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))

def load_subthemes(project: str) -> dict:
    """themes/<PROJ>/sub-themes.yaml → {labels:{id:显示名}, sub_themes:{id:[{label,keywords}]}}"""
    p = THEMES_DIR / project / 'sub-themes.yaml'
    if not p.exists():
        return {'labels': {}, 'sub_themes': {}}
    doc = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    return {'labels': doc.get('labels', {}) or {}, 'sub_themes': doc.get('sub_themes', {}) or {}}

def subcluster(trs, subdefs):
    """[二级下钻·2026-07-07暂禁] 按子主题关键词顺序匹配主题工单; 返回 [{label,n,cust}], 守卫≥2个≥SUB_GROUP_MIN。当前无调用点。"""
    groups, other = [], []
    assigned = {}
    for r in trs:
        text = _NOISE.sub(' ', (r['summary'] or '') + ' ' + (r.get('solution') or '')).lower()
        hit = None
        for sd in subdefs:
            if any(k.lower() in text for k in sd.get('keywords', [])):
                hit = sd['label']; break
        (assigned.setdefault(hit, []) if hit else other).append(r)
    def reps(rs, k=2):
        """代表工单: 解决方案最完整 + 客户不重复, 取前 k"""
        out, seen = [], set()
        for r in sorted(rs, key=lambda r: -len(r.get('solution') or '')):
            if r['customer'] in seen:
                continue
            seen.add(r['customer'])
            out.append({'key': r['key'], 'summary': (r['summary'] or '')[:50], 'customer': r['customer']})
            if len(out) == k:
                break
        return out
    for sd in subdefs:
        rs = assigned.get(sd['label'], [])
        if rs:
            groups.append({'label': sd['label'], 'n': len(rs),
                           'cust': len({r['customer'] for r in rs if r['customer']}),
                           'reps': reps(rs)})
    groups.sort(key=lambda g: -g['n'])
    big = [g for g in groups if g['n'] >= SUB_GROUP_MIN]
    if len(big) < 2:                       # 分不出 ≥2 个有意义子群 → 不下钻
        return None
    if other:
        groups.append({'label': '其他(未细分)', 'n': len(other),
                       'cust': len({r['customer'] for r in other if r['customer']}), 'reps': []})
    return groups

_STOP = {'友户通', '麻烦老师', '老师您好', '老师', '谢谢', '您好', '帮忙看', '帮忙看下', '帮忙看看',
         '请问', '如图', '问题', '这个', '需要', '为什么', '是否', '能否', '可以', '如何', '现在',
         '附件', '截图', '客户', '请老师', '麻烦', '一下', '提示', '出现', '联系电话', '手机号'}
def distinct_terms(trs, own_kws, topn=12):
    """未定义 sub_themes 时给 Agent 的区分词频提示(剔除父主题关键词+礼貌噪音后的高频 2-4 字中文词)"""
    own = {k.lower() for k in own_kws}
    w = Counter()
    for r in trs:
        t = _NOISE.sub(' ', r['summary'] or '')
        for tok in re.findall(r'[一-鿿]{2,4}', t):
            if tok.lower() not in own and tok not in _STOP and len(tok) >= 2:
                w[tok] += 1
    return [t for t, _ in w.most_common(topn)]

def stats(rows):
    custs = {r['customer'] for r in rows if r['customer']}
    n = len(rows)
    return {'n': n, 'cust': len(custs), 'ipc': round(n / max(len(custs), 1), 2)}

def yoy(cur, prev):
    return round((cur - prev) / prev * 100, 1) if prev else None

def analyze(wd: Path):
    # v1.1 状态校验: 须完成主题确认(finalize)才可分析; 同级/更早重入允许, 只拦跳级
    st = read_state(wd)
    project = st.get('project') or ''
    if not project:                    # 兜底: 从 workdir 名 ticket-insight-<PROJ>-<label> 推断
        m = re.match(r'ticket-insight-([A-Z0-9]+)-', Path(wd).name)
        project = m.group(1) if m else ''
    sub_cfg = load_subthemes(project)
    labels = sub_cfg['labels']          # 一级主题友好显示名(保留); sub_themes 二级下钻暂禁不用
    if not stage_at_least(st.get('stage'), 'confirmed'):
        print(f"❌ 当前阶段={st.get('stage') or '未知'}，主题尚未确认固化。")
        print('   发生了什么: 分析要求主题结构先经人工确认(--finalize)，防止未确认数据进入报告。')
        print('   需要您做什么: 完成确认闸口后执行 ti_themes.py --finalize，再重跑本命令。')
        sys.exit(5)
    cur = load(wd / 'data' / 'theme_ticket_map_cur.csv')
    prev = load(wd / 'data' / 'theme_ticket_map_prev.csv')
    # 数据一致性: 主题映射行数=原始明细行数(对磁盘实时计数, 防跨会话串用)
    raw_n = len(load(wd / 'data' / 'raw_tickets_cur.csv'))
    if raw_n and len(cur) != raw_n:
        print(f'❌ 数据不一致: theme_ticket_map_cur({len(cur)}) ≠ raw_tickets_cur({raw_n})。')
        print('   已自动做什么: 拒绝继续，防止旧聚合结果混入新报告。')
        print('   需要您做什么: 重跑聚合 ti_themes.py --workdir ... --project ... 后再分析。')
        sys.exit(5)
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
        # 主要问题 = 维度内主题, 按 n 降序贪心取到累计覆盖 ≥COVER_TARGET(min3/max8);
        # 覆盖集之外并成"长尾合并"(dim['tail'])——供智能总结用一条 Agent 业务命名的合并主题分析(禁"其他")
        dim_themes = sorted((t for t in themes_doc['themes'] if t['dim'] == d and not t['theme'].endswith('未归类')),
                            key=lambda t: -t['n'])
        cov_n, tops, rest = 0, [], []
        for t in dim_themes:
            take = len(tops) < COVER_MAX_THEMES and (len(tops) < COVER_MIN_THEMES
                                                     or cov_n < COVER_TARGET * max(sc_d['n'], 1))
            (tops if take else rest).append(t)
            if take:
                cov_n += t['n']
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
            entry = {'theme': t['theme'], 'label': labels.get(t['theme'], t['theme']),
                     'n': t['n'], 'cust': t['cust'], 'ipc': t['ipc'], 'score': t['score'],
                     'typical': typical, 'sub': None}   # sub 恒 None（二级下钻 2026-07-07 暂禁）
            dim['top_themes'].append(entry)
        # 长尾合并聚合（覆盖集之外的业务主题）
        rest_ids = {t['theme'] for t in rest}
        tail_rows = [r for r in rc if r['theme'] in rest_ids]
        dim['tail'] = {'n': sum(t['n'] for t in rest),
                       'cust': len({r['customer'] for r in tail_rows if r['customer']}),
                       'n_themes': len(rest),
                       'share': round(sum(t['n'] for t in rest) / max(sc_d['n'], 1), 3),
                       'themes': [{'theme': t['theme'], 'label': labels.get(t['theme'], t['theme']), 'n': t['n']}
                                  for t in rest[:12]]}
        dim['coverage'] = round(cov_n / max(sc_d['n'], 1), 3)   # top 主题覆盖本维度工单比例
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
        out['caveats'].append('「环境问题」自 2025-04 起被「运维问题」取代；二者及升级/安全均归入「其他」维度（2026-07 口径），不计入实施。')
    if '2026-06' in months:
        out['caveats'].append('「研发确认问题类型=未填写」2026-06 起出现填报漏填激增；未填写工单已按主题倾向推断维度，剩余计入"其他"。')
    x = next((dm for dm in out['dimensions'] if dm['dim'] == 'X'), None)
    if x and sc['n'] and x['cur']['n'] / sc['n'] > 0.03:
        out['caveats'].append(f'"其他"维度占比 {x["cur"]["n"]/sc["n"]*100:.1f}% 超过 3% 目标，原因与处置见主题门禁记录。')

    out['pending_subtheme'] = []          # 二级下钻 2026-07-07 暂禁
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
    write_state(wd, stage='analyzed')
    # 每维度覆盖率提示: insights 须覆盖 ≥COVER_TARGET, 未达标的维度需 Claude 用长尾合并主题补足
    print(f'📐 各业务维度 top 主题覆盖率（目标 ≥{COVER_TARGET*100:.0f}%，长尾合并见 tail）:')
    for dm in out['dimensions']:
        if dm['dim'] == 'X':
            continue
        tail = dm.get('tail', {})
        print(f"    {dm['name']}: top {len(dm['top_themes'])} 主题覆盖 {dm.get('coverage',0)*100:.0f}%"
              f" · 长尾合并 {tail.get('n_themes',0)} 主题/{tail.get('n',0)} 单")
    print(f"✓ 分析完成: 总量 {sc['n']:,}(同比{out['overall']['yoy_n']}%) · IPC {sc['ipc']}(同比{out['overall']['yoy_ipc']}%)"
          f" · 维度 {len(out['dimensions'])} 个 → analysis.json + 3 个 CSV")
    print('→ 下一步: 由 Claude 按 references/insights-template.md 协议撰写 data/insights.md(智能总结, 每维度≥75%覆盖), 再生成报告')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    a = ap.parse_args()
    analyze(Path(a.workdir).expanduser())

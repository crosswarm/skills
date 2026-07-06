#!/usr/bin/env python3
"""ticket-insight S2b/S2c 主题引擎
内建 jira-ticket-classification-best-practices 8 原则:
  ①权威字段cf10729 ②解决方案语义层(优先级0) ③SSO模板噪音剥离 ④抽样人工验证
  ⑤IPC复合权重 ⑥研发镜像产品主题 ⑦窄化排除 ⑧叶级主题(种子库)
「其他/未归类」硬指标 ≤3%: --gate 检查, 超标 exit 3(供 SKILL.md 阻断流程)。

用法:
  python3 ti_themes.py --workdir DIR --project LCZX            # 聚合(cur+prev)
  python3 ti_themes.py --workdir DIR --project LCZX --sample 200   # 抽样人工验证表
  python3 ti_themes.py --workdir DIR --batches                 # 确认批次JSON(供弹窗)
  python3 ti_themes.py --workdir DIR --apply-edits edits.json  # 应用确认修订(rename/merge/assign)
  python3 ti_themes.py --workdir DIR --gate                    # ≤3% 门禁
  python3 ti_themes.py --workdir DIR --project LCZX --finalize # 固化 themes-final.yaml
"""
import argparse, csv, json, random, re, sys
from collections import Counter, defaultdict
from pathlib import Path
import yaml
from ti_common import THEMES_DIR, OVERAGG_SHARE, read_state, stage_at_least, write_state

# ── 原则② 解决方案语义层信号（优先级0, 摘自 best-practices v1.2）────────────────
RD_FIX_SIGNALS = ['代码修复', '代码已修复', '代码中修复', '代码已经修复', '链路', '新链路', '旧链路',
                  '已修复', '已经修复', '修复了', '补丁修复', '版本修复', '已上线修复', '上线修复',
                  '高版本已支持', '集团已修复', '研发已修复', '提交研发', '提交bug', '研发同事处理']
RD_FIX_EXCLUDE = ['修复错误数据', '修复了数据', '修复脏数据']
PRODUCT_REQ_SIGNALS = ['个性化需求', '个性需求', '暂不支持', '暂时不支持', '不支持修改', '设计如此',
                       '暂无规划', '暂无此规划', '后续规划', '后续优化', '纳入需求', '纳入需求库',
                       '产品规划', '功能规划', '产品迭代']
KF_SIGNALS = ['客开实现', '二开实现', '需要客开', '需要二开', '客户化开发', '走客开', '走二开']
# ── 原则⑦ 窄化排除: 真无效信号 ─────────────────────────────────────────────────
TRULY_INVALID = ['废弃', '作废', '已废弃', '已作废', '无法复现', '无法重现', '未复现', '不能复现',
                 '非流程产品', '非流程问题', '非流程错误', '不是流程', '与流程无关', '非应用平台',
                 '非本产品', '属于业务', '单据已被删除', '单据已删除', '数据已清理', '已解决，无需',
                 '项目自行解决', '客户自己解决', '现场自行解决', '请描述清楚', '信息不全', '请补充',
                 '不清楚说的', '请提供详细', '重复提单', '重复工单', '重复问题']
# ── 原则③ SSO/账户分享模板噪音 ─────────────────────────────────────────────────
SSO_NOISE = re.compile(r'【(?:帐户|账户)分享链接】[^【]*?(?:https?://\S+)?|【SSO链接】\s*https?://\S+'
                       r'|友户通\s*https?://\S+|友费控\s*https?://\S+'
                       r'|帐户分享链接[:：]?\s*https?://\S+|sso链接[:：]?\s*https?://\S+', re.I)
URL_PAT = re.compile(r'https?://\S+')

# ── 原则① 权威字段 cf10729 → 四维度（2026-07 口径调整, 见 memory 维度映射）
#   产品P=需求/UE; 研发R=产品错误/数据错误/设计/效率; 实施I=实施/应用操作;
#   客开K=客开/API; 其他X=安全/运维/升级/环境/无效/未填写
DIM_MAP = {'需求问题': 'P', 'UE问题': 'P',
           '产品错误': 'R', '数据错误': 'R', '设计问题': 'R', '效率问题': 'R',
           '实施问题': 'I', '应用操作': 'I',
           '客开问题': 'K', 'API问题': 'K',
           '安全问题': 'X', '运维问题': 'X', '升级问题': 'X', '环境问题': 'X'}
DIM_NAME = {'P': '产品', 'R': '研发', 'I': '实施', 'K': '客开', 'X': '其他'}

def clean(s: str) -> str:
    return URL_PAT.sub(' ', SSO_NOISE.sub(' ', s or '')).lower()

class ThemeSets:
    """v1.1 加载优先级: themes-confirmed.yaml(用户确认,最前) → ported种子 → themes-auto.yaml(最后)
    同 id 去重规则: 先加载者(confirmed)完全替换后来者; 空关键词条目(manual-assign)跳过不参与聚类"""
    def __init__(self, project: str):
        d = THEMES_DIR / project
        self.has_seed = d.exists()
        self.p, self.i, self.k, self.r_fb = [], [], [], []
        self._seen = {'P': set(), 'I': set(), 'K': set(), 'R': set()}
        self.confirmed_ids = set()
        # 1) 用户确认主题(最优先)
        self._load_doc(d / 'themes-confirmed.yaml', confirmed=True)
        # 2) ported 种子
        for f, dim in (('themes-product.yaml', 'P'), ('themes-impl.yaml', 'I'),
                       ('themes-kf.yaml', 'K'), ('themes-rd-fallback.yaml', 'R')):
            self._load_doc(d / f, default_dim=dim)
        # 3) LLM 归纳沉淀(最后匹配)
        self._load_doc(d / 'themes-auto.yaml')
        lk = d / 'login-title-kws.yaml'
        self.login_kws = (yaml.safe_load(lk.read_text(encoding='utf-8')) or {}).get('keywords', []) if lk.exists() else []

    def _bucket(self, dim):
        return {'P': self.p, 'I': self.i, 'K': self.k, 'R': self.r_fb}.get(dim, self.p)

    def _load_doc(self, p: Path, confirmed: bool = False, default_dim: str = None):
        if not p.exists():
            return
        doc = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
        for t in doc.get('leaf_themes', []):
            kws = [str(k).lower() for k in t.get('keywords', [])]
            if not kws:                                   # manual-assign 等空关键词: 不参与聚类
                continue
            dim = (t.get('dimension') or default_dim or t.get('id', 'P-')[0]).upper()
            if dim not in self._seen:
                dim = 'P'
            if t['id'] in self._seen[dim]:                # 同 id: 先加载者(confirmed)已占位, 跳过
                continue
            self._seen[dim].add(t['id'])
            self._bucket(dim).append((t['id'], kws))
            if confirmed:
                self.confirmed_ids.add(t['id'])

def match(rules, text):
    for tid, kws in rules:
        if any(k in text for k in kws):
            return tid
    return None

def classify(rec: dict, ts: ThemeSets) -> tuple[str, str]:
    """→ (维度字母, 主题id)"""
    title_c, sol = clean(rec['summary']), (rec.get('solution') or '')
    sol_c = clean(sol)
    text = f'{title_c} {sol_c}'
    rd = rec.get('rd_type') or '未填写'
    # 优先级0: 语义层（原则②）
    dim = None
    if any(s in sol for s in RD_FIX_SIGNALS) and not any(s in sol for s in RD_FIX_EXCLUDE):
        dim = 'R'
    elif any(s in sol for s in PRODUCT_REQ_SIGNALS):
        dim = 'P'
    elif any(s in sol for s in KF_SIGNALS):
        dim = 'K'
    # 权威字段（原则①）
    if dim is None:
        if rd == '无效问题':                     # 原则⑦ 窄化排除
            if (not sol.strip()) or any(s in sol for s in TRULY_INVALID):
                return 'X', '排除-真无效'
            dim = 'I'
        elif rd == '未填写':
            for rules, d in ((ts.i, 'I'), (ts.p, 'P'), (ts.k, 'K')):   # 主题倾向推断
                t = match(rules, text)
                if t:
                    return d, t
            return 'X', '未填写-未归类'
        else:
            dim = DIM_MAP.get(rd, 'X')
    if dim == 'X':
        # 其他维度：安全/运维/升级/环境 保留其问题类型为主题（可分辨），未知类型入"其他-未知"
        return 'X', f'其他-{rd}' if rd and rd != '未填写' else '未填写-未归类'
    # 主题匹配（原则⑧种子库 + ⑥研发镜像产品）
    if dim == 'I':
        if ts.login_kws and any(k in title_c for k in ts.login_kws):
            return 'I', 'I-账号登录(真实)'
        t = match(ts.i, text)
        return 'I', t or 'I-未归类'
    if dim == 'P':
        t = match(ts.p, text)
        return 'P', t or 'P-未归类'
    if dim == 'R':
        t = match(ts.p, text)
        if t:
            return 'R', 'R-' + t[2:]            # 镜像: P-xxx → R-xxx
        t = match(ts.r_fb, text)
        return 'R', t or 'R-未归类'
    if dim == 'K':
        t = match(ts.k, text)
        return 'K', t or 'K-未归类'
    return 'X', '排除-其他'

# ── 主流程 ────────────────────────────────────────────────────────────────────
def load_rows(p: Path):
    with open(p, newline='', encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))

def run_classify(wd: Path, project: str):
    ts = ThemeSets(project)
    if not ts.has_seed:
        print(f'ℹ️ {project} 无主题种子库 → 需 LLM 归纳: ①先 `ti_scope.py --project {project} --seeds` '
              f'拿业务子模块 seed 候选 ②读未归类样本标题 ③按8原则+seed 起草 themes/{project}/themes-auto.yaml '
              f'(seed 只做候选命名, 关键词须来自标题且够具体, 避免过度聚合) 后重跑')
    out_all = {}
    for tag in ('cur', 'prev'):
        src = wd / 'data' / f'raw_tickets_{tag}.csv'
        if not src.exists():
            continue
        rows = load_rows(src)
        for r in rows:
            r['dim'], r['theme'] = classify(r, ts)
        dst = wd / 'data' / f'theme_ticket_map_{tag}.csv'
        with open(dst, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ['key'])
            w.writeheader(); w.writerows(rows)
        n = len(rows)
        uncls = sum(1 for r in rows if r['theme'].endswith('未归类'))
        other = sum(1 for r in rows if r['dim'] == 'X')
        out_all[tag] = {'n': n, 'unclassified': uncls, 'uncls_pct': round(uncls / n * 100, 2) if n else 0,
                        'other_dim': other, 'other_pct': round(other / n * 100, 2) if n else 0}
        print(f'[{tag}] {n:,} 单 · 主题未归类 {uncls} ({out_all[tag]["uncls_pct"]}%) '
              f'· 维度其他 {other} ({out_all[tag]["other_pct"]}%)')
    # 主题汇总（原则⑤ IPC 复合权重）
    rows = load_rows(wd / 'data' / 'theme_ticket_map_cur.csv')
    agg = defaultdict(lambda: {'n': 0, 'custs': set(), 'samples': []})
    for r in rows:
        a = agg[(r['dim'], r['theme'])]
        a['n'] += 1
        if r['customer']:
            a['custs'].add(r['customer'])
        if len(a['samples']) < 5:
            a['samples'].append({'key': r['key'], 'summary': r['summary'][:60]})
    themes = []
    max_n = max((a['n'] for a in agg.values()), default=1)
    max_ipc = max((a['n'] / max(len(a['custs']), 1) for a in agg.values()), default=1)
    for (dim, tid), a in agg.items():
        ipc = a['n'] / max(len(a['custs']), 1)
        themes.append({'dim': dim, 'theme': tid, 'n': a['n'], 'cust': len(a['custs']),
                       'ipc': round(ipc, 2),
                       'score': round(a['n'] / max_n * 1.0 + ipc / max_ipc * 1.5, 3),
                       'samples': a['samples']})
    themes.sort(key=lambda t: -t['n'])
    # 过度聚合检测：单一主题占其【维度】>OVERAGG_SHARE = 人工未细分（如 LCZX 工作流设计>60%），须再拆。
    # 绝对数量下限 floor：避免小维度里"占比高"的小样本假象（如客开维度共 11 单里某主题 4 单=36%，无意义）。
    total_cur = out_all.get('cur', {}).get('n', 0)
    floor = max(30, round(total_cur * 0.03))
    dim_tot = defaultdict(int)
    for t in themes:
        dim_tot[t['dim']] += t['n']
    overagg = []
    for t in themes:
        if t['theme'].endswith('未归类') or t['dim'] == 'X':
            continue
        share = t['n'] / max(dim_tot[t['dim']], 1)
        t['dim_share'] = round(share, 3)
        if share > OVERAGG_SHARE and t['n'] >= floor:      # 占比超阈 且 体量够大 才算过度聚合
            overagg.append({'theme': t['theme'], 'dim': t['dim'], 'n': t['n'],
                            'dim_share_pct': round(share * 100, 1),
                            'total_pct': round(t['n'] / max(total_cur, 1) * 100, 1)})
    (wd / 'data' / 'themes_summary.json').write_text(
        json.dumps({'project': project, 'stats': out_all, 'themes': themes, 'overagg': overagg},
                   ensure_ascii=False, indent=1), encoding='utf-8')
    if overagg:
        print(f'⚠️ 过度聚合 {len(overagg)} 个主题（占其维度 >{OVERAGG_SHARE*100:.0f}%，须按标题再拆）:')
        for o in overagg:
            print(f'    {o["dim"]} {o["theme"]}: {o["n"]}单 = 维度内 {o["dim_share_pct"]}%')
        print('    → 用 `--overagg` 看样本标题，拆成更细叶级主题后重跑')
    # v1.1: 用户确认主题低命中警示（固定 <3 单，不自动禁用，交确认闸口人工决定）
    if ts.confirmed_ids:
        hit = {t['theme']: t['n'] for t in themes}
        low = [(cid, hit.get(cid, 0)) for cid in sorted(ts.confirmed_ids) if hit.get(cid, 0) < 3]
        if low:
            print(f'⚠️ 用户确认主题本期低命中 {len(low)} 个（<3单，请在确认环节决定保留/调整）:')
            for cid, n in low:
                print(f'    {cid}: {n} 单')
    write_state(wd, stage='themed', confirmed_theme_ids=sorted(ts.confirmed_ids))
    # 未归类样本单独导出（供 LLM 补聚 / 人工指定）
    un = [r for r in rows if r['theme'].endswith('未归类')]
    if un:
        with open(wd / 'data' / 'unclassified_cur.csv', 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.DictWriter(fh, fieldnames=list(un[0].keys())); w.writeheader(); w.writerows(un)
    print(f'✓ 主题聚合完成: {len(themes)} 个主题 → themes_summary.json / theme_ticket_map_*.csv')
    return out_all

def cmd_sample(wd: Path, n: int):
    rows = load_rows(wd / 'data' / 'theme_ticket_map_cur.csv')
    random.seed(20260408)
    smp = random.sample(rows, min(n, len(rows)))
    dims = Counter(r['dim'] for r in rows)
    print(f'== 抽样人工验证 {len(smp)} 条（原则④, seed=20260408）==')
    print('维度分布(全量):', {DIM_NAME[d]: f'{c}({c/len(rows)*100:.0f}%)' for d, c in dims.most_common()})
    print(f'{"KEY":<12} {"维度":<4} {"主题":<26} {"研发确认":<8} 标题(截断)')
    for r in smp:
        print(f'{r["key"]:<12} {DIM_NAME[r["dim"]]:<4} {r["theme"]:<26} {r["rd_type"]:<8} {r["summary"][:42]}')

def cmd_batches(wd: Path):
    doc = json.loads((wd / 'data' / 'themes_summary.json').read_text(encoding='utf-8'))
    themes = [t for t in doc['themes'] if not t['theme'].endswith('未归类') and t['dim'] != 'X']
    tail_thr = max(3, int(doc['stats']['cur']['n'] * 0.005))
    head = [t for t in themes if t['n'] >= tail_thr]
    tail = [t for t in themes if t['n'] < tail_thr]
    un = [t for t in doc['themes'] if t['theme'].endswith('未归类')]
    batches = [head[i:i+4] for i in range(0, len(head), 4)]
    # v1.1: 用户确认主题低命中单列（<3单, 请用户决定保留/调整）
    conf_ids = set(read_state(wd).get('confirmed_theme_ids') or [])
    hit = {t['theme']: t['n'] for t in doc['themes']}
    low_conf = [{'theme': c, 'n': hit.get(c, 0)} for c in sorted(conf_ids) if hit.get(c, 0) < 3]
    print(json.dumps({'n_themes': len(themes), 'n_batches': len(batches),
                      'tail_count': len(tail), 'unclassified': un,
                      'low_hit_confirmed': low_conf,
                      'batches': batches,
                      'tail': [{'theme': t['theme'], 'dim': t['dim'], 'n': t['n']} for t in tail]},
                     ensure_ascii=False, indent=1))

def cmd_apply_edits(wd: Path, edits_path: str):
    edits = json.loads(Path(edits_path).read_text(encoding='utf-8'))
    ren, mrg, asg = edits.get('rename', {}), edits.get('merge', {}), edits.get('assign', {})
    for tag in ('cur', 'prev'):
        p = wd / 'data' / f'theme_ticket_map_{tag}.csv'
        if not p.exists():
            continue
        rows = load_rows(p)
        for r in rows:
            if r['key'] in asg:
                r['theme'] = asg[r['key']]
                if r['theme'][:1] in DIM_NAME:
                    r['dim'] = r['theme'][0]
            t = r['theme']
            t = mrg.get(t, t); t = ren.get(t, t)
            r['theme'] = t
        with open(p, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    # v1.1: 累积修订到 edits_applied.json（finalize 回写 confirmed.yaml 时按此归并关键词）
    acc_p = wd / 'data' / 'edits_applied.json'
    acc = json.loads(acc_p.read_text(encoding='utf-8')) if acc_p.exists() else {'rename': {}, 'merge': {}, 'assign': {}}
    acc['rename'].update(ren); acc['merge'].update(mrg); acc['assign'].update(asg)
    acc_p.write_text(json.dumps(acc, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'✓ 修订已应用: rename×{len(ren)} merge×{len(mrg)} assign×{len(asg)}; 请重跑 --workdir ... --project ... 刷新汇总')

def cmd_gate(wd: Path):
    doc = json.loads((wd / 'data' / 'themes_summary.json').read_text(encoding='utf-8'))
    s = doc['stats']['cur']
    overagg = doc.get('overagg', [])
    # 其他(X)维度现含安全/运维/升级/环境等合法业务类, 不再对"其他占比"硬拦; 只保未归类率≤3%
    ok_uncls = s['uncls_pct'] <= 3.0
    ok_agg = not overagg
    print(f'门禁① 覆盖率: 主题未归类 {s["uncls_pct"]}% / 阈值 3% → '
          f'{"✅" if ok_uncls else "❌"}   (其他维度 {s["other_pct"]}% 仅供参考, 含安全/运维/升级/环境等合法类)')
    print(f'门禁② 过度聚合: {len(overagg)} 个主题占其维度 >{OVERAGG_SHARE*100:.0f}% → '
          f'{"✅ 无" if ok_agg else "❌ 须再拆"}')
    for o in overagg:
        print(f'      {o["dim"]} {o["theme"]}: {o["dim_share_pct"]}%')
    if not (ok_uncls and ok_agg):
        if not ok_uncls:
            print('→ 覆盖率处置: ①LLM 补聚(写 themes-auto.yaml 重跑) ②确认闸口人工指定 ③用户显式豁免(记入口径)')
        if not ok_agg:
            print('→ 过度聚合处置: `--overagg` 看该主题样本标题 → 拆成更细叶级主题(改 themes-auto.yaml)重跑；'
                  '过度聚合=人工未细分, 不得直接放行(除非用户显式豁免)')
        sys.exit(3)

def cmd_overagg(wd: Path, project: str):
    """列出过度聚合主题 + 各自样本标题，供 LLM 拆分为更细叶级主题"""
    doc = json.loads((wd / 'data' / 'themes_summary.json').read_text(encoding='utf-8'))
    overagg = doc.get('overagg', [])
    if not overagg:
        print('✅ 无过度聚合主题'); return
    rows = load_rows(wd / 'data' / 'theme_ticket_map_cur.csv')
    for o in overagg:
        hits = [r for r in rows if r['theme'] == o['theme'] and r['dim'] == o['dim']]
        print(f'\n══ {o["dim"]} {o["theme"]} · {o["n"]}单 = 维度内 {o["dim_share_pct"]}%（须拆）══')
        import re as _re
        for r in hits[:40]:
            print('  ' + _re.sub(r'【[^】]*】', '', r['summary'])[:66])
    print(f'\n→ 依据以上标题，在 themes/{project}/themes-auto.yaml 把每个过度聚合主题拆成 3-6 个更细'
          f'叶级主题(具体关键词，非宽泛模块名)，删掉/收窄原宽主题的关键词，再重跑聚合。')

def cmd_finalize(wd: Path, project: str):
    """固化本次主题结构 + 回写用户确认主题库 themes/<PROJ>/themes-confirmed.yaml
    关键词来源=现行生效规则(ThemeSets)按 edits_applied 归并: rename→旧id关键词移新id;
    merge→并集且删被并id; assign→keywords:[]仅记录(下次加载跳过)"""
    import datetime
    doc = json.loads((wd / 'data' / 'themes_summary.json').read_text(encoding='utf-8'))
    final = {'project': project, 'themes': [{'id': t['theme'], 'dimension': t['dim'],
                                             'n': t['n'], 'cust': t['cust'], 'ipc': t['ipc']}
                                            for t in doc['themes']]}
    (wd / 'data' / 'themes-final.yaml').write_text(
        yaml.safe_dump(final, allow_unicode=True, sort_keys=False), encoding='utf-8')
    # ── 回写 confirmed 主题库 ──
    ts = ThemeSets(project)
    live = {}          # id → (dim, keywords) 现行生效规则
    for dim, rules in (('P', ts.p), ('I', ts.i), ('K', ts.k), ('R', ts.r_fb)):
        for tid, kws in rules:
            live[tid] = (dim, list(kws))
    acc_p = wd / 'data' / 'edits_applied.json'
    acc = json.loads(acc_p.read_text(encoding='utf-8')) if acc_p.exists() else {'rename': {}, 'merge': {}, 'assign': {}}
    # merge: 目标关键词 = 并集; 被并 id 移除
    for src, dst in acc.get('merge', {}).items():
        if src in live:
            sdim, skws = live.pop(src)
            ddim, dkws = live.get(dst, (sdim, []))
            live[dst] = (ddim, list(dict.fromkeys(dkws + skws)))
    # rename: 旧 id 关键词移到新 id
    for old, new in acc.get('rename', {}).items():
        if old in live:
            live[new] = live.pop(old)
    today = datetime.date.today().isoformat()
    # ⚠️ 回写顺序必须=规则匹配优先级顺序(具体优先), 不能用 themes_summary 的工单量降序——
    #    否则宽主题(如"通用配置咨询")被排最前吸走细主题, 触发过度聚合(回归实测 79→45 主题坍缩)。
    summary_ids = {t['theme'] for t in doc['themes']
                   if not t['theme'].endswith('未归类') and t['dim'] != 'X'}
    leaf, seen = [], set()
    for dim, rules in (('P', ts.p), ('I', ts.i), ('K', ts.k), ('R', ts.r_fb)):
        for tid, _kws in rules:
            # 应用 rename/merge 变换后的最终 id
            fid = tid
            fid = acc.get('merge', {}).get(fid, fid)
            fid = acc.get('rename', {}).get(fid, fid)
            if fid in summary_ids and fid in live and fid not in seen:
                seen.add(fid)
                leaf.append({'id': fid, 'dimension': live[fid][0], 'keywords': live[fid][1],
                             'source': 'user-confirmed', 'confirmed_at': today})
    live_p_ids = {tid for tid, _ in ts.p}
    for t in doc['themes']:        # assign 等不在规则里的孤立主题: 追加末尾, 空关键词仅记录
        tid = t['theme']
        if tid in summary_ids and tid not in seen:
            if tid.startswith('R-') and ('P-' + tid[2:]) in live_p_ids:
                continue           # R-镜像主题由 P 规则动态派生, 不落 confirmed(避免空关键词噪音)
            seen.add(tid)
            leaf.append({'id': tid, 'dimension': t['dim'], 'keywords': [],
                         'source': 'manual-assign', 'confirmed_at': today})
    conf_dir = THEMES_DIR / project
    conf_dir.mkdir(parents=True, exist_ok=True)
    (conf_dir / 'themes-confirmed.yaml').write_text(
        yaml.safe_dump({'project': project, 'version': today,
                        'description': '用户确认主题库(优先于种子与auto加载; 同id完全替换后者)',
                        'leaf_themes': leaf}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    write_state(wd, stage='confirmed')
    print(f'✓ themes-final.yaml 已固化 ({len(final["themes"])} 主题)')
    print(f'✓ 用户确认主题库已回写: themes/{project}/themes-confirmed.yaml ({len(leaf)} 条, 下次分析优先加载)')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--project')
    ap.add_argument('--sample', type=int)
    ap.add_argument('--batches', action='store_true')
    ap.add_argument('--apply-edits')
    ap.add_argument('--gate', action='store_true')
    ap.add_argument('--overagg', action='store_true', help='列出过度聚合主题+样本标题供拆分')
    ap.add_argument('--finalize', action='store_true')
    a = ap.parse_args()
    wd = Path(a.workdir).expanduser()
    if a.batches:
        cmd_batches(wd)
    elif a.apply_edits:
        cmd_apply_edits(wd, a.apply_edits)
    elif a.gate:
        cmd_gate(wd)
    elif a.overagg:
        cmd_overagg(wd, a.project or '')
    elif a.finalize:
        cmd_finalize(wd, a.project or '')
    elif a.sample:
        cmd_sample(wd, a.sample)
    else:
        if not a.project:
            sys.exit('聚合需要 --project')
        run_classify(wd, a.project)

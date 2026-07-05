#!/usr/bin/env python3
"""ticket-insight 共享基座：配置/认证级联/Jira请求(退避)/JQL构建/路径工具

认证优先级（per-user）:
  1. config.jsessionid（上次成功写回）
  2. /tmp/jira-session.json（Playwright storageState）
  3. CDP 9333/9222（运行中 Chrome 实时 cookie；SQLite 是滞后值，不做首选）
  4. POST /rest/auth/1/session（用户名+Fernet解密密码；MFA 实例 403 自动跳过）
校验一律用 GET /rest/auth/1/session（勿用公开页——Dashboard 可能匿名可访问）。
"""
import base64, getpass, hashlib, json, os, socket, ssl, struct, sys, time, uuid
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

SKILL_DIR   = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / 'config.json'
THEMES_DIR  = SKILL_DIR / 'themes'
CACHE_DIR   = SKILL_DIR / '.cache'
CACHE_DIR.mkdir(exist_ok=True)

_CTX = ssl.create_default_context(); _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE

DEFAULT_BASE = 'https://gfjira.yyrd.com'

# ── 领域模块(cf10123) seed 甄别：技术分层类无业务主题意义，不采纳为 seed ──────────
# 规则：cf10123 的领域/子模块【只做主题 seed（候选命名/关键词来源）】，绝不按字段值直接绑定工单。
# 这些是"技术分层/非业务"分类，出现在领域或子模块名里 → 该项 seed 丢弃（工单仍按标题聚类，不受影响）。
NON_BUSINESS_MODULES = {
    '技术特性', '架构与运维', '架构', '运维', '性能', '安全', '稳定性', '数据库',
    '前端', '后端', '中间件', '基础技术', '技术', '底层', '部署', '监控', '日志',
    '缓存', '网关', '基础设施', '其他', '其它', '未分类', '待分类', '默认', '通用', '综合',
}
_TECH_SEG = ('前端', '后端', '运维', '架构', '性能', '稳定性', '中间件', '数据库', '技术特性', '技术')

def is_business_module(name: str) -> bool:
    """cf10123 领域/子模块是否有业务主题意义（过滤技术分层类，如 技术特性/架构与运维/前端）"""
    n = (name or '').strip()
    if not n:
        return False
    if n in NON_BUSINESS_MODULES:
        return False
    seg = n.split('-')[-1].strip()          # "流程中心-前端" → 末段"前端" 是技术分层 → 非业务
    if seg in NON_BUSINESS_MODULES or seg in _TECH_SEG:
        return False
    return True

# 单一主题在其【所属维度】内的占比上限。超过 = 过度聚合（人工未细分，如 LCZX "工作流设计">60%），
# 必须再拆：该主题只能当 seed 候选名，实际须按工单标题拆成更细的叶级主题。
OVERAGG_SHARE = 0.25

# ── 配置 ──────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    return {}

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')

def base_url(cfg=None) -> str:
    return ((cfg or load_config()).get('jira_base_url') or DEFAULT_BASE).rstrip('/')

# ── Fernet 机器绑定加密（同 ticket-query 思路：密文不可跨机器复制）──────────────
def _machine_key() -> bytes:
    seed = f'{uuid.getnode()}|{getpass.getuser()}|ticket-insight'.encode()
    return base64.urlsafe_b64encode(hashlib.pbkdf2_hmac('sha256', seed, b'ti-salt-v1', 100_000, 32))

def encrypt_password(plain: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_machine_key()).encrypt(plain.encode()).decode()

def decrypt_password(enc: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_machine_key()).decrypt(enc.encode()).decode()

# ── HTTP（指数退避：本机代理对并发 TLS 握手会偶发重置 SSL EOF）─────────────────
def jira_get(path: str, params: dict, cookie: str, retries: int = 5, base: str = None):
    url = f'{base or base_url()}{path}' + (('?' + urlencode(params, quote_via=quote)) if params else '')
    req = Request(url, headers={'Cookie': cookie, 'Accept': 'application/json',
                                'User-Agent': 'Mozilla/5.0 (ticket-insight)'})
    last = None
    for a in range(retries):
        try:
            with urlopen(req, context=_CTX, timeout=60) as r:
                return json.loads(r.read())
        except HTTPError:
            raise                      # 4xx/5xx 直接抛（401 需走认证级联，重试无意义）
        except (ssl.SSLError, OSError) as e:
            last = e
            if a < retries - 1:
                time.sleep(2 ** a)
    raise last

def jira_post(path: str, body: dict, retries: int = 3, base: str = None):
    url = f'{base or base_url()}{path}'
    req = Request(url, data=json.dumps(body).encode(),
                  headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                           'User-Agent': 'Mozilla/5.0 (ticket-insight)'})
    last = None
    for a in range(retries):
        try:
            with urlopen(req, context=_CTX, timeout=30) as r:
                return json.loads(r.read())
        except HTTPError:
            raise
        except (ssl.SSLError, OSError) as e:
            last = e; time.sleep(2 ** a)
    raise last

# ── cookie 来源 ───────────────────────────────────────────────────────────────
def _cookies_from_session_file() -> dict:
    out = {}
    for p in ('/tmp/jira-session.json',):
        if os.path.exists(p):
            try:
                st = json.load(open(p))
                for c in (st.get('cookies') if isinstance(st, dict) else []) or []:
                    out.setdefault(c['name'], c['value'])
            except Exception:
                pass
    return out

def _cdp_cookies(port: int) -> dict:
    """CDP HTTP+最小WS握手，取运行中 Chrome 实时 cookie（SQLite 是滞后值）"""
    import http.client
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('GET', '/json'); targets = json.loads(conn.getresponse().read())
    ws_url = next((t['webSocketDebuggerUrl'] for t in targets if t.get('webSocketDebuggerUrl')), None)
    if not ws_url:
        return {}
    host_port = ws_url.split('://')[1].split('/')[0]
    h, p = host_port.split(':'); p = int(p)
    path = ws_url.split(host_port)[-1]
    s = socket.create_connection((h, p), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f'GET {path} HTTP/1.1\r\nHost: {h}:{p}\r\nUpgrade: websocket\r\n'
               f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
    s.recv(4096)
    def send(obj):
        data = json.dumps(obj).encode(); mask = os.urandom(4); ln = len(data)
        hdr = bytes([0x81])
        if ln < 126:     hdr += bytes([0x80 | ln])
        elif ln < 65536: hdr += bytes([0x80 | 126]) + struct.pack('>H', ln)
        else:            hdr += bytes([0x80 | 127]) + struct.pack('>Q', ln)
        s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    def recv():
        b = s.recv(2); ln = b[1] & 0x7f
        if ln == 126:   ln = struct.unpack('>H', s.recv(2))[0]
        elif ln == 127: ln = struct.unpack('>Q', s.recv(8))[0]
        buf = b''
        while len(buf) < ln:
            buf += s.recv(ln - len(buf))
        return buf
    send({'id': 1, 'method': 'Network.getAllCookies'})
    host = base_url().split('://')[1]
    out = {}
    for _ in range(10):
        msg = json.loads(recv())
        if msg.get('id') == 1:
            for c in msg.get('result', {}).get('cookies', []):
                d = c.get('domain', '')
                if d and (d.lstrip('.') in host or host.endswith(d.lstrip('.')) or 'yyrd' in d):
                    out.setdefault(c['name'], c['value'])
            break
    s.close()
    return out

def _cookie_header(cookies: dict) -> str:
    keys = [k for k in ('JSESSIONID', 'seraph.rememberme.cookie', 'atlassian.xsrf.token') if k in cookies]
    return '; '.join(f'{k}={cookies[k]}' for k in keys)

def validate_session(cookie: str) -> str | None:
    """有效返回登录名，无效返回 None。用 /rest/auth/1/session（不会匿名假阳性）"""
    if not cookie:
        return None
    try:
        d = jira_get('/rest/auth/1/session', {}, cookie, retries=3)
        return d.get('name') or None
    except Exception:
        return None

GUIDE_COOKIE = """
❌ 自动获取登录态失败。请按最简单的一种方式补一次登录态：
  ① 在本机 Chrome 打开一次 Jira 页面（{base}），保持登录，然后重试本命令；
  ② 浏览器按 F12 → Application/存储 → Cookies → 复制 JSESSIONID 的值，运行：
     python3 scripts/ti_auth.py --paste-cookie <JSESSIONID值>
  ③ 若从未配置账号：python3 scripts/ti_auth.py --setup
""".strip()

def get_cookie(verbose: bool = True) -> tuple[str, str, str]:
    """认证级联 → (cookie_header, 来源, 登录名)。全失败 SystemExit(2) 并打印引导。"""
    cfg = load_config()
    def _log(m):
        if verbose: print(m)
    # 1. config 里的 jsessionid
    if cfg.get('jsessionid'):
        ch = _cookie_header({'JSESSIONID': cfg['jsessionid'], **({'seraph.rememberme.cookie': cfg['seraph']} if cfg.get('seraph') else {})})
        who = validate_session(ch)
        if who:
            return ch, 'config', who
        _log('[auth] config 中的 session 已失效，尝试其他来源…')
    # 2/3. session 文件 → CDP
    sources = [('session文件', _cookies_from_session_file),
               ('CDP-9333', lambda: _cdp_cookies(9333)),
               ('CDP-9222', lambda: _cdp_cookies(9222))]
    for name, fn in sources:
        try:
            cookies = fn()
        except Exception:
            continue
        if not cookies.get('JSESSIONID'):
            continue
        ch = _cookie_header(cookies)
        who = validate_session(ch)
        if who:
            cfg['jsessionid'] = cookies['JSESSIONID']
            if cookies.get('seraph.rememberme.cookie'):
                cfg['seraph'] = cookies['seraph.rememberme.cookie']
            save_config(cfg)
            _log(f'[auth] ✅ {name} → 已写回 config')
            return ch, name, who
        _log(f'[auth] {name} 的 session 无效')
    # 4. 用户名密码创建 session（MFA 实例 403 会拦）
    if cfg.get('username') and cfg.get('password_enc'):
        try:
            d = jira_post('/rest/auth/1/session',
                          {'username': cfg['username'], 'password': decrypt_password(cfg['password_enc'])})
            sess = d.get('session', {})
            if sess.get('value'):
                cfg['jsessionid'] = sess['value']; save_config(cfg)
                ch = _cookie_header({'JSESSIONID': sess['value']})
                who = validate_session(ch)
                if who:
                    _log('[auth] ✅ 账号密码创建 session 成功')
                    return ch, 'session-api', who
        except HTTPError as e:
            if e.code == 403:
                _log('[auth] session API 被 MFA 拦截(403)，需浏览器登录态')
            else:
                _log(f'[auth] session API 失败: HTTP {e.code}')
        except Exception as e:
            _log(f'[auth] session API 异常: {e}')
    print(GUIDE_COOKIE.format(base=base_url(cfg)))
    sys.exit(2)

# ── JQL / 查询 ────────────────────────────────────────────────────────────────
def build_jql(project: str, start: str, end_excl: str,
              domain: str = None, sub: str = None, issuetype: str = '支持问题') -> str:
    jql = f'project = {project} AND issuetype = "{issuetype}" AND created >= "{start}" AND created < "{end_excl}"'
    if domain and sub:
        jql += f' AND cf[10123] in cascadeOption("{domain}", "{sub}")'
    elif domain:
        jql += f' AND cf[10123] in cascadeOption("{domain}")'
    return jql

def count_only(jql: str, cookie: str) -> int:
    return jira_get('/rest/api/2/search', {'jql': jql, 'startAt': 0, 'maxResults': 0}, cookie).get('total', 0)

# ── 输出目录 ──────────────────────────────────────────────────────────────────
def downloads_dir() -> Path:
    cfg = load_config()
    if cfg.get('default_output_dir'):
        return Path(cfg['default_output_dir']).expanduser()
    if os.name == 'nt':
        return Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Downloads'
    d = Path.home() / 'Downloads'
    return d if d.exists() else Path.home()

def workdir(project: str, label: str, outdir: str = None) -> Path:
    root = Path(outdir).expanduser() if outdir else downloads_dir()
    wd = root / f'ticket-insight-{project}-{label}'
    (wd / 'data').mkdir(parents=True, exist_ok=True)
    return wd

# ── 进度/横幅 ─────────────────────────────────────────────────────────────────
STAGES = ['登录', '范围', '主题聚合', '四维度', '报告']
def banner(stage_idx: int) -> str:
    parts = []
    for i, s in enumerate(STAGES):
        parts.append(('✓' if i < stage_idx else '▶' if i == stage_idx else '○') + s)
    return f"━━ ticket-insight ━ [{' '.join(parts)}] ━ 第{stage_idx+1}/5阶段 ━━"

def fmt_secs(s: float) -> str:
    return f'{s:.0f} 秒' if s < 90 else f'{s/60:.1f} 分钟'

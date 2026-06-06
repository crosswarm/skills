import os as _bootstrap_os

# Demo 沙箱模式：设置 IS_DEMO_INSTANCE=true 时屏蔽所有 Jira/PM 写操作
_IS_DEMO = _bootstrap_os.getenv("IS_DEMO_INSTANCE", "").lower() in ("1", "true")

# 确保本地回环地址 + 内网 Jira 域名不走 HTTP 代理
# 问题：Surge/系统代理 (192.168.9.100) 会拦截 jira.example.com 的流量
# 导致即使 JSESSIONID cookie 有效，Jira 也返回 403（触发 CAPTCHA 风控）
_bootstrap_os.environ.setdefault("no_proxy", "localhost,127.0.0.1,0.0.0.0,::1")
_existing_no_proxy = _bootstrap_os.environ.get("no_proxy", "")
for _host in (
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    # Jira & PM 系统不走系统代理（从 JIRA_BASE_URL / PM_BASE_URL 自动提取）
    *([_h for _u in [_bootstrap_os.environ.get("JIRA_BASE_URL", ""), _bootstrap_os.environ.get("PM_BASE_URL", "")]
       if _u for _h in [__import__('urllib.parse', fromlist=['urlparse']).urlparse(_u).hostname or ""] if _h]),
):
    if _host not in _existing_no_proxy:
        _bootstrap_os.environ["no_proxy"] = f"{_existing_no_proxy},{_host}".strip(",")
        _existing_no_proxy = _bootstrap_os.environ["no_proxy"]
_bootstrap_os.environ["NO_PROXY"] = _bootstrap_os.environ["no_proxy"]

from fastapi import Depends, FastAPI, BackgroundTasks, Body, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware

# HTTPException已在上方导入，此处删除重复导入
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any
try:
    from analysis import TicketAnalyzer
    _TICKET_ANALYZER_AVAILABLE = True
except ImportError:
    TicketAnalyzer = None
    _TICKET_ANALYZER_AVAILABLE = False

# Chroma优化版服务（使用语义搜索和向量缓存）
from search_chroma import SemanticSearchEngine, SearchEngine as ChromaSearchEngine
from board_service_chroma import BoardService as ChromaBoardService

# 保留原有服务（向后兼容）
# from search import SearchEngine
# from board_service import BoardService

from llm_service import LLMService
# requirement_planning 已从 core 移除（文件不存在）。
# compact 桩：保留 /api/spec/* 路由的 16 个 requirement_service.* 调用点不报 NameError(500)，
# 而是统一返回 503『功能已移除』。避免裁剪后死路由抛未定义异常。
class _RemovedRequirementService:
    def __getattr__(self, _name):
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=503, detail="需求规划/Spec 功能在 compact 版已移除")
requirement_service = _RemovedRequirementService()
from kb_analysis import get_kb_analyzer
from kb_runtime_service import KnowledgeRuntimeService
# design_fact_service, competitor_*_service 已从 core 移除
from jira_service import jira_service as jira_svc, JiraService
from search_chroma import get_vector_store
# spec_generation, crew_service 延迟导入（route handler 内）
from auth_service import get_auth_service
from auth_deps import require_reply_quota, require_authenticated_user, log_api_request

# 网络缓存和监控服务 (三节点架构)
from jira_cache_service import get_jira_cache_service, JiraCacheService
from network_monitor import get_network_monitor, NetworkMonitor

# PM 协作任务看板服务（guard：依赖 pm_* services，非 core 必须）
_pm_router_available = False
pm_router = None
try:
    from api.pm_routes import router as pm_router
    _pm_router_available = True
except Exception:
    pm_router = None
try:
    from api.memory_router import router as memory_router
    _memory_router_available = True
except ImportError:
    memory_router = None
    _memory_router_available = False
try:
    from api.feishu_webhook_router import router as feishu_router
    _feishu_router_available = True
except ImportError:
    feishu_router = None
    _feishu_router_available = False
try:
    from api.scheduler_router import router as scheduler_router
    _scheduler_router_available = True
except ImportError:
    scheduler_router = None
    _scheduler_router_available = False
try:
    from api.channel_router import router as channel_router
    _channel_router_available = True
except ImportError:
    channel_router = None
    _channel_router_available = False
try:
    from api.agents_router import router as agents_router, user_router as agents_user_router
    _agents_router_available = True
except ImportError:
    agents_router = None
    _agents_router_available = False
_jobmaster_router_available = False
# pm_scheduler / pm_collaboration_service 延迟导入（非核心，guard 在路由注册处）
_pm_scheduler_available = False
_pm_service_available = False
try:
    from services.pm_scheduler import start_pm_scheduler, get_pm_scheduler
    _pm_scheduler_available = True
except Exception:
    start_pm_scheduler = None
    get_pm_scheduler = None
try:
    from services.pm_collaboration_service import get_pm_service
    _pm_service_available = True
except Exception:
    get_pm_service = None

import uvicorn
import os
import hashlib
import json
import re
import requests

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# 日志配置
import logging
import logging.handlers
from pathlib import Path
import time

from services.pipeline_config_manager import PipelineConfigManager
_REPLY_GATES_YAML = Path(__file__).parent / "config" / "reply_gates.yaml"
reply_gates_mgr = PipelineConfigManager(_REPLY_GATES_YAML)

# 配置日志
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 创建日志格式
log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
date_format = '%Y-%m-%d %H:%M:%S'

# 配置根日志记录器
logger = logging.getLogger('ai_ticket')
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / 'main.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

# 关闭 uvicorn 的默认日志
uvicorn_loggers = ["uvicorn", "uvicorn.access", "uvicorn.error"]
for name in uvicorn_loggers:
    logging.getLogger(name).handlers = []
    logging.getLogger(name).propagate = False

app = FastAPI()

_PROCESS_STARTED_AT = time.time()

@app.get("/api/liveness", include_in_schema=False)
def liveness():
    """Watchdog 专用：不进 ChromaDB、不进 worker queue，只证明 uvicorn 在响应。"""
    return {"ok": True, "pid": os.getpid(), "uptime_s": int(time.time() - _PROCESS_STARTED_AT)}

@app.get("/api/fd_health", include_in_schema=False)
def fd_health():
    """报告本进程 fd 软/硬上限；配合外部 lsof 统计实际使用数。"""
    if sys.platform == "win32":
        return {"ok": True, "note": "fd limits N/A on Windows", "pid": os.getpid()}
    import resource as _resource
    soft, hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    return {"ok": True, "rlimit_soft": soft, "rlimit_hard": hard, "pid": os.getpid()}

# Custom exception handler to return 400 instead of 422 for validation errors
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=400,
        content={
            "status": "error",
            "detail": "请求参数验证失败",
            "errors": exc.errors()
        }
    )

from role_guard import NoUserContextError, is_strict_role

@app.exception_handler(NoUserContextError)
async def _no_user_ctx_handler(request: Request, exc: NoUserContextError):
    return JSONResponse(
        {"detail": str(exc), "code": "NO_USER_CONTEXT", "where": exc.where},
        status_code=401,
    )

# Enable CORS for Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

auth_service = get_auth_service()
SESSION_COOKIE_NAME = "ai_ticket_session"
PUBLIC_PATHS = {
    "/health",
    "/login.html",
    "/api/auth/bootstrap-status",
    "/api/auth/bootstrap",
    "/api/auth/login",
    "/api/agents/internal/scheduler/tick",  # 系统看门狗专用，localhost-only 鉴权在端点内做
}
PUBLIC_PREFIXES = ("/assets", "/api/auth")
PROTECTED_PAGE_PATHS = {
    "/",
    "/search.html",
    "/board.html",
    "/kb.html",
    "/settings.html",
    "/guide.html",
}


def _wants_json_response(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return request.url.path.startswith("/api/") or request.url.path in {"/query", "/api/analyze/stream"} or "application/json" in accept


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _is_protected_page_path(path: str) -> bool:
    return path in PROTECTED_PAGE_PATHS


def _is_protected_api_path(path: str) -> bool:
    if _is_public_path(path):
        return False
    return path.startswith("/api/") or path in {"/query", "/analyze"}


def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    return getattr(request.state, "current_user", None)


def require_authenticated_user(request: Request) -> Dict[str, Any]:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin_user(request: Request) -> Dict[str, Any]:
    user = require_authenticated_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_request_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else ""


def mask_jira_token(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _write_user_session_file(username: str, cookies: Dict[str, str]) -> None:
    """将用户的 session cookies 写成 Playwright storageState JSON，供 jira_proxy 附件下载/move_issue 脚本按用户隔离读取。"""
    if not username:
        return
    try:
        from services.host_context import session_path as _session_path
        state_path = _session_path(user=username, prefix="jira")
        state_cookies = []
        jsession = (cookies.get("JSESSIONID") or "").strip()
        if jsession:
            state_cookies.append({
                "name": "JSESSIONID",
                "value": jsession,
                "domain": "." + (__import__('urllib.parse', fromlist=['urlparse']).urlparse(os.getenv("JIRA_BASE_URL", "https://jira.example.com")).hostname or "jira.example.com"),
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
                "expires": -1,
            })
        # atlassian.xsrf.token 故意不写入 storage_state：
        # Playwright 首次 GET 时 Jira 会主动 Set-Cookie 颁发与当前 session 同源的 xsrf，
        # form 的隐藏字段 atl_token 也是服务端基于该新 xsrf 渲染的，两者必匹配。
        # 若携带用户粘贴的『陈旧 xsrf』，form atl_token 与 cookie 不一致 → POST 被 Jira 判定会话过期。
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"cookies": state_cookies, "origins": []}, f)
    except Exception as exc:
        logger.warning(f"[jira] 写入 per-user session 文件失败 user={username}: {exc}")


def _resolve_jira_base_url(binding_base_url: Optional[str] = None) -> Optional[str]:
    """
    根据 AITICKET_ROLE 决定 Jira 访问入口:
    - mini (默认): 直连 Jira（binding.jira_base_url 或默认 jira.example.com）
    - qcl: 强制走 Mini 的 mini_proxy（frp 5001 隧道）
          因 QCL 云服务器无法直连 Jira 内网 (172.20.x)，必须中转
          mini_proxy 是透明代理，会带着用户自己的 JSESSIONID 转发
          这样每个用户用自己的 session，不会和其他用户冲突
    """
    role = os.environ.get("AITICKET_ROLE", "mini").lower()
    if role == "qcl":
        return os.environ.get("MINI_PROXY_URL", "http://127.0.0.1:5001")
    return binding_base_url or None


def build_request_jira_client(request: Request, require_binding: bool = True) -> Optional[JiraService]:
    cached_client = getattr(request.state, "jira_client", None)
    if cached_client is not None:
        return cached_client

    # 优先使用已登录用户的 Jira 绑定凭据
    user = get_current_user(request)
    if user:
        binding = auth_service.get_jira_binding_credentials(user["id"])
        if binding:
            auth_type = binding.get("auth_type", "basic_auth")
            if auth_type == "session_cookie":
                # session_cookie 模式：用 JSESSIONID 认证
                cookies = auth_service.get_jira_session_cookies(user["id"])
                if cookies and cookies.get("JSESSIONID"):
                    _write_user_session_file(user["username"], cookies)
                    jira_client = JiraService(
                        session_cookies={
                            "JSESSIONID": cookies["JSESSIONID"],
                            "xsrf_token": cookies.get("xsrf_token", ""),
                        },
                        base_url=_resolve_jira_base_url(binding.get("jira_base_url")),
                        include_config_cookies=False,
                        enable_cache=False,
                        cache_namespace=user["username"],
                    )
                    request.state.jira_client = jira_client
                    return jira_client
                # 没有可用 cookies：降级到默认配置（不应该发生，防御性处理）
                logger.warning(f"[jira] user={user['username']} 绑定为 session_cookie 但无 cookies，降级为默认配置")
            else:
                # basic_auth 模式（原逻辑保留）
                jira_client = JiraService(
                    username=binding["jira_username"],
                    password=binding["jira_api_token"],
                    base_url=_resolve_jira_base_url(binding.get("jira_base_url")),
                    include_config_cookies=False,
                    enable_cache=False,
                    cache_namespace=user["username"],
                )
                request.state.jira_client = jira_client
                return jira_client

    # demo 用户：无需个人 Jira 绑定，使用服务器默认凭据（只读）
    if user and user.get("is_demo"):
        if is_strict_role():
            request.state.jira_client = None
            return None
        jira_client = JiraService(base_url=_resolve_jira_base_url())
        request.state.jira_client = jira_client
        return jira_client

    # 已登录但未绑定 Jira：返回 None，让看板显示空数据
    if user:
        request.state.jira_client = None
        return None

    # 未登录（公开 API 等场景）：strict 模式拒绝匿名访问
    if is_strict_role():
        request.state.jira_client = None
        return None
    jira_client = JiraService(base_url=_resolve_jira_base_url())
    request.state.jira_client = jira_client
    return jira_client


@app.get("/api/system/role")
def get_system_role():
    """
    返回当前服务器角色，供前端决定 UI 展示方式。
    - mini: 本机，允许完整的 Jira/PM 绑定（session_cookie / basic_auth）
    - qcl: 云服务器，通过 mini_proxy 中转。仍允许绑定用户自己的 JSESSIONID，
           但提示用户"请不要和 Mini 重复绑定同一 JSESSIONID，避免 Jira 多地登录风控"
    """
    role = os.environ.get("AITICKET_ROLE", "mini").lower()
    return {
        "role": role,
        "mini_proxy_url": os.environ.get("MINI_PROXY_URL", "http://127.0.0.1:5001") if role == "qcl" else None,
        "hint": {
            "mini": "本机直连 Jira/PM，允许完整绑定。",
            "qcl": "通过 Mini 代理访问 Jira。仍需绑定您自己的 JSESSIONID，但请不要同一 session 在两台服务器重复绑定。",
        }.get(role, ""),
    }

# 健康检查端点
@app.get("/health")
async def health_check():
    """服务健康检查"""
    try:
        # 基本状态
        status_info = {
            "status": "healthy",
            "service": "ai_ticket",
            "timestamp": time.time()
        }

        logger.info("Health check OK")
        return status_info

    except Exception as e:
        logger.error(f"Health check error: {str(e)}")
        return {
            "status": "unhealthy",
            "service": "ai_ticket",
            "error": str(e)
        }

# 系统看门狗专用端点 — 仅 127.0.0.1，无需用户认证（安全由 localhost 保证）
@app.post("/api/agents/internal/scheduler/tick")
async def internal_scheduler_tick(request: Request):
    client_host = (request.client.host if request.client else "")
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Internal-only endpoint")
    from services.scheduler_service import get_scheduler
    import datetime as _dt
    get_scheduler()._check_and_execute()
    return {"ok": True, "ts": _dt.datetime.utcnow().isoformat()}


# 会话保活管理 API
@app.get("/api/session-keepalive/status")
async def get_session_keepalive_status():
    """查看所有已注册系统的 session 保活状态"""
    from services.session_keepalive_service import get_keepalive_manager
    return {"status": "success", "sessions": get_keepalive_manager().get_status()}

@app.post("/api/session-keepalive/{name}/refresh")
async def force_session_refresh(name: str):
    """立即刷新指定系统的 session（不等待定时器）"""
    from services.session_keepalive_service import get_keepalive_manager
    mgr = get_keepalive_manager()
    ok = mgr.refresh_now(name)
    return {"status": "success" if ok else "error",
            "name": name, "refreshed": ok,
            "session": mgr.get_session_status(name)}

def _register_session_keepalives():
    """
    统一注册所有外部系统的 session 保活。
    接入规范（spec: design/spec/session-keepalive-service.md）：
      - 每个外部系统（Jira / PM / BIP …）注册一次，与具体模块无关
      - refresh_fn 优先指向 shell 脚本，与服务实例解耦
      - 新系统在此追加 mgr.register(...) 即可
    """
    try:
        from services.session_keepalive_service import get_keepalive_manager
        from pathlib import Path
        import subprocess

        scripts_dir = Path(__file__).parent / "scripts"
        mgr = get_keepalive_manager()

        def _make_script_refresher(script_path: Path):
            def _refresh():
                if sys.platform == "win32":
                    logger.warning(f"[KeepAlive] bash 脚本在 Windows 上不可用，跳过: {script_path}")
                    return False
                r = subprocess.run(["bash", str(script_path)],
                                   capture_output=True, timeout=30)
                return r.returncode == 0
            return _refresh

        # ── 告警公共函数（失败时发飞书通知）──────────────────────────────────
        def _make_feishu_alerter(system_name: str):
            def _alert(label: str, reason: str, failures: int):
                try:
                    from services.feishu_notifier import get_notifier
                    msg = (
                        f"⚠️ {label} Session 刷新告警\n"
                        f"原因：{reason}\n"
                        f"连续失败 {failures} 次\n"
                        f"请检查 Chrome 是否已登录，Keychain 是否授权\n"
                        f"手动刷新：POST /api/session-keepalive/{system_name}/refresh"
                    )
                    get_notifier().send_message(msg)
                    logger.warning(f"[KeepAlive] 飞书告警已发送: {label} {reason}")
                except Exception as ex:
                    logger.warning(f"[KeepAlive] 飞书告警失败: {ex}")
            return _alert

        # ── PM 有效性验证：用轻量请求验证 token 真正可用 ──────────────────
        def _pm_validate():
            try:
                from services.pm_module_service import PMModuleService
                svc = PMModuleService("original_demand")
                result = svc.check_token_valid()
                return bool(result.get("valid"))
            except Exception:
                return False

        # ── Jira 有效性验证（轻量 myself API）────────────────────────────
        def _jira_validate():
            try:
                import requests
                from jira_proxy import jira_proxy
                cookies = jira_proxy._get_session_cookies()
                if not cookies:
                    return False
                r = requests.get(
                    "https://jira.example.com/rest/api/2/myself",
                    cookies=cookies, verify=False, timeout=8,
                    headers={"Accept": "application/json"}
                )
                return r.status_code == 200
            except Exception:
                return False

        # ── Jira ─────────────────────────────────────────────────────────────
        jira_script = scripts_dir / "refresh_jira_session.sh"
        if jira_script.exists():
            mgr.register("jira", "Jira",
                         refresh_fn=_make_script_refresher(jira_script),
                         interval_minutes=30,
                         validate_fn=_jira_validate,
                         alert_fn=_make_feishu_alerter("jira"),
                         alert_after_failures=2)

        # ── PM 系统（原始需求 / 协作需求 / 特性等模块共享同一 token）────────
        pm_script = scripts_dir / "refresh_pm_token.sh"
        if pm_script.exists():
            mgr.register("pm", "PM系统",
                         refresh_fn=_make_script_refresher(pm_script),
                         interval_minutes=25,
                         validate_fn=_pm_validate,
                         alert_fn=_make_feishu_alerter("pm"),
                         alert_after_failures=2)

        # ── BIP（待接入时取消注释并提供刷新脚本）────────────────────────────
        # bip_script = scripts_dir / "refresh_bip_session.sh"
        # if bip_script.exists():
        #     mgr.register("bip", "BIP",
        #                  refresh_fn=_make_script_refresher(bip_script),
        #                  interval_minutes=60,
        #                  alert_fn=_make_feishu_alerter("bip"))

        # ── LLM 服务健康监控 ─────────────────────────────────────────────
        _llm_monitor_state = {"local_was_down": True, "codex_was_down": True}

        def _check_local_model():
            """检查本地 MLX 模型服务是否可用"""
            try:
                import requests as _req
                r = _req.get("http://localhost:8090/v1/models", timeout=3)
                alive = r.status_code == 200
                if alive and _llm_monitor_state["local_was_down"]:
                    _llm_monitor_state["local_was_down"] = False
                    try:
                        from services.feishu_notifier import get_notifier
                        get_notifier().send_message("✅ 本地模型 SuperGemma4 已上线 (localhost:8090)")
                    except Exception:
                        pass
                    logger.info("[LLM Monitor] 本地模型已上线")
                elif not alive:
                    _llm_monitor_state["local_was_down"] = True
                return alive
            except Exception:
                _llm_monitor_state["local_was_down"] = True
                return False

        def _check_codex_proxy():
            """检查 Codex 代理服务是否可用（密钥/地址从环境变量读取，未配置则跳过）"""
            codex_key = os.environ.get("CODEX_PROXY_KEY", "")
            codex_url = os.environ.get("CODEX_PROXY_URL", "")
            if not codex_key or not codex_url:
                return False  # 未配置 codex 代理 → 视为不可用，跳过探测
            try:
                import requests as _req
                r = _req.post(
                    codex_url,
                    json={"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                    headers={"Authorization": f"Bearer {codex_key}", "Content-Type": "application/json"},
                    timeout=10,
                )
                alive = r.status_code == 200
                if alive and _llm_monitor_state["codex_was_down"]:
                    _llm_monitor_state["codex_was_down"] = False
                    try:
                        from services.feishu_notifier import get_notifier
                        get_notifier().send_message(
                            "✅ Codex 代理已恢复"
                        )
                    except Exception:
                        pass
                    logger.info("[LLM Monitor] Codex 代理已恢复")
                elif not alive:
                    _llm_monitor_state["codex_was_down"] = True
                return alive
            except Exception:
                _llm_monitor_state["codex_was_down"] = True
                return False

        mgr.register("local_model", "本地模型(MLX)",
                     refresh_fn=lambda: True,
                     interval_minutes=5,
                     validate_fn=_check_local_model,
                     alert_fn=_make_feishu_alerter("local_model"),
                     alert_after_failures=3)

        # codex_proxy 监控已停用（代理不稳定，告警噪音 > 收益）

    except Exception as e:
        logger.warning(f"会话保活注册失败（不影响功能）: {e}")

# 应用启动时注册
_register_session_keepalives()

# 请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    path = request.url.path

    session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    request.state.session_token = session_token
    try:
        if session_token:
            request.state.current_user = auth_service.get_user_by_session(session_token)
        else:
            # 无 session cookie 时回退 skill token（headless 调用方：MCP server / 瘦客户端）
            # 支持 Authorization: Bearer <token> 或 X-Skill-Token: <token>
            _auth_hdr = request.headers.get("Authorization", "")
            _skill_tok = request.headers.get("X-Skill-Token", "") or (
                _auth_hdr[7:].strip() if _auth_hdr[:7].lower() == "bearer " else "")
            request.state.current_user = (
                auth_service.get_user_by_skill_token(_skill_tok) if _skill_tok else None)
    except Exception:
        request.state.current_user = None

    # Demo guard: 演示账号禁止所有写操作（reset-demo 和 logout 除外）
    _cu = request.state.current_user
    _demo_write_allowed = {
        "/api/auth/logout",
        "/api/admin/reset-demo",
        "/api/pm/modules/collaboration_demand/demands",  # 只读 POST（分页查询用 POST 传参）
        "/api/board/generate-reply",  # 智能回复生成（只读，不修改数据）
    }
    if (_cu and _cu.get("is_demo")
            and request.method in {"POST", "PUT", "DELETE", "PATCH"}
            and request.url.path not in _demo_write_allowed):
        return JSONResponse(
            status_code=403,
            content={"detail": "demo_blocked", "message": "演示账号不可执行写操作"},
        )

    # Project context: query param > header > user's saved current_project > "_global"
    pk = (request.query_params.get("project_key")
          or request.headers.get("X-Project-Key")
          or (_cu.get("current_project") if _cu else None)
          or "_global")
    request.state.project_key = pk

    # Current modules for this project (used by Board JQL / Reply trainer / KB boost)
    _cu_modules = (_cu.get("project_modules", {}).get(pk, []) if _cu else [])
    request.state.current_modules = _cu_modules

    # Lazy index probe: if this project has no Chroma history, enqueue a background fill job
    if pk and pk != "_global":
        try:
            from services.project_index_service import get_project_index_service
            get_project_index_service().trigger_if_empty(pk)
        except Exception:
            pass

    # 记录请求
    logger.info(f"Request: {request.method} {path}")

    try:
        response = await call_next(request)

        # 记录响应
        process_time = (time.time() - start_time) * 1000
        logger.info(f"Response: status={response.status_code} time={process_time:.0f}ms")

        return response
    except Exception as e:
        import traceback
        logger.error(f"Request error: {str(e)}\n{traceback.format_exc()}")
        raise

# Global Instances
llm_service = LLMService()

# 使用Chroma优化版服务（确保使用绝对路径）
# demo 沙箱可通过 DEMO_RUNTIME_DIR 重定向 chroma_db / data_cache，主站不设则走默认
BASE_DIR = os.environ.get("DEMO_RUNTIME_DIR") or os.path.dirname(os.path.abspath(__file__))
persist_dir = os.path.join(BASE_DIR, "chroma_db")
# 周报/月报目录：主站用代码相对路径，demo 通过 WEEKLY_REPORT_DIR/MONTHLY_REPORT_DIR 隔离
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
WEEKLY_REPORT_DIR = os.environ.get("WEEKLY_REPORT_DIR") or os.path.normpath(os.path.join(_CODE_DIR, "../../conclusion/WeeklyReports"))
MONTHLY_REPORT_DIR = os.environ.get("MONTHLY_REPORT_DIR") or os.path.normpath(os.path.join(_CODE_DIR, "../../conclusion/MonthlyReports"))

# 统一embedding模型下载配置（可通过环境变量控制）
ALLOW_EMBEDDING_DOWNLOAD = os.environ.get("ALLOW_EMBEDDING_DOWNLOAD", "true").lower() == "true"

search_engine = ChromaSearchEngine(allow_download=ALLOW_EMBEDDING_DOWNLOAD)
board_service = ChromaBoardService(llm_service, api_key=None, allow_download=ALLOW_EMBEDDING_DOWNLOAD)

kb_analyzer = get_kb_analyzer(llm_service)
kb_runtime_service = KnowledgeRuntimeService()

vector_store_instance = get_vector_store(allow_download=ALLOW_EMBEDDING_DOWNLOAD)

# 注册 KB 自动采集和编译服务
from kb_auto_import import KBAutoImport, register_auto_import
from kb_compile_service import KBCompileService, register_compile_service

_kb_auto_import = KBAutoImport(
    kb_hybrid_index=kb_runtime_service.hybrid_index if hasattr(kb_runtime_service, 'hybrid_index') else None,
    llm_service=llm_service,
)
register_auto_import(_kb_auto_import)

_kb_compile_svc = KBCompileService(
    kb_hybrid_index=kb_runtime_service.hybrid_index if hasattr(kb_runtime_service, 'hybrid_index') else None,
    kb_runtime_service=kb_runtime_service,
    llm_service=llm_service,
)
register_compile_service(_kb_compile_svc)

# competitor_validation_service 已从 core 移除

# --- 网络缓存服务初始化 (三节点架构) ---

# 加载网络配置
ENABLE_CACHE_SERVICE = os.environ.get("ENABLE_CACHE_SERVICE", "false").lower() == "true"
ENABLE_NETWORK_MONITOR = os.environ.get("ENABLE_NETWORK_MONITOR", "false").lower() == "true"
BOARD_FETCH_STRATEGY = "server_unified"
BOARD_FETCH_ORDER = ["jira_direct", "jira_proxy", "local_cache"]
FRP_EXPECTED_PORTS = {
    "bind_port": int(os.environ.get("FRP_BIND_PORT", "7000")),
    "vhost_http_port": int(os.environ.get("FRP_VHOST_HTTP_PORT", "8080")),
    "dashboard_port": int(os.environ.get("FRP_DASHBOARD_PORT", "7500")),
    "mini_proxy_port": int(os.environ.get("MINI_PROXY_PORT", "5001")),
}
DEFAULT_PROXY_BASE_URL = f"http://localhost:{FRP_EXPECTED_PORTS['vhost_http_port']}/jira_proxy"

# Jira 缓存服务实例
jira_cache_service: Optional[JiraCacheService] = None

# 网络监控实例
network_monitor: Optional[NetworkMonitor] = None

if ENABLE_CACHE_SERVICE:
    try:
        # 加载缓存配置
        cache_config = {
            'proxy_nodes': [],
            'timeout': {
                'connect': int(os.environ.get('CACHE_CONNECT_TIMEOUT', '10')),
                'read': int(os.environ.get('CACHE_READ_TIMEOUT', '30')),
                'total': int(os.environ.get('CACHE_TOTAL_TIMEOUT', '60'))
            },
            'retry': {
                'max_attempts': int(os.environ.get('CACHE_RETRY_ATTEMPTS', '3')),
                'retry_delay': int(os.environ.get('CACHE_RETRY_DELAY', '1'))
            },
            'cache': {
                'enabled': True,
                'ttl': {
                    'board_data': int(os.environ.get('CACHE_TTL_BOARD', '300')),
                    'field_data': int(os.environ.get('CACHE_TTL_FIELD', '1800')),
                    'search_results': int(os.environ.get('CACHE_TTL_SEARCH', '120')),
                    'operations': 0
                },
                'cache_dir': os.environ.get('CACHE_DIR', 'data_cache')
            },
            'monitoring': {
                'enabled': ENABLE_NETWORK_MONITOR,
                'health_check_interval': int(os.environ.get('HEALTH_CHECK_INTERVAL', '30')),
                'node_timeout': int(os.environ.get('NODE_TIMEOUT', '10')),
                'log_level': os.environ.get('MONITOR_LOG_LEVEL', 'INFO')
            },
            'fallback': {
                'enabled': True,
                'use_local_cache': True,
                'cache_ttl_extend': int(os.environ.get('FALLBACK_CACHE_TTL_EXTEND', '300'))
            }
        }

        # 从环境变量加载代理节点配置
        # 格式: PROXY_NODES=[{"name":"mini","base_url":"http://localhost:8080/jira_proxy","enabled":true}]
        proxy_nodes_env = os.environ.get('PROXY_NODES')
        if proxy_nodes_env:
            try:
                cache_config['proxy_nodes'] = json.loads(proxy_nodes_env)
            except json.JSONDecodeError:
                print(f"[Network] PROXY_NODES 配置解析失败，使用空列表")
        else:
            # 默认本地节点（用于测试）
            cache_config['proxy_nodes'] = [
                {
                    'name': 'mini',
                    'base_url': DEFAULT_PROXY_BASE_URL,
                    'enabled': True,
                    'weight': 1
                }
            ]

        jira_cache_service = get_jira_cache_service(cache_config)
        print(f"[Network] Jira 缓存服务已启用，节点数: {len(cache_config['proxy_nodes'])}")
    except Exception as e:
        print(f"[Network] 缓存服务初始化失败: {e}")

if ENABLE_NETWORK_MONITOR and jira_cache_service:
    try:
        # 使用缓存服务中的节点配置
        nodes_config = jira_cache_service.config.get('proxy_nodes', [])
        monitor_config = {
            'health_check_interval': int(os.environ.get('HEALTH_CHECK_INTERVAL', '30')),
            'node_timeout': int(os.environ.get('NODE_TIMEOUT', '10')),
            'success_rate_threshold': float(os.environ.get('SUCCESS_RATE_THRESHOLD', '0.9')),
            'latency_threshold_ms': int(os.environ.get('LATENCY_THRESHOLD_MS', '1000')),
            'error_threshold': int(os.environ.get('ERROR_THRESHOLD', '5')),
            'alert_retention_hours': int(os.environ.get('ALERT_RETENTION_HOURS', '24')),
            'log_level': os.environ.get('MONITOR_LOG_LEVEL', 'INFO')
        }

        network_monitor = get_network_monitor(nodes_config, monitor_config)

        # 定义告警回调
        def alert_callback(alert):
            """告警回调函数"""
            print(f"[Alert] {alert['level']}: {alert['message']}")

        network_monitor.start(alert_callback=alert_callback)
        print(f"[Network] 网络监控已启用")
    except Exception as e:
        print(f"[Network] 网络监控初始化失败: {e}")

board_service.set_jira_cache_service(jira_cache_service)

# --- 启动时数据源状态摘要 ---
def _print_datasource_status():
    print("\n" + "=" * 60)
    print("  数据源状态")
    print("=" * 60)
    has_auth = bool(jira_svc.headers.get("Authorization"))
    has_cookies = bool(jira_svc.cookies)
    print(f"  [Jira直连]  Auth={'OK' if has_auth else 'MISSING'}  Cookies={'ON' if has_cookies else 'OFF'}  SSL={jira_svc.ssl_verify}")
    if jira_cache_service:
        nodes = getattr(jira_cache_service, '_nodes', [])
        node_info = ", ".join(f"{getattr(n, 'name', '?')}@{getattr(n, 'base_url', '?')}" for n in nodes)
        print(f"  [Mini代理]  ENABLED  Nodes: {node_info}")
    else:
        print(f"  [Mini代理]  DISABLED (ENABLE_CACHE_SERVICE=false)")
    cache_info = jira_svc.get_cache_info()
    if cache_info.get("exists"):
        print(f"  [本地缓存]  {cache_info.get('count', 0)} 条, 更新: {cache_info.get('timestamp', 'unknown')}")
    else:
        print(f"  [本地缓存]  无缓存文件")
    print("=" * 60 + "\n")

_print_datasource_status()



class QueryRequest(BaseModel):
    query: str
    api_key: Optional[str] = None
    images: List[str] = [] # Base64 strings
    model_provider: str = "" # 空字符串使用llm_config默认值; 可选: gemini, openai_compatible, minimax
    model_name: str = ""
    base_url: str = ""

class AnalysisStatusRequest(BaseModel):
    issue_keys: List[str]

class BoardQueryRequest(BaseModel):
    q: str
    top_k: int = 5
    min_score: float = 0.6

class AnalyzeStreamRequest(BaseModel):
    query: str
    images: List[str] = []
    api_key: str
    provider: str = ""
    model_name: str = ""
    base_url: str = ""
    use_cache: bool = True


class GenerateReplyRequest(BaseModel):
    issue_key: str
    force: bool = False
    force_pass_gate1: bool = False
    force_pass_gate2: bool = False


class BootstrapRequest(BaseModel):
    username: str
    password: str
    display_name: str = "管理员"


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str
    role: str = "member"
    current_project: Optional[str] = None
    project_modules: Optional[dict] = None


class JiraBindingUpdateRequest(BaseModel):
    jira_username: str
    jira_api_token: str
    jira_base_url: str = ""


class JiraSessionBindingRequest(BaseModel):
    jsessionid: str
    xsrf_token: Optional[str] = ""
    jira_base_url: Optional[str] = ""


# Absolute path to frontend directories（始终基于代码目录，不跟随 DEMO_RUNTIME_DIR）
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.normpath(os.path.join(_CODE_DIR, "../frontend"))
FRONTEND_ASSETS_DIR = os.path.join(FRONTEND_DIR, "assets")

# Mount static assets only from the dedicated assets directory.
app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS_DIR), name="assets")

# Mount StackEdit as self-hosted local service (no external stackedit.io dependency)
_STACKEDIT_DIR = os.path.join(FRONTEND_DIR, "stackedit")
if os.path.isdir(_STACKEDIT_DIR):
    app.mount("/stackedit", StaticFiles(directory=_STACKEDIT_DIR, html=True), name="stackedit")

# Mount conclusion directory for exploration assets (screenshots, prototypes, findings)
_CONCLUSION_DIR = os.path.join(os.path.dirname(FRONTEND_DIR), "..", "conclusion")
_CONCLUSION_DIR = os.path.normpath(_CONCLUSION_DIR)
if os.path.isdir(_CONCLUSION_DIR):
    app.mount("/conclusion", StaticFiles(directory=_CONCLUSION_DIR, html=True), name="conclusion")


def frontend_html_response(filename: str) -> FileResponse:
    return FileResponse(
        os.path.join(FRONTEND_DIR, filename),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

def _build_session_response(payload: Dict[str, Any], session_token: str, max_age: Optional[int] = None) -> JSONResponse:
    response = JSONResponse(content=payload)
    cookie_kwargs: Dict[str, Any] = {
        "key": SESSION_COOKIE_NAME,
        "value": session_token,
        "httponly": True,
        "samesite": "lax",
        "secure": False,
    }
    if max_age is not None:
        cookie_kwargs["max_age"] = max_age
    else:
        cookie_kwargs["max_age"] = 60 * 60 * 24
    response.set_cookie(**cookie_kwargs)
    return response


@app.get("/")
def read_root():
    return frontend_html_response("index.html")

@app.get("/login.html")
def read_login_page():
    return frontend_html_response("login.html")

@app.get("/search.html")
def read_search_page():
    return frontend_html_response("index.html")

@app.get("/kb.html")
def read_kb_page():
    return frontend_html_response("kb.html")


@app.get("/settings.html")
def read_settings_page():
    return frontend_html_response("settings.html")

@app.get("/guide.html")
def read_guide_page():
    return frontend_html_response("guide.html")

@app.get("/agents.html")
def read_agents_page(request: Request):
    cu = request.state.current_user
    if not cu:
        return RedirectResponse("/login.html?next=/agents.html", status_code=302)
    if cu.get("role") != "admin" and not cu.get("is_demo"):
        return RedirectResponse("/login.html?next=/agents.html&reason=admin_required", status_code=302)
    return frontend_html_response("agents.html")



# /demo/* routes — local dev testing of demo path behavior (QCL uses nginx alias)
_DEMO_HTML_MAP = {
    "board.html": "board.html",
    "agents.html": "agents.html",
    "login.html": "login.html",
    "kb.html": "kb.html",
    "settings.html": "settings.html",
    "guide.html": "guide.html",
}

@app.get("/demo/")
def read_demo_index():
    return frontend_html_response("board.html")

@app.get("/demo/{page}")
def read_demo_page(page: str):
    fn = _DEMO_HTML_MAP.get(page)
    if fn:
        return frontend_html_response(fn)
    from fastapi import HTTPException
    raise HTTPException(status_code=404)




@app.get("/api/auth/bootstrap-status")
def get_bootstrap_status():
    return {"bootstrap_required": not auth_service.has_users()}


@app.post("/api/auth/bootstrap")
def bootstrap_admin_account(request: BootstrapRequest, raw_request: Request):
    if auth_service.has_users():
        raise HTTPException(status_code=409, detail="Bootstrap already completed")

    user = auth_service.bootstrap_admin(request.username, request.password, request.display_name)
    session_token = auth_service.create_session(
        user["id"],
        user_agent=raw_request.headers.get("user-agent", ""),
        ip=get_request_ip(raw_request),
    )
    auth_service.log_audit(user["id"], "bootstrap_admin", "user", user["id"], {"username": user["username"]})
    return _build_session_response({"status": "success", "user": user}, session_token)


@app.post("/api/auth/login")
def login(request: LoginRequest, raw_request: Request):
    user = auth_service.authenticate(request.username, request.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    ttl_hours = 24 * 30 if request.remember_me else 24
    session_token = auth_service.create_session(
        user["id"],
        user_agent=raw_request.headers.get("user-agent", ""),
        ip=get_request_ip(raw_request),
        ttl_hours=ttl_hours,
    )
    auth_service.log_audit(user["id"], "login", "session", user["id"], {"remember_me": request.remember_me})
    cookie_max_age = 60 * 60 * 24 * 30 if request.remember_me else None
    return _build_session_response({"status": "success", "user": user}, session_token, max_age=cookie_max_age)


@app.post("/api/auth/logout")
def logout(request: Request):
    user = require_authenticated_user(request)
    auth_service.delete_session(request.state.session_token)
    auth_service.log_audit(user["id"], "logout", "session", user["id"], {})
    response = JSONResponse(content={"status": "success"})
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/api/auth/me")
def get_current_session_user(request: Request):
    user = require_authenticated_user(request)
    return {"user": user}


# ── Skill Device Token 端点（/api/auth 前缀已在 PUBLIC_PREFIXES 内，无需额外白名单）────

class DeviceTokenRequest(BaseModel):
    username: str
    password: str
    client_fingerprint: str
    label: str = ""

class DeviceVerifyRequest(BaseModel):
    token: str
    client_fingerprint: str

class DeviceRevokeRequest(BaseModel):
    token: str
    client_fingerprint: str

@app.post("/api/auth/device-token")
def issue_device_token(req: DeviceTokenRequest):
    try:
        token = auth_service.issue_device_token(req.username, req.password, req.client_fingerprint, req.label)
    except ValueError:
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    with auth_service._connect() as conn:
        row = conn.execute(
            """SELECT users.id, users.display_name FROM device_tokens
               JOIN users ON users.id = device_tokens.user_id
               WHERE device_tokens.client_fingerprint = ? AND device_tokens.revoked = 0""",
            (req.client_fingerprint,),
        ).fetchone()
    return {"token": token, "user_id": row["id"] if row else "", "display_name": row["display_name"] if row else ""}

@app.post("/api/auth/device-verify")
def verify_device_token(req: DeviceVerifyRequest):
    user = auth_service.verify_device_token(req.token, req.client_fingerprint)
    if not user:
        return {"ok": False, "user_id": "", "display_name": ""}
    return {"ok": True, "user_id": user["id"], "display_name": user["display_name"]}

@app.post("/api/auth/device-revoke")
def revoke_device_token(req: DeviceRevokeRequest):
    ok = auth_service.revoke_device_token(req.token, req.client_fingerprint)
    return {"ok": ok}


@app.patch("/api/user/settings")
def update_user_settings(request: Request, payload: dict):
    user = require_authenticated_user(request)
    if "current_project" in payload:
        pk = str(payload["current_project"]).strip().upper()
        if pk:
            auth_service.update_current_project(user["id"], pk)
            # Refresh cached user in session
            request.state.current_user = auth_service.get_user_by_session(
                request.cookies.get(SESSION_COOKIE_NAME, "")
            )
    if "project_modules" in payload:
        pm = payload["project_modules"]
        if isinstance(pm, dict):
            auth_service.update_user_modules(user["id"], pm)
            request.state.current_user = auth_service.get_user_by_session(
                request.cookies.get(SESSION_COOKIE_NAME, "")
            )
    return {"status": "ok"}


@app.get("/api/admin/users")
def get_admin_users(request: Request):
    require_admin_user(request)
    return {"users": auth_service.list_users()}


@app.post("/api/admin/users")
def create_admin_user(payload: CreateUserRequest, request: Request):
    admin = require_admin_user(request)
    user = auth_service.create_user(
        payload.username,
        payload.password,
        payload.display_name,
        role=payload.role,
        created_by=admin["id"],
        project_modules=payload.project_modules,
    )
    if payload.current_project:
        auth_service.update_current_project(user["id"], payload.current_project.strip().upper())
        user["current_project"] = payload.current_project.strip().upper()
    auth_service.log_audit(admin["id"], "create_user", "user", user["id"], {"role": user["role"]})
    return {"status": "success", "user": user}


@app.patch("/api/admin/users/{user_id}")
def update_admin_user(user_id: str, payload: dict, request: Request):
    require_admin_user(request)
    if "project_modules" in payload and isinstance(payload["project_modules"], dict):
        auth_service.update_user_modules(user_id, payload["project_modules"])
    if "current_project" in payload and payload["current_project"]:
        auth_service.update_current_project(user_id, str(payload["current_project"]).strip().upper())
    return {"status": "ok"}


@app.post("/api/admin/jira-session/refresh")
def admin_jira_session_refresh(request: Request):
    """管理员触发 Jira session 全局刷新（无需用户 token）。供 refresh_jira_session.sh 调用。"""
    try:
        from services.jira_session_refresher import JiraSessionRefresher
        meta = JiraSessionRefresher.get_instance().refresh_now()
        return {"status": "ok", **meta}
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.post("/api/admin/reset-demo")
def reset_demo(request: Request):
    """Demo 沙箱重置（仅 IS_DEMO_INSTANCE=true 时有效）."""
    if not _IS_DEMO:
        raise HTTPException(status_code=403, detail="非 Demo 实例，拒绝重置")
    import subprocess, shutil
    reset_script = os.path.join(os.path.dirname(__file__), "scripts", "reset_demo.sh")
    if not os.path.isfile(reset_script):
        raise HTTPException(status_code=500, detail="reset_demo.sh 不存在")
    if sys.platform == "win32":
        return {"status": "error", "message": "Demo 重置脚本在 Windows 上不支持"}
    try:
        result = subprocess.run(
            ["/bin/bash", reset_script],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return {"status": "error", "output": result.stderr or result.stdout}
        return {"status": "success", "output": result.stdout[-2000:]}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "reset 超时"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/settings/profile")
def get_profile_settings(request: Request):
    return {"user": require_authenticated_user(request)}


@app.get("/api/settings/jira-binding")
def get_jira_binding(request: Request):
    user = require_authenticated_user(request)
    binding = auth_service.get_jira_binding_summary(user["id"])
    return {"binding": binding}


@app.put("/api/settings/jira-binding")
def save_jira_binding(payload: JiraBindingUpdateRequest, request: Request):
    user = require_authenticated_user(request)
    binding = auth_service.upsert_jira_binding(
        user["id"],
        jira_username=payload.jira_username,
        jira_api_token=payload.jira_api_token,
        jira_base_url=payload.jira_base_url,
    )
    auth_service.log_audit(
        user["id"],
        "update_jira_binding",
        "jira_binding",
        user["id"],
        {
            "jira_username": payload.jira_username,
            "jira_base_url": payload.jira_base_url,
            "jira_api_token": mask_jira_token(payload.jira_api_token),
        },
    )
    return {"status": "success", "binding": binding}


@app.post("/api/settings/jira-session-binding")
def save_jira_session_binding(payload: JiraSessionBindingRequest, request: Request):
    """session_cookie 模式绑定：把用户从 Chrome 复制的 JSESSIONID 存起来。

    专为用友内部 Jira（MFA 挡 Basic Auth）设计的认证模式。
    """
    user = require_authenticated_user(request)
    jsessionid = (payload.jsessionid or "").strip()
    if not jsessionid:
        raise HTTPException(status_code=400, detail="JSESSIONID 不能为空")

    binding = auth_service.upsert_jira_session_binding(
        user_id=user["id"],
        jsessionid=jsessionid,
        xsrf_token=(payload.xsrf_token or "").strip(),
        jira_base_url=(payload.jira_base_url or "").strip(),
    )
    auth_service.log_audit(
        user["id"],
        "update_jira_session_binding",
        "jira_binding",
        user["id"],
        {
            "jira_base_url": (payload.jira_base_url or "").strip(),
            "jsessionid": mask_jira_token(jsessionid),
            "xsrf_token": mask_jira_token((payload.xsrf_token or "").strip()),
        },
    )
    return {"status": "success", "binding": binding}


_jira_status_cache: dict = {}
_JIRA_STATUS_TTL = 30  # 秒


@app.post("/api/settings/jira-session-bind")
def jira_session_bind(payload: JiraSessionBindingRequest, request: Request):
    """[铁论方案 Layer 2] 带 Jira myself 试探性校验的 JSESSIONID 绑定。
    校验失败（403/网络错误/其他非 200）不阻断保存，仅影响 verified 字段。
    仅 401（JSESSIONID 字段本身无效）才拒绝保存。
    """
    user = require_authenticated_user(request)
    js = (payload.jsessionid or "").strip()
    if len(js) < 10:
        raise HTTPException(status_code=400, detail="JSESSIONID 看起来不正确（太短）")

    role = os.environ.get("AITICKET_ROLE", "mini").lower()
    if role == "qcl":
        mini_proxy = os.environ.get("MINI_PROXY_URL", "http://127.0.0.1:5001")
        proxies: dict = {"http": mini_proxy, "https": mini_proxy}
    else:
        # 必须显式禁用系统代理（macOS Surge/Clash 会拦截 HTTPS 请求返回伪 403）
        proxies = {"http": None, "https": None}

    jira_name = ""
    verified = False
    verify_reason = ""

    try:
        r = requests.get(
            "https://jira.example.com/rest/api/2/myself",
            cookies={"JSESSIONID": js},
            proxies=proxies,
            verify=False,
            timeout=10,
        )
        if r.status_code == 200:
            jira_name = r.json().get("name", "")
            verified = True
        elif r.status_code == 401:
            # 401 = JSESSIONID 本身无效，唯一拒绝保存的情况
            raise HTTPException(status_code=401, detail="JSESSIONID 已过期，请重新复制")
        elif r.status_code == 403:
            # IP 风控 / CAPTCHA / Surge 拦截：session 可能有效，保存但标记未验证
            verify_reason = "IP 风控（403），session 已保存，后续业务调用将自检"
            print(f"[JiraBind] Jira 返回 403（IP 风控），跳过校验直接保存 session")
        else:
            verify_reason = f"Jira 返回 {r.status_code}，session 已保存"
    except HTTPException:
        raise
    except Exception as exc:
        hint = "（QCL 请确认 Mini 代理可达）" if role == "qcl" else ""
        verify_reason = f"网络异常{hint}，session 已保存"
        print(f"[JiraBind] 网络异常，session 仍保存：{exc}")

    xsrf = (payload.xsrf_token or "").strip()
    existing = auth_service.get_jira_binding_summary(user["id"])
    base_url = (payload.jira_base_url or existing.get("jira_base_url") or "").strip()

    auth_service.upsert_jira_session_binding(
        user_id=user["id"],
        jsessionid=js,
        xsrf_token=xsrf,
        jira_base_url=base_url,
    )
    auth_service.log_audit(
        user["id"], "jira_session_bind", "jira_binding", user["id"],
        {"jira_name": jira_name, "jsessionid": mask_jira_token(js), "verified": verified},
    )
    _jira_status_cache.pop(user["id"], None)
    result = {"status": "success", "verified": verified, "jira_name": jira_name, "xsrf_present": bool(xsrf)}
    if verify_reason:
        result["reason"] = verify_reason
    return result


@app.get("/api/system/jira-session-status")
def jira_session_status_endpoint(request: Request):
    """[铁论方案 Layer 2] 探活当前用户 Jira session 绑定状态；30s TTL 缓存。
    状态值：active / expired / unverified / none
    403 = IP 风控，返回 unverified（不判定 expired）。仅 401 才是真正过期。
    """
    user = require_authenticated_user(request)
    uid = user["id"]
    now = time.time()
    if uid in _jira_status_cache:
        ts, cached = _jira_status_cache[uid]
        if now - ts < _JIRA_STATUS_TTL:
            return cached

    cookies = auth_service.get_jira_session_cookies(uid)
    if not cookies or not cookies.get("JSESSIONID"):
        out = {"state": "none", "jira_name": ""}
        _jira_status_cache[uid] = (now, out)
        return out

    role = os.environ.get("AITICKET_ROLE", "mini").lower()
    if role == "qcl":
        mini_proxy = os.environ.get("MINI_PROXY_URL", "http://127.0.0.1:5001")
        proxies: dict = {"http": mini_proxy, "https": mini_proxy}
    else:
        # 必须显式禁用系统代理（macOS Surge/Clash 会拦截 HTTPS 请求）
        proxies = {"http": None, "https": None}

    try:
        _probe_cookies = {"JSESSIONID": cookies["JSESSIONID"]}
        if cookies.get("xsrf_token"):
            _probe_cookies["atlassian.xsrf.token"] = cookies["xsrf_token"]
        r = requests.get(
            "https://jira.example.com/rest/api/2/myself",
            cookies=_probe_cookies,
            headers={
                # 用友 Jira 反爬：Python 默认 UA → 403 Automated access forbidden
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                "Accept": "application/json",
            },
            proxies=proxies,
            verify=False,
            timeout=5,
        )
        if r.status_code == 200:
            out = {"state": "active", "jira_name": r.json().get("name", "")}
        elif r.status_code == 401:
            out = {"state": "expired", "jira_name": ""}
        else:
            # 403 = IP 风控（UA 已伪装），其他临时错误：不判定过期
            out = {"state": "unverified", "jira_name": ""}
    except Exception:
        out = {"state": "unverified", "jira_name": ""}

    _jira_status_cache[uid] = (now, out)
    return out


@app.post("/api/settings/jira-session-auto-refresh")
def auto_refresh_jira_session(request: Request):
    """通过 JiraSessionRefresher 从 Chrome 解密 cookies，
    写入 /tmp/jira-session-{username}.json，并把 JSESSIONID 同步到数据库绑定。"""
    user = require_authenticated_user(request)
    username = user["username"]

    try:
        from services.jira_session_refresher import JiraSessionRefresher
        refresher = JiraSessionRefresher.get_instance()
        meta = refresher.refresh_now(user=username)
    except Exception as exc:
        return {"status": "error", "message": f"Refresher 异常: {exc}"}

    if meta.get("cookie_count", 0) == 0:
        return {"status": "error", "message": "刷新未获取到 cookies（Chrome 可能未运行或未登录）"}

    # 读取刚生成的 per-user session 文件，提取 JSESSIONID 存数据库
    from services.host_context import session_path as _session_path
    state_path = _session_path(user=username, prefix="jira")
    if not os.path.exists(state_path):
        return {"status": "error", "message": f"未找到 session 文件: {state_path}"}

    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except Exception as exc:
        return {"status": "error", "message": f"session 文件解析失败: {exc}"}

    cookie_map = {c.get("name"): c.get("value") for c in state.get("cookies", [])}
    jsession = (cookie_map.get("JSESSIONID") or "").strip()
    if not jsession:
        return {"status": "error", "message": "session 文件中没有 JSESSIONID"}

    xsrf = (cookie_map.get("atlassian.xsrf.token") or "").strip()
    # 保留绑定里已有的 base_url
    existing = auth_service.get_jira_binding_summary(user["id"])
    base_url = existing.get("jira_base_url") or ""

    binding = auth_service.upsert_jira_session_binding(
        user_id=user["id"],
        jsessionid=jsession,
        xsrf_token=xsrf,
        jira_base_url=base_url,
    )
    auth_service.log_audit(
        user["id"],
        "auto_refresh_jira_session",
        "jira_binding",
        user["id"],
        {"jsessionid": mask_jira_token(jsession), "xsrf_token": mask_jira_token(xsrf)},
    )
    return {
        "status": "success",
        "message": "已从 Chrome 自动刷新 session",
        "binding": binding,
    }


# --- Jira Session Push / Peek (Mini → QCL internal sync) ---

@app.post("/internal/jira-session/push")
def internal_jira_session_push(request: Request):
    """Mini 推送 Chrome-解密的 Jira session 到此实例（QCL）。
    Bearer token 由环境变量 JIRA_SESSION_PUSH_TOKEN 控制。"""
    import os as _os, json as _json
    expected_token = _os.environ.get("JIRA_SESSION_PUSH_TOKEN", "")
    if not expected_token:
        return JSONResponse({"error": "push not configured"}, status_code=503)
    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {expected_token}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = _json.loads(request.state._body if hasattr(request.state, "_body") else b"")
    except Exception:
        import asyncio as _aio
        body = {}
    try:
        from services.jira_session_refresher import JiraSessionRefresher
        JiraSessionRefresher.get_instance().receive_push(body)
        meta = body.get("_meta", {})
        return {"status": "ok", "source": meta.get("source"), "cookie_count": meta.get("cookie_count")}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/internal/jira-session/push-body")
async def internal_jira_session_push_body(request: Request):
    """Mini → QCL 推送（异步版，正确解析 body）。"""
    import os as _os
    expected_token = _os.environ.get("JIRA_SESSION_PUSH_TOKEN", "")
    if not expected_token:
        return JSONResponse({"error": "push not configured"}, status_code=503)
    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {expected_token}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse({"error": f"bad json: {exc}"}, status_code=400)
    try:
        from services.jira_session_refresher import JiraSessionRefresher
        JiraSessionRefresher.get_instance().receive_push(body)
        meta = body.get("_meta", {})
        return {"status": "ok", "source": meta.get("source"), "cookie_count": meta.get("cookie_count")}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/internal/jira-session/peek")
def internal_jira_session_peek(request: Request):
    """健康检查：返回 session 文件的 mtime + source，不返回 cookie 值。"""
    import os as _os, time as _t
    from services.host_context import session_path as _session_path
    expected_token = _os.environ.get("JIRA_SESSION_PUSH_TOKEN", "")
    if expected_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {expected_token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    path = _session_path()
    if not _os.path.exists(path):
        return {"exists": False}
    try:
        mtime = _os.path.getmtime(path)
        age_sec = int(_t.time() - mtime)
        with open(path) as f:
            import json as _json
            state = _json.load(f)
        meta = state.get("_meta", {})
        return {
            "exists": True,
            "age_sec": age_sec,
            "source": meta.get("source", "unknown"),
            "cookie_count": len(state.get("cookies", [])),
            "refreshed_at": meta.get("refreshed_at"),
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# --- PM Session Auto-Refresh (Chrome 解密) ---

@app.post("/api/pm/session/me/auto-refresh")
def auto_refresh_pm_session(request: Request):
    """从 Chrome 解密 pm.example.com cookies，自动写入 PM 钱包。"""
    import subprocess, re as _re
    user = require_authenticated_user(request)
    username = user["username"]
    pm_user = request.headers.get("X-PM-User", "").strip() or username

    script = Path(__file__).parent / "scripts" / "refresh_pm_session.sh"
    if not script.exists():
        return {"status": "error", "message": f"脚本不存在: {script}"}

    if sys.platform == "win32":
        return {"status": "error", "message": "PM session 刷新脚本在 Windows 上不支持"}

    try:
        result = subprocess.run(
            ["bash", str(script), "--user", pm_user],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "刷新脚本超时（30s）"}
    except Exception as exc:
        return {"status": "error", "message": f"脚本执行异常: {exc}"}

    if result.returncode != 0:
        return {"status": "error", "message": (result.stderr or result.stdout or "刷新失败")[-500:]}

    from services.host_context import session_path as _session_path
    state_path = _session_path(user=pm_user, prefix="pm")
    if not os.path.exists(state_path):
        return {"status": "error", "message": f"未找到 session 文件: {state_path}"}

    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except Exception as exc:
        return {"status": "error", "message": f"session 文件解析失败: {exc}"}

    from services.pm_wallet_service import save_user_token
    record = save_user_token(pm_user, {
        "yht_access_token": state.get("yht_access_token", ""),
        "tenant_info": state.get("tenant_info", "0000"),
        "extra_cookies": state.get("ycap_cookies", {}),
        "proxy_endpoint": "",
    })
    return {
        "status": "success",
        "message": f"已从 Chrome 自动刷新 PM session",
        "expires_at": record.get("expires_at", ""),
        "ycap_count": len(state.get("ycap_cookies", {})),
    }


# --- PM Binding ---

@app.get("/api/settings/pm-binding")
def get_pm_binding_status(request: Request):
    user = require_authenticated_user(request)
    summary = auth_service.get_pm_binding_summary(user["id"])
    return summary


@app.put("/api/settings/pm-binding")
def save_pm_binding(body: dict = Body(...), request: Request = None):
    user = require_authenticated_user(request)
    pm_token = body.get("pm_token", "")
    tenant_info = body.get("tenant_info", "0000")
    if not pm_token:
        raise HTTPException(status_code=400, detail="pm_token is required")
    result = auth_service.upsert_pm_binding(user["id"], pm_token, tenant_info)
    auth_service.log_audit(user["id"], "update_pm_binding", "pm_binding", user["id"], {"tenant_info": tenant_info})
    return {"status": "success", "binding": result}


def build_request_pm_service(request: Request):
    """Build a PMModuleService with per-user PM token override if available."""
    from services.pm_module_service import PMModuleService
    user = get_current_user(request)
    if user:
        pm_creds = auth_service.get_pm_binding_token(user["id"])
        if pm_creds:
            svc = PMModuleService("original_demand")
            svc._token_override = pm_creds["token"]
            svc._tenant_override = pm_creds.get("tenant_info")
            return svc
    return None  # Caller falls back to default singleton


@app.post("/analyze")
def trigger_analysis(background_tasks: BackgroundTasks):
    def run_analysis_task():
        print("Starting analysis...")
        analyzer = TicketAnalyzer()
        analyzer.run()
        print("Analysis complete. Reloading search index...")
        search_engine.reload_data()
        print("Search index reloaded.")

    background_tasks.add_task(run_analysis_task)
    return {"message": "Analysis started in background"}

@app.post("/query")
def search_tickets(request: QueryRequest):
    from fastapi.responses import StreamingResponse
    import json
    llm_runtime = resolve_effective_llm_runtime(
        feature="smart_reply",
        provider=request.model_provider,
        api_key=request.api_key or "",
        model_name=request.model_name,
        base_url=request.base_url,
    )

    def stream_response():
        # 1. Local Search (Recall) - Quick because of in-memory cache
        search_results = search_engine.search(request.query)

        # Send search results first as a single JSON line
        yield json.dumps({"search_results": search_results}) + "\n---\n"

        kb_summary = kb_runtime_service.answer_question(
            query=request.query,
            mode="short",
            api_key=llm_runtime["api_key"],
            provider=llm_runtime["provider"],
            model_name=llm_runtime["model_name"],
            base_url=llm_runtime["base_url"],
        )
        yield json.dumps({"kb_summary": kb_summary}) + "\n---\n"

        # 2. LLM Analysis (if API key provided)
        if llm_runtime["api_key"]:
            # Extract top recall items for context
            context_docs = search_results.get("results", [])[:5]
            gen = llm_service.analyze_query(
                query=request.query, 
                images=request.images, 
                context_docs=context_docs, 
                api_key=llm_runtime["api_key"],
                provider=llm_runtime["provider"],
                model_name=llm_runtime["model_name"],
                base_url=llm_runtime["base_url"]
            )
            for chunk in gen:
                if chunk:
                    yield json.dumps({"llm_chunk": chunk}) + "\n---\n"
    
    return StreamingResponse(stream_response(), media_type="text/event-stream")

@app.post("/api/analyze/stream")
async def stream_analysis(request: AnalyzeStreamRequest):
    """
    流式分析API端点

    返回SSE格式的流式响应，包含搜索结果和AI分析内容

    请求格式:
    {
        "query": "用户问题",
        "images": ["data:image/jpeg;base64,..."],
        "api_key": "API密钥",
        "provider": "gemini",
        "model_name": "",
        "base_url": "",
        "use_cache": true
    }

    SSE事件格式:
    event: start
    data: {"status": "searching", "message": "正在搜索相关知识库..."}

    event: search_results
    data: {"results": [...], "count": 5}

    event: analyzing
    data: {"message": "AI正在分析..."}

    event: content
    data: {"chunk": "文字片段", "index": 0}

    event: done
    data: {"status": "complete", "total_chunks": 45, "duration_ms": 3200, "content": "完整内容"}

    event: error
    data: {"code": "ERROR_CODE", "message": "错误信息"}
    """
    from fastapi.responses import StreamingResponse
    llm_runtime = resolve_effective_llm_runtime(
        feature="smart_reply",
        provider=request.provider,
        api_key=request.api_key or "",
        model_name=request.model_name,
        base_url=request.base_url,
    )

    async def generate_sse():
        # 首先生成缓存key（不依赖搜索结果，只基于query）
        # 这样可以提前检查缓存，避免不必要的搜索
        cache_key = hashlib.md5(request.query.encode()).hexdigest()

        # 检查缓存
        if request.use_cache and llm_runtime["api_key"]:
            cached_data = vector_store_instance.get_query_cache(cache_key)
            if cached_data:
                # 缓存命中，返回缓存的事件
                try:
                    cached_events = json.loads(cached_data.get("content", "[]"))
                    for event in cached_events:
                        event_type = event.get("event", "unknown")
                        event_data = event.get("data", {})
                        yield f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"
                    return
                except Exception as e:
                    # 缓存解析失败，继续执行
                    print(f"[Stream API] Cache parse error: {e}")

        # 缓存未命中或use_cache=False，执行完整流程
        if not llm_runtime["api_key"]:
            # 没有API key，返回错误
            error_event = {
                "event": "error",
                "data": {"code": "NO_API_KEY", "message": "请先在设置中配置 API Key 以启用智能分析。"}
            }
            yield f"event: error\ndata: {json.dumps(error_event['data'], ensure_ascii=False)}\n\n"
            return

        # 执行向量搜索
        search_results = search_engine.search(request.query)
        context_docs = search_results.get("results", [])

        # 重新生成缓存key（包含context_keys）
        context_keys = [doc.get("key") for doc in context_docs]
        cache_content = f"{request.query}:{sorted(context_keys)}"
        cache_key = hashlib.md5(cache_content.encode()).hexdigest()

        # 收集所有事件用于缓存
        events_to_cache = []

        # 调用LLM服务进行流式分析
        llm_stream = llm_service.analyze_query_stream(
            query=request.query,
            images=request.images,
            context_docs=context_docs,
            api_key=llm_runtime["api_key"],
            provider=llm_runtime["provider"],
            model_name=llm_runtime["model_name"],
            base_url=llm_runtime["base_url"]
        )

        for event_dict in llm_stream:
            event_type = event_dict.get("event", "unknown")
            event_data = event_dict.get("data", {})

            # 发送SSE事件
            yield f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"

            # 收集事件用于缓存
            events_to_cache.append(event_dict)

        # 保存到缓存
        if request.use_cache and events_to_cache:
            try:
                vector_store_instance.save_query_cache(
                    cache_key=cache_key,
                    query=request.query,
                    content=json.dumps(events_to_cache, ensure_ascii=False),
                    context_keys=context_keys,
                    ttl_hours=24
                )
            except Exception as e:
                print(f"[Stream API] Failed to save cache: {e}")

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Type": "text/event-stream; charset=utf-8"
        }
    )

@app.get("/index.html")
def read_index_page():
    """问题分析页面（原搜索页面）"""
    return frontend_html_response("index.html")

@app.get("/api/spec/files")
def list_spec_files():
    """List all spec files with their status"""
    files = requirement_service.list_spec_files()
    return [
        {
            "filename": f.filename,
            "file_type": f.file_type,
            "size_bytes": f.size_bytes,
            "modified_time": f.modified_time,
            "has_output": f.has_output,
            "output_files": f.output_files
        }
        for f in files
    ]

@app.get("/api/spec/file/{filename:path}")
def get_spec_file(filename: str):
    """Read content of a spec file — 若有输出文件则返回最新输出，否则返回原始 spec"""
    result = requirement_service.get_latest_content(filename)
    if result["content"] is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="File not found")
    return {"filename": filename, **result}

class SaveFileRequest(BaseModel):
    content: str

@app.put("/api/spec/file/{filename:path}")
def save_spec_file(filename: str, request: SaveFileRequest):
    """Save content to a spec file"""
    success = requirement_service.save_file_content(filename, request.content)
    if not success:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Failed to save file")
    return {"status": "success"}

class UploadFileRequest(BaseModel):
    filename: str
    content: str

@app.post("/api/spec/file")
def upload_spec_file(request: UploadFileRequest):
    """Upload a new spec file"""
    success = requirement_service.upload_file(request.filename, request.content)
    if not success:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Failed to upload file")
    return {"status": "success", "filename": request.filename}

@app.get("/api/templates")
def list_templates():
    """List available templates"""
    return requirement_service.list_templates()

@app.get("/api/template/{filename:path}")
def get_template(filename: str):
    """Read content of a template file"""
    content = requirement_service.get_template_content(filename)
    if content is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Template not found")
    return {"filename": filename, "content": content}

class GenerateRequirementRequest(BaseModel):
    spec_file: str
    template: str
    output_formats: List[str] = ["md"]
    final_decision_notes: str = ""
    draft_context: Optional[Dict[str, Any]] = None
    api_key: Optional[str] = None
    provider: str = ""
    model_name: str = ""
    base_url: str = ""

@app.post("/api/requirements/generate")
def start_requirement_generation(request: GenerateRequirementRequest):
    """Start a requirement generation task"""
    llm_runtime = resolve_effective_llm_runtime(
        feature="spec_gen",
        provider=request.provider,
        api_key=request.api_key or "",
        model_name=request.model_name,
        base_url=request.base_url,
    )
    task_id = requirement_service.start_generation(
        spec_file=request.spec_file,
        template=request.template,
        output_formats=request.output_formats,
        final_decision_notes=request.final_decision_notes,
        draft_context=request.draft_context,
        api_key=llm_runtime["api_key"],
        provider=llm_runtime["provider"],
        model_name=llm_runtime["model_name"],
        base_url=llm_runtime["base_url"]
    )
    return {"task_id": task_id, "status": "started"}

@app.get("/api/requirements/status/{task_id}")
def get_task_status(task_id: str):
    """Get task status"""
    status = requirement_service.get_task_status(task_id)
    if status is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")
    return status

@app.post("/api/requirements/cancel/{task_id}")
def cancel_task(task_id: str):
    """Cancel a running task"""
    success = requirement_service.cancel_task(task_id)
    return {"success": success}

@app.get("/api/requirements/versions/{spec_file:path}")
def list_versions(spec_file: str):
    """List all versions of a spec file's outputs"""
    return requirement_service.list_versions(spec_file)

@app.get("/api/requirements/version/{version_file:path}")
def get_version_content(version_file: str):
    """Get content of a specific version"""
    content = requirement_service.get_version_content(version_file)
    if content is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Version not found")
    return {"filename": version_file, "content": content}

# --- AI Content Refinement Endpoints ---

@app.get("/api/output/{filename:path}")
def get_output_file(filename: str):
    """Get content of an output file"""
    content = requirement_service.get_output_content(filename)
    if content is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Output file not found")
    return {"filename": filename, "content": content}

class RefineContentRequest(BaseModel):
    filename: str
    content: str
    instruction: str
    section_name: Optional[str] = None
    api_key: Optional[str] = None
    provider: str = ""
    model_name: str = ""
    base_url: str = ""

@app.post("/api/requirements/refine")
def refine_content(request: RefineContentRequest):
    """Refine AI-generated content based on user instructions"""
    if request.section_name:
        # Section-level refinement
        refined = requirement_service.refine_section(
            original_content=request.content,
            section_name=request.section_name,
            user_instruction=request.instruction,
            api_key=request.api_key or "",
            provider=request.provider,
            model_name=request.model_name,
            base_url=request.base_url
        )
    else:
        # Full document refinement
        refined = requirement_service.refine_content(
            original_content=request.content,
            user_instruction=request.instruction,
            api_key=request.api_key or "",
            provider=request.provider,
            model_name=request.model_name,
            base_url=request.base_url
        )
    
    # Save the refined content
    if request.filename:
        requirement_service.save_output_content(request.filename, refined)
    
    return {"content": refined, "filename": request.filename}

class SaveOutputRequest(BaseModel):
    content: str

@app.put("/api/output/{filename:path}")
def save_output_file(filename: str, request: SaveOutputRequest):
    """Save content to an output file"""
    success = requirement_service.save_output_content(filename, request.content)
    if not success:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Failed to save output")
    return {"status": "success"}

# --- Knowledge Base Endpoints ---

@app.get("/api/kb/files")
def list_kb_files():
    """兼容旧接口：返回多源知识库条目列表"""
    return kb_runtime_service.get_manifest()["items"]

@app.get("/api/kb/file/{filename:path}")
def get_kb_file(filename: str):
    """兼容旧接口：按 source_rel_path 查找知识条目"""
    for item in kb_runtime_service.get_manifest()["items"]:
        if item.get("source_rel_path") == filename:
            return item
    raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/kb/manifest")
def get_kb_manifest():
    return kb_runtime_service.get_manifest()


@app.get("/api/kb/content/{content_id}")
def get_kb_content(content_id: str):
    item = kb_runtime_service.get_content(content_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Content not found")
    return item


@app.post("/api/kb/sync")
def sync_kb():
    try:
        return kb_runtime_service.sync(force_refresh=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"KB sync failed: {exc}")


# ── KB Refresh (build+sync+compile cascade) ───────────────────────────────────
import uuid as _uuid
_kb_refresh_tasks: dict[str, dict] = {}


def _run_kb_refresh(task_id: str, force: bool) -> None:
    """Background thread: build → sync → compile-all cascade."""
    _kb_refresh_tasks[task_id]["status"] = "running"
    try:
        # 1. Build (scan orphans, update manifest)
        try:
            from kb_local_builder import KBLocalBuilder
            KBLocalBuilder().build()
            _kb_refresh_tasks[task_id]["step"] = "build_done"
        except Exception as e:
            logger.warning("[kb/refresh] build step failed (non-fatal): %s", e)

        # 2. Sync (rebuild SQLite index from manifest)
        sync_result = kb_runtime_service.sync(force_refresh=True)
        _kb_refresh_tasks[task_id]["step"] = "sync_done"
        _kb_refresh_tasks[task_id]["sync"] = sync_result if isinstance(sync_result, dict) else {}

        # 3. Compile-all (LLM synthesis, incremental or force)
        try:
            from kb_compile_service import get_or_create_compile_service
            svc = get_or_create_compile_service()
            compiled = svc.compile_all()
            _kb_refresh_tasks[task_id]["step"] = "compile_done"
            _kb_refresh_tasks[task_id]["compiled"] = compiled if isinstance(compiled, dict) else {}
        except Exception as e:
            logger.warning("[kb/refresh] compile step failed (non-fatal): %s", e)
            _kb_refresh_tasks[task_id]["step"] = "compile_skipped"

        _kb_refresh_tasks[task_id]["status"] = "done"
    except Exception as e:
        logger.exception("[kb/refresh] task %s failed", task_id)
        _kb_refresh_tasks[task_id]["status"] = "error"
        _kb_refresh_tasks[task_id]["error"] = str(e)


@app.post("/api/kb/refresh", status_code=202)
def kb_refresh(body: dict = Body({})):
    """统一 ingestion 入口：build + sync + compile 级联，异步执行。"""
    import threading
    force = body.get("force", False)
    task_id = f"kbr-{_uuid.uuid4().hex[:12]}"
    _kb_refresh_tasks[task_id] = {"task_id": task_id, "status": "pending", "step": ""}
    t = threading.Thread(target=_run_kb_refresh, args=(task_id, force), daemon=True)
    t.start()
    return {"task_id": task_id, "status": "pending"}


@app.get("/api/kb/refresh/status/{task_id}")
def kb_refresh_status(task_id: str):
    """查询 kb/refresh 任务进度。"""
    task = _kb_refresh_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return task

class KBAnalyzeRequest(BaseModel):
    summary: str
    module_hint: str = ""
    top_k: int = 10
    api_key: Optional[str] = None
    provider: str = ""
    model_name: str = ""
    base_url: str = ""


class KBDraftRequest(BaseModel):
    summary: str
    module_hint: str = ""
    top_k: int = 10
    api_key: Optional[str] = None
    provider: str = ""
    model_name: str = ""
    base_url: str = ""

class KBQuestionRequest(BaseModel):
    query: str
    mode: str = "short"
    api_key: Optional[str] = None
    provider: str = ""
    model_name: str = ""
    base_url: str = ""

class KBReviewRequest(BaseModel):
    summary: str
    draft_markdown: str
    module_hint: str = ""
    top_k: int = 10
@app.post("/api/kb/analyze")
def analyze_kb_file(request: KBAnalyzeRequest):
    """多源知识库分析：返回证据包、章节建议和待确认项"""
    llm_runtime = resolve_effective_llm_runtime(
        feature="req_analysis",
        provider=request.provider,
        api_key=request.api_key or "",
        model_name=request.model_name,
        base_url=request.base_url,
    )
    return kb_runtime_service.analyze(
        summary=request.summary,
        module_hint=request.module_hint,
        top_k=request.top_k,
        llm_config={
            "apiKey": llm_runtime["api_key"],
            "provider": llm_runtime["provider"],
            "modelName": llm_runtime["model_name"],
            "baseUrl": llm_runtime["base_url"],
        },
    )


@app.post("/api/kb/draft")
def draft_kb_prd(request: KBDraftRequest):
    """基于知识证据生成 PRD 初稿"""
    llm_runtime = resolve_effective_llm_runtime(
        feature="spec_gen",
        provider=request.provider,
        api_key=request.api_key or "",
        model_name=request.model_name,
        base_url=request.base_url,
    )
    return kb_runtime_service.draft(
        summary=request.summary,
        module_hint=request.module_hint,
        top_k=request.top_k,
        llm_config={
            "apiKey": llm_runtime["api_key"],
            "provider": llm_runtime["provider"],
            "modelName": llm_runtime["model_name"],
            "baseUrl": llm_runtime["base_url"],
        },
    )


@app.post("/api/kb/review")
def review_kb_prd(request: KBReviewRequest):
    """检查 PRD 草稿的章节和证据覆盖情况"""
    return kb_runtime_service.review(
        summary=request.summary,
        draft_markdown=request.draft_markdown,
        module_hint=request.module_hint,
        top_k=request.top_k,
    )


@app.get("/api/kb/metadata/{content_id}")
def get_kb_metadata(content_id: str):
    """Get KB metadata by content ID"""
    item = kb_runtime_service.get_metadata(content_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Metadata not found")
    return item

@app.get("/api/kb/search")
def search_kb(request: Request, q: str, top_k: int = Query(20, description="返回条数"), source_kind: str = Query("", description="知识源过滤")):
    """Search knowledge base"""
    return kb_runtime_service.search_bundle(q, top_k=top_k, source_kind=source_kind or None, module_boost=getattr(request.state, "current_modules", []))

@app.post("/api/kb/qa")
def ask_kb_question(request: KBQuestionRequest, raw_request: Request, _quota=Depends(require_reply_quota)):
    """Answer user question from knowledge base"""
    log_api_request(raw_request, _quota, query_text=request.query)
    llm_runtime = resolve_effective_llm_runtime(
        feature="smart_reply",
        provider=request.provider,
        api_key=request.api_key or "",
        model_name=request.model_name,
        base_url=request.base_url,
    )
    return kb_runtime_service.answer_question(
        query=request.query,
        mode=request.mode,
        api_key=llm_runtime["api_key"],
        provider=llm_runtime["provider"],
        model_name=llm_runtime["model_name"],
        base_url=llm_runtime["base_url"],
    )


# ─── KB 编译 & 贡献 ────────────────────────────────────────────────────────────

@app.post("/api/kb/compile", status_code=202)
def kb_compile_topic(body: dict = Body({})):
    """提交编译任务（异步执行，立即返回 job_id）。GET /api/kb/jobs/{job_id} 轮询结果。"""
    from services.kb_write_dispatcher import submit
    topic = body.get("topic", "").strip()
    content = body.get("content", "")
    source_ctx = body.get("source_context", {}) or {}
    if not topic and not content:
        raise HTTPException(status_code=400, detail="topic 或 content 至少提供一个")
    job_id = submit("compile", {
        "topic": topic or source_ctx.get("ref_id", "未知话题"),
        "override_content": content or None,
        "extra_metadata": source_ctx or None,
        "llm_config": body.get("llm_config") or None,
    })
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/kb/compile-all", status_code=202)
def kb_compile_all(body: dict = Body({})):
    """批量提交编译任务（每个 topic 一个 job）。返回 job_id 列表。"""
    from services.kb_write_dispatcher import submit
    from kb_compile_service import get_compile_service, KBCompileService
    topics = body.get("topics", None)
    if not topics:
        # 沿用默认高频话题列表
        topics = [
            "打印", "公式", "规则引擎", "流程引擎", "字段权限",
            "审批矩阵", "消息模板", "流程设计器", "组织管理",
        ]
    jobs = [submit("compile", {"topic": t}) for t in topics]
    return {"submitted": len(jobs), "job_ids": jobs}


@app.get("/api/kb/jobs/{job_id}")
def kb_job_status(job_id: str):
    """查询 KB 写操作任务状态（compile / delete / rebuild）。"""
    from services.kb_write_dispatcher import get_job
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/kb/jobs")
def kb_job_list(limit: int = 30):
    """列出最近的 KB 写操作任务。"""
    from services.kb_write_dispatcher import list_jobs
    return {"items": list_jobs(limit)}


@app.get("/api/kb/compiled")
def kb_list_compiled(top_k: int = 50):
    """列出所有 kb_compiled 来源的条目（含完整 content 供前端渲染）"""
    results = kb_runtime_service.hybrid_index.list_by_source_kind("kb_compiled", top_k=top_k)
    return {"count": len(results), "items": results}


@app.delete("/api/kb/compiled/{content_id:path}", status_code=202)
def kb_delete_compiled(content_id: str):
    """提交删除任务（异步执行）。validation 在 API 层同步完成，写操作由 daemon 执行。"""
    from services.kb_write_dispatcher import submit
    doc_row = kb_runtime_service.hybrid_index.conn.execute(
        "SELECT source_kind FROM documents WHERE content_id = ?", (content_id,)
    ).fetchone()
    if not doc_row:
        raise HTTPException(status_code=404, detail=f"content not found: {content_id}")
    if doc_row["source_kind"] != "kb_compiled":
        raise HTTPException(status_code=403, detail="only kb_compiled entries can be deleted via this endpoint")
    job_id = submit("delete", {"content_id": content_id})
    return {"job_id": job_id, "status": "pending", "content_id": content_id}


@app.get("/api/kb/compiled-health")
def kb_compiled_health():
    """返回受保护数据条目数，用于监控 sync 后是否丢失"""
    from kb_hybrid_index import _PRESERVED_SOURCE_KINDS
    counts = {}
    for kind in _PRESERVED_SOURCE_KINDS:
        counts[kind] = kb_runtime_service.hybrid_index.count_by_source_kind(kind)
    total = sum(counts.values())
    return {"total": total, "by_kind": counts, "healthy": total > 0}


@app.post("/api/kb/restore-compiled")
def kb_restore_compiled():
    """从最新的 JSON 备份恢复受保护数据（kb_compiled 等），sync 误清时使用"""
    restored = kb_runtime_service._restore_from_backup()
    if restored == 0:
        raise HTTPException(status_code=404, detail="无可用备份或备份为空，需重新运行 batch_compile_kb.py")
    return {"ok": True, "restored": restored}


@app.post("/api/kb/note")
def kb_quick_note(req: dict):
    """快捷 KB 笔记：在 product_facts.md 指定话题下追加一条事实，并立即重索引。"""
    topic = (req.get("topic") or "").strip()
    content = (req.get("content") or "").strip()
    if not topic:
        raise HTTPException(status_code=422, detail="topic 不能为空")
    if not content:
        raise HTTPException(status_code=422, detail="content 不能为空")

    from pathlib import Path as _Path
    from datetime import datetime as _dt

    facts_path = _Path(__file__).parent / "data" / "product_facts.md"
    if not facts_path.exists():
        raise HTTPException(status_code=500, detail="product_facts.md 不存在")

    md_text = facts_path.read_text(encoding="utf-8")
    date_str = _dt.now().strftime("%Y-%m-%d")
    new_line = f"- {content}（快捷记录，{date_str}）"

    section_header = f"## {topic}"
    if section_header in md_text:
        idx = md_text.index(section_header)
        eol = md_text.index("\n", idx)
        md_text = md_text[:eol + 1] + new_line + "\n" + md_text[eol + 1:]
    else:
        md_text = md_text.rstrip("\n") + f"\n\n{section_header}\n{new_line}\n"

    facts_path.write_text(md_text, encoding="utf-8")

    n = 0
    try:
        from services.search.product_facts_indexer import reindex as _reindex_facts
        n = _reindex_facts(force=True)
    except Exception as e:
        print(f"[kb/note] reindex failed: {e}")

    return {"status": "ok", "topic": topic, "indexed": n}


@app.post("/api/kb/lint")
def kb_lint():
    """KB 健康检查：覆盖率 + 缺失话题"""
    from kb_compile_service import get_compile_service
    svc = get_compile_service()
    if not svc:
        raise HTTPException(status_code=503, detail="kb_compile_service 未初始化")
    return svc.lint()


@app.get("/api/kb/contributions")
def kb_contributions(top_k: int = 50):
    """列出近期 user_contributed 来源的知识条目"""
    results = kb_runtime_service.hybrid_index.list_by_source_kind("user_contributed", top_k=top_k)
    return {"count": len(results), "items": results}


@app.get("/api/kb/enrichment-report")
def kb_enrichment_report():
    """
    KB 自动成长日报 — 供飞书推送。
    读取 kb_enrichment_log.jsonl 最近 24h 的入库记录，生成统计报告。
    """
    import datetime as _dt
    from pathlib import Path as _Path

    BACKEND = _Path(__file__).parent
    LOG_FILE = BACKEND / "data" / "kb_enrichment_log.jsonl"
    CONSOL_LOG = BACKEND / "data" / "kb_consolidation_log.jsonl"

    now = _dt.datetime.now()
    cutoff = now - _dt.timedelta(hours=24)
    cutoff_str = cutoff.isoformat()

    # 1. 读取 enrichment 日志
    enrich_stats = {"extracted": 0, "ingested": 0, "skipped": 0, "conflicts": 0, "pending_review": 0}
    affected_topics = set()
    if LOG_FILE.exists():
        try:
            for line in LOG_FILE.read_text(encoding="utf-8").strip().splitlines():
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                ts = entry.get("timestamp", "")
                if ts < cutoff_str:
                    continue
                action = entry.get("action", "")
                if action in ("enrich", "create"):
                    enrich_stats["ingested"] += 1
                    topic = entry.get("topic", "")
                    if topic:
                        affected_topics.add(topic)
                elif action == "skip":
                    enrich_stats["skipped"] += 1
                elif action == "conflict":
                    enrich_stats["conflicts"] += 1
                elif action == "pending_review":
                    enrich_stats["pending_review"] += 1
                enrich_stats["extracted"] += 1
        except Exception:
            pass

    # 2. 读取 consolidation 日志（最近一条）
    consol_summary = None
    if CONSOL_LOG.exists():
        try:
            lines = CONSOL_LOG.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                consol_summary = json.loads(lines[-1])
        except Exception:
            pass

    # 3. 生成 Markdown 报告
    md = [
        f"## KB 自动成长日报 — {now.strftime('%Y-%m-%d %H:%M')}",
        "",
        "### 过去 24h 知识萃取",
        f"- 萃取数: {enrich_stats['extracted']}",
        f"- 入库数: {enrich_stats['ingested']}",
        f"- 跳过数: {enrich_stats['skipped']}",
        f"- 冲突数: {enrich_stats['conflicts']}",
        f"- 待审核: {enrich_stats['pending_review']}",
    ]

    if affected_topics:
        md += ["", "### 受影响话题", ", ".join(sorted(affected_topics))]

    if consol_summary:
        ts = consol_summary.get("timestamp", "")[:16].replace("T", " ")
        stats = consol_summary.get("stats", {})
        md += [
            "",
            f"### 最近合并运行 ({ts})",
            f"- 话题处理: {stats.get('topics_processed', 0)}",
            f"- 碎片合并: {stats.get('chunks_merged', 0)}",
            f"- 文档归档: {stats.get('docs_archived', 0)}",
        ]

    return {"markdown": "\n".join(md), "stats": enrich_stats, "affected_topics": sorted(affected_topics)}


@app.get("/api/kb/health-report")
def kb_health_report():
    """KB 健康报告 — 调用 kb_consolidator 生成（仅报告模式，不执行合并）"""
    try:
        from scripts.kb_consolidator import KBConsolidator
        consolidator = KBConsolidator()
        report = consolidator.generate_health_report()
        consolidator.close()
        return {"markdown": report}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成健康报告失败: {e}")


# --- Board Endpoints (Chroma优化版) ---

@app.get("/board.html")
def read_board_page():
    return frontend_html_response("board.html")

@app.get("/api/board/issues")
async def get_board_issues(request: Request):
    """Get issues for the board (Chroma优化版)"""
    import asyncio
    jira_client = build_request_jira_client(request)
    if jira_client is None:
        return {}
    return await asyncio.to_thread(board_service.get_board_data, jira_client=jira_client)

@app.get("/api/board")
async def get_board_data(
    request: Request,
    project_key: str = Query("MYPROJECT", description="项目Key"),
    assignee: str = Query("currentUser()", description="经办人"),
    force: bool = Query(False, description="强制跳过缓存直接从Jira获取最新数据"),
    created_start: str = Query("", description="创建时间开始 (YYYY-MM-DD)"),
    created_end: str = Query("", description="创建时间结束 (YYYY-MM-DD)"),
    labels: str = Query("", description="标签"),
    dev_issue_type: str = Query("", description="研发确认问题类型 (customfield_10729)"),
    customer_issue_type: str = Query("", description="客户问题类型 (customfield_10402)"),
    resolution_method: str = Query("", description="解决方式 (customfield_10906)"),
    ignore_module: bool = Query(False, description="忽略用户模块过滤，查看全部"),
    domain_modules: Optional[str] = Query(None, description="逗号分隔领域模块；提供时覆盖用户绑定模块；空字符串=全部")
):
    """
    获取看板数据（Chroma优化版，支持可配置查询条件）

    返回按到期日分组的工单列表，每个工单包含AI分析结果（如已缓存）

    筛选条件:
        - project_key: 项目Key，默认 MYPROJECT
        - assignee: 经办人，默认 currentUser()，传 "ALL" 查询全部
        - created_start: 创建时间开始 (YYYY-MM-DD)
        - created_end: 创建时间结束 (YYYY-MM-DD)
        - labels: 标签
        - dev_issue_type: 研发确认问题类型 (customfield_10729)
        - customer_issue_type: 客户问题类型 (customfield_10402)
        - resolution_method: 解决方式 (customfield_10906)

    说明:
        - 默认总是优先从Jira获取最新数据
        - 仅当Jira API失败时，才会回退到本地缓存
        - force参数已弃用，保留仅用于API兼容性
    """
    try:
        jira_client = build_request_jira_client(request)
        if jira_client is None and not _IS_DEMO:
            return {}
        if domain_modules is not None:
            _domain_modules = [m.strip() for m in domain_modules.split(",") if m.strip()]
        elif ignore_module:
            _domain_modules = []
        else:
            _domain_modules = getattr(request.state, "current_modules", [])
        import asyncio
        data = await asyncio.to_thread(
            board_service.get_board_data,
            project_key=project_key,
            assignee=assignee,
            created_start=created_start,
            created_end=created_end,
            labels=labels,
            dev_issue_type=dev_issue_type,
            customer_issue_type=customer_issue_type,
            resolution_method=resolution_method,
            jira_client=jira_client,
            force=force,
            domain_modules=_domain_modules,
        )
        fetch_meta = board_service.get_last_board_fetch_meta()
        # 将 analysis_cache 中的 grounded_confidence_score 注入每个工单的 ai_analysis
        try:
            _ac_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache", "analysis_cache.json")
            _ac = {}
            if os.path.exists(_ac_path):
                _raw = open(_ac_path, encoding="utf-8").read().strip()
                if _raw:
                    _ac = json.loads(_raw)
            if _ac and isinstance(data, dict):
                for col_issues in data.values():
                    if not isinstance(col_issues, list):
                        continue
                    for iss in col_issues:
                        key = iss.get("key")
                        if not key or key not in _ac:
                            continue
                        gc_score = _ac[key].get("grounded_confidence_score")
                        if gc_score is None:
                            continue
                        if iss.get("ai_analysis") is None:
                            iss["ai_analysis"] = {}
                        iss["ai_analysis"]["grounded_confidence_score"] = gc_score
        except Exception:
            pass
        try:
            from services.gate_decision_log import get_gate_summary as _get_gs
            if isinstance(data, dict):
                for col_issues in data.values():
                    if not isinstance(col_issues, list):
                        continue
                    for iss in col_issues:
                        _iss_key = iss.get("key") or iss.get("issue_key", "")
                        if not _iss_key:
                            continue
                        _gs = _get_gs(_iss_key)
                        if _gs:
                            iss["gate_summary"] = _gs
        except Exception:
            pass
        return {
            "status": "success",
            "data": data,
            "stats": board_service.get_stats(),
            "data_source": fetch_meta.get("data_source"),
            "cache_timestamp": fetch_meta.get("cache_timestamp"),
            "jira_error": fetch_meta.get("jira_error"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/board/analysis-status")
def get_analysis_status(request: AnalysisStatusRequest):
    """
    批量查询工单的AI分析状态（用于前端轮询）
    
    请求：{"issue_keys": ["MYPROJECT-12345", "MYPROJECT-12346"]}
    
    响应：
    {
        "MYPROJECT-12345": {"status": "completed", "analysis": {...}},
        "MYPROJECT-12346": {"status": "analyzing"}
    }
    """
    try:
        updates = board_service.get_analysis_updates(request.issue_keys)
        return {"status": "success", "updates": updates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/board/{issue_key}/reanalyze")
def force_reanalyze(issue_key: str, request: Request):
    """强制重新分析指定工单"""
    try:
        jira_client = build_request_jira_client(request)
        result = board_service.force_analyze(issue_key, jira_client=jira_client)
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class BatchReanalyzeRequest(BaseModel):
    issue_keys: List[str]

@app.post("/api/board/batch-reanalyze")
def batch_reanalyze_issues(request: BatchReanalyzeRequest):
    """
    批量重新分析多个工单

    请求: {"issue_keys": ["MYPROJECT-12345", "MYPROJECT-12346"]}
    响应: {"status": "success", "data": {"submitted": 2, "queue_size": 5}}
    """
    try:
        result = board_service.batch_reanalyze(request.issue_keys)
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- JQL 直接查询端点 ---

# --- 看板拖拽移动端点 ---

class MoveIssueRequest(BaseModel):
    issue_key: str
    target_board: str
    source_board: Optional[str] = None
    sync_jira: bool = True

class BatchMoveRequest(BaseModel):
    moves: List[Dict[str, Any]]
    sync_jira: bool = True


@app.post("/api/board/move-issue")
def move_issue_to_board(request: MoveIssueRequest):
    """
    移动工单到指定看板

    请求: {
        "issue_key": "MYPROJECT-12345",
        "target_board": "done",
        "source_board": "in_progress",
        "sync_jira": true
    }
    响应: {
        "status": "success",
        "data": {
            "success": true,
            "issue_key": "MYPROJECT-12345",
            "target_board": "done",
            "synced_to_jira": true,
            "new_status": "已完成"
        }
    }
    """
    try:
        result = board_service.move_issue_to_board(
            issue_key=request.issue_key,
            target_board=request.target_board,
            source_board=request.source_board,
            sync_jira=request.sync_jira
        )
        if result['success']:
            return {"status": "success", "data": result}
        else:
            raise HTTPException(status_code=400, detail=result.get('error', '移动失败'))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/board/batch-move")
def batch_move_issues(request: BatchMoveRequest):
    """
    批量移动工单

    请求: {
        "moves": [
            {"issue_key": "MYPROJECT-12345", "target_board": "done"},
            {"issue_key": "MYPROJECT-12346", "target_board": "done"}
        ],
        "sync_jira": true
    }
    响应: {
        "status": "success",
        "data": {
            "completed": 2,
            "failed": 0,
            "results": [...]
        }
    }
    """
    try:
        result = board_service.batch_move_issues(
            moves=request.moves,
            sync_jira=request.sync_jira
        )
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/board/move-history")
def get_move_history(
    issue_key: Optional[str] = Query(None, description="工单编号"),
    limit: int = Query(10, description="返回数量")
):
    """
    获取工单移动历史

    响应: {
        "status": "success",
        "data": [
            {
                "id": "MYPROJECT-12345_1704067200",
                "issue_key": "MYPROJECT-12345",
                "source_board": "todo",
                "target_board": "done",
                "timestamp": "2024-01-01T00:00:00"
            }
        ]
    }
    """
    try:
        history = board_service.get_move_history(issue_key=issue_key, limit=limit)
        return {"status": "success", "data": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 自动化任务规则 API ──────────────────────────────────────────────

@app.get("/api/board/automation/rules")
def get_automation_rules():
    return {"rules": board_service.get_automation_rules(), "count": len(board_service.get_automation_rules())}

@app.post("/api/board/automation/rules")
def add_automation_rule(request: dict):
    rule = board_service.add_automation_rule(request)
    return {"success": True, "rule": rule}

@app.put("/api/board/automation/rules/{rule_id}")
def update_automation_rule(rule_id: str, request: dict):
    rule = board_service.update_automation_rule(rule_id, request)
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"success": True, "rule": rule}

@app.delete("/api/board/automation/rules/{rule_id}")
def delete_automation_rule(rule_id: str):
    if not board_service.delete_automation_rule(rule_id):
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"success": True}

@app.post("/api/board/automation/rules/{rule_id}/toggle")
def toggle_automation_rule(rule_id: str):
    result = board_service.toggle_automation_rule(rule_id)
    if result is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"success": True, **result}

@app.post("/api/board/automation/rules/{rule_id}/run")
def run_automation_rule(rule_id: str, dry_run: bool = Query(True, description="试运行模式")):
    return board_service.run_automation_rule(rule_id, dry_run=dry_run)


@app.get("/api/board/search")
def search_similar_issues(
    background_tasks: BackgroundTasks,
    q: str = Query(..., description="搜索查询"),
    top_k: int = Query(5, description="返回数量"),
    min_score: float = Query(0.6, description="最小相似度")
):
    """
    语义搜索相似工单（Chroma优化版）

    相比原有/search端点，使用语义向量搜索，结果更准确
    """
    try:
        results = search_engine.search(q, top_k=top_k, min_score=min_score)
        # 后台入队：对搜索结果工单触发 AI 分析（不阻塞响应）
        if results:
            background_tasks.add_task(board_service._auto_submit_analysis, list(results))
        return {
            "status": "success",
            "query": q,
            "results": results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/board/stats")
def get_board_stats():
    """获取看板统计信息"""
    try:
        return {
            "status": "success",
            "stats": board_service.get_stats()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/board/diagnose")
def diagnose_board_datasources(request: Request):
    """独立探测三个数据源，返回详细诊断信息。"""
    user = require_authenticated_user(request)
    jira_client = build_request_jira_client(request, require_binding=False)
    from datetime import datetime
    proxy_nodes = jira_cache_service.config.get("proxy_nodes", []) if jira_cache_service else []
    proxy_base_url = proxy_nodes[0].get("base_url") if proxy_nodes else DEFAULT_PROXY_BASE_URL

    result = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "fetch_strategy": BOARD_FETCH_STRATEGY,
        "fetch_order": BOARD_FETCH_ORDER,
        "client_network_bypass_enabled": os.environ.get("CLIENT_NETWORK_BYPASS_ENABLED", "false").lower() == "true",
        "frp_expected_ports": FRP_EXPECTED_PORTS,
        "env": {
            "ENABLE_CACHE_SERVICE": ENABLE_CACHE_SERVICE,
            "JIRA_SSL_VERIFY": os.environ.get("JIRA_SSL_VERIFY", "true"),
            "JIRA_SKIP_COOKIES": os.environ.get("JIRA_SKIP_COOKIES", "false"),
            "BOARD_FETCH_PREFER_PROXY": os.environ.get("BOARD_FETCH_PREFER_PROXY", "false"),
            "BOARD_JIRA_DIRECT_COOLDOWN_SECONDS": os.environ.get("BOARD_JIRA_DIRECT_COOLDOWN_SECONDS", "120"),
            "PROXY_NODES": proxy_nodes,
        },
    }

    if hasattr(board_service, "get_fetch_strategy_state"):
        result["board_fetch_state"] = board_service.get_fetch_strategy_state()

    # 1. Jira 直连诊断
    if jira_client:
        result["jira_direct"] = jira_client.diagnose_connection()
    else:
        result["jira_direct"] = {"status": "missing_binding", "user": user["username"]}

    # 2. Mini 代理状态
    if jira_cache_service:
        try:
            metrics = jira_cache_service.get_metrics()
            result["mini_proxy"] = {
                "enabled": True,
                "status": "ok",
                "expected_base_url": proxy_base_url,
                "metrics": metrics,
            }
        except Exception as e:
            result["mini_proxy"] = {
                "enabled": True,
                "status": "error",
                "expected_base_url": proxy_base_url,
                "error": str(e)[:200],
            }
    else:
        result["mini_proxy"] = {
            "enabled": False,
            "status": "disabled",
            "expected_base_url": proxy_base_url,
            "error": "ENABLE_CACHE_SERVICE is false",
        }

    # 3. 本地缓存状态
    cache_info = jira_client.get_cache_info() if jira_client else {"exists": False}
    if cache_info.get("exists"):
        ts = cache_info.get("timestamp", "")
        age_hours = None
        if ts:
            try:
                cache_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                age_hours = round((datetime.now() - cache_dt).total_seconds() / 3600, 1)
            except ValueError:
                pass
        result["local_cache"] = {
            "exists": True,
            "count": cache_info.get("count", 0),
            "timestamp": ts,
            "age_hours": age_hours,
        }
    else:
        result["local_cache"] = {"exists": False}

    return result


@app.get("/api/board/meta")
def get_board_meta(request: Request):
    """看板元数据：项目列表 + 当前Jira用户（无需登录）"""
    jira_client = build_request_jira_client(request, require_binding=False)
    projects, current_user = [], {}
    try:
        raw = jira_client.get_projects()
        projects = sorted(
            [{"key": p["key"], "name": p.get("name", p["key"])} for p in raw],
            key=lambda p: p["key"]
        )
    except Exception as e:
        print(f"[board/meta] 获取项目失败: {e}")
    try:
        current_user = jira_client.get_myself()
    except Exception as e:
        print(f"[board/meta] 获取当前用户失败: {e}")
    # Jira 不可达时从缓存数据回退默认项目列表和用户
    if not projects:
        fallback_projects = [
            {"key": "MYPROJECT", "name": "云平台-流程中心"},
            {"key": "LYJM", "name": "云平台-流程引擎"},
            {"key": "YYZJ", "name": "应用支撑-应用构建"},
        ]
        cache_path = os.path.join(BASE_DIR, "data_cache", "jira_board_data.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    cached = json.load(f)
                cached_keys = set()
                for col_items in cached.values():
                    if isinstance(col_items, list):
                        for item in col_items:
                            if isinstance(item, dict) and item.get("key"):
                                cached_keys.add(item["key"].split("-")[0])
                if cached_keys:
                    fallback_projects = sorted(
                        [{"key": k, "name": k} for k in cached_keys],
                        key=lambda p: p["key"]
                    )
            except Exception:
                pass
        projects = fallback_projects
        print(f"[board/meta] Jira不可达，使用回退项目列表: {[p['key'] for p in projects]}")
    if not current_user and not is_strict_role():
        current_user = {"name": "admin", "displayName": "管理员"}
    return {"projects": projects, "current_user": current_user}


@app.get("/api/board/assignees")
def get_board_assignees(request: Request, project_key: str = Query("MYPROJECT")):
    """指定项目的可分配用户列表"""
    jira_client = build_request_jira_client(request, require_binding=False)
    try:
        users = jira_client.get_assignable_users(project_key)
        return {
            "results": [
                {"username": u["name"], "displayName": u.get("displayName", u["name"])}
                for u in users if u.get("active", True)
            ]
        }
    except Exception as e:
        print(f"[board/assignees] 失败: {e}")
        return {"results": []}


# --- 工单详情 / Jira移动 / 附件代理 端点 ---

@app.get("/api/board/issue-detail/{issue_key}")
def get_issue_detail(issue_key: str, request: Request, background_tasks: BackgroundTasks):
    """获取工单详情: 附件 + 活动日志(changelog + comments)

    会话隔离：必须使用 build_request_jira_client(request) 得到当前用户的
    JiraService（base_url = mini_proxy on QCL, 带用户自己的 JSESSIONID）。
    不再走老的 /proxy/jira/issue/{key}（那条路径 mini_proxy 用的是自己的
    session，会让所有用户看到 admin 的视角）。
    透明代理 /rest/* 会读 request.cookies 转发用户 session 到真实 Jira。
    """
    try:
        jira_client = build_request_jira_client(request)
        url = f"{jira_client.base_url}/rest/api/2/issue/{issue_key}?expand=changelog&fields=attachment,comment,labels"
        response = requests.get(
            url,
            headers=jira_client.headers,
            cookies=getattr(jira_client, 'cookies', {}),
            verify=jira_client.ssl_verify,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        # 附件
        attachments = []
        for att in data.get("fields", {}).get("attachment", []):
            attachments.append({
                "id": att.get("id"),
                "filename": att.get("filename"),
                "size": att.get("size"),
                "mimeType": att.get("mimeType", ""),
                "content": att.get("content", ""),  # 下载URL
                "thumbnail": att.get("thumbnail", ""),
                "created": att.get("created", "")[:16],
                "author": att.get("author", {}).get("displayName", ""),
            })

        # 评论
        comments = []
        for c in data.get("fields", {}).get("comment", {}).get("comments", []):
            comments.append({
                "author": c.get("author", {}).get("displayName", ""),
                "body": c.get("body", "")[:2000],
                "created": c.get("created", "")[:16],
            })

        # 变更历史
        changelog = []
        for history in data.get("changelog", {}).get("histories", [])[-20:]:
            for item in history.get("items", []):
                changelog.append({
                    "author": history.get("author", {}).get("displayName", ""),
                    "created": history.get("created", "")[:16],
                    "field": item.get("field", ""),
                    "from": item.get("fromString", ""),
                    "to": item.get("toString", ""),
                })

        labels = data.get("fields", {}).get("labels", []) or []
        # 后台入队：对当前工单触发 AI 分析（不阻塞响应）
        _fields = data.get("fields", {})
        _ticket_dict = {
            "key": issue_key,
            "summary": (_fields.get("summary") or "")[:200],
            "description": (_fields.get("description") or "")[:2000],
            "status": (_fields.get("status") or {}).get("name", ""),
            "priority": (_fields.get("priority") or {}).get("name", ""),
            "assignee": ((_fields.get("assignee") or {}).get("displayName") or ""),
            "reporter": ((_fields.get("reporter") or {}).get("displayName") or ""),
        }
        background_tasks.add_task(board_service._auto_submit_analysis, [_ticket_dict])
        detail_response = {
            "status": "success",
            "attachments": attachments,
            "comments": comments[-10:],  # 最新10条
            "changelog": changelog[-15:],  # 最新15条
            "labels": labels,
        }
        try:
            from services.gate_decision_log import get_recent_decision as _get_rd, get_gate_summary as _get_gs
            _gate_log = _get_rd(issue_key)
            if _gate_log:
                detail_response["reply_gateway"] = _gate_log.get("reply_gateway", {})
                detail_response["gate_summary"] = _get_gs(issue_key)
        except Exception:
            pass
        return detail_response
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e), "attachments": [], "comments": [], "changelog": [], "labels": []}


class IssueLabelUpdateRequest(BaseModel):
    add: list[str] = []
    remove: list[str] = []


@app.post("/api/board/issue/{issue_key}/labels")
def update_issue_labels_route(issue_key: str, payload: IssueLabelUpdateRequest, request: Request):
    """增量更新工单标签（add / remove）."""
    jira_client = build_request_jira_client(request)
    result = jira_client.update_issue_labels(issue_key, add=payload.add, remove=payload.remove)
    if not result.get('success'):
        raise HTTPException(status_code=400, detail=result.get('message', '更新失败'))
    try:
        url = f"{jira_client.base_url}/rest/api/2/issue/{issue_key}?fields=labels"
        r = requests.get(url, headers=jira_client.headers,
                         cookies=getattr(jira_client, 'cookies', {}),
                         verify=jira_client.ssl_verify, timeout=10)
        labels = r.json().get('fields', {}).get('labels', []) if r.ok else []
    except Exception:
        labels = []
    return {"status": "success", "labels": labels}


@app.get("/api/board/labels/suggest")
def suggest_labels_route(request: Request, query: str = ""):
    """代理 Jira 的标签联想接口."""
    jira_client = build_request_jira_client(request)
    url = f"{jira_client.base_url}/rest/api/1.0/labels/suggest?query={requests.utils.quote(query or '')}"
    try:
        r = requests.get(url, headers=jira_client.headers,
                         cookies=getattr(jira_client, 'cookies', {}),
                         verify=jira_client.ssl_verify, timeout=8)
        if not r.ok:
            return {"suggestions": []}
        data = r.json()
        return {"suggestions": [{"label": s.get("label", "")} for s in data.get("suggestions", [])]}
    except Exception:
        return {"suggestions": []}


class MoveIssueJiraRequest(BaseModel):
    issue_id: str = ""  # Jira内部ID (如 8810864), 可选，会从issue_key自动获取
    issue_key: str = ""  # 工单key (如 MYPROJECT-61182)
    target_project_id: str
    target_project_key: str = ""
    issuetype_id: str = "10400"
    field_values: Dict[str, str] = {}
    comment: Optional[str] = None


@app.post("/api/board/move-issue-jira")
def move_issue_jira(request: MoveIssueJiraRequest, raw_request: Request):
    """通过Jira Web界面移动工单到另一个项目"""
    if _IS_DEMO:
        return {"status": "demo_blocked", "message": "演示模式：Jira 移动操作已屏蔽"}
    try:
        jira_client = build_request_jira_client(raw_request)

        # 如果没有issue_id，从issue_key获取
        issue_id = request.issue_id
        if not issue_id or issue_id == "0":
            issue_url = f"{jira_client.base_url}/rest/api/2/issue/{request.issue_key}?fields=project"
            id_resp = requests.get(issue_url, headers=jira_client.headers,
                                   cookies=getattr(jira_client, 'cookies', {}),
                                   verify=jira_client.ssl_verify, timeout=10)
            if id_resp.ok:
                issue_id = id_resp.json().get("id", "")
            if not issue_id:
                return {"status": "error", "message": f"无法获取 {request.issue_key} 的内部ID"}

        result = jira_client.move_issue(
            issue_id=issue_id,
            target_project_id=request.target_project_id,
            issuetype_id=request.issuetype_id,
            field_values=request.field_values,
        )
        if result.get("success"):
            try:
                from services.operation_event_log import log_event as _log_event
                _move_issue_info = board_service._get_issue_from_cache(request.issue_key or request.issue_id) or {}
                _log_event(
                    "move_jira", request.issue_key or request.issue_id,
                    raw_request.headers.get("X-User-Name", "unknown"),
                    to_project_key=request.target_project_key or request.target_project_id,
                    module=(request.field_values or {}).get("customfield_10123"),
                    comment=request.comment,
                    source="ui_modal",
                    summary=_move_issue_info.get("summary", ""),
                    customer=_move_issue_info.get("customer_name", ""),
                    product_version=_move_issue_info.get("product_version", ""),
                )
            except Exception:
                pass
            return {"status": "success", "message": result["message"]}
        else:
            return {"status": "error", "message": result["message"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class JiraSessionRequest(BaseModel):
    jsessionid: str
    xsrf_token: str = ""

@app.post("/api/setup/jira-session")
def setup_jira_session(request: JiraSessionRequest):
    """保存用户提供的Jira浏览器会话cookies（用于移动工单等需要Web UI的操作）"""
    import json as _json
    cookies_list = [
        {"name": "JSESSIONID", "value": request.jsessionid.strip(), "domain": "jira.example.com", "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "None", "expires": -1},
    ]
    if request.xsrf_token:
        cookies_list.append({"name": "atlassian.xsrf.token", "value": request.xsrf_token.strip(),
                              "domain": "jira.example.com", "path": "/",
                              "httpOnly": False, "secure": False, "sameSite": "Lax", "expires": -1})
    state = {"cookies": cookies_list, "origins": []}
    from services.host_context import session_path as _session_path
    state_path = _session_path()
    with open(state_path, "w", encoding="utf-8") as f:
        _json.dump(state, f)
    return {"status": "success", "message": f"已保存 {len(cookies_list)} 个cookies"}


@app.post("/api/setup/jira-session-auto")
def setup_jira_session_auto():
    """优先让 mini(lap) 做 Chrome 解密（拿真实浏览器 JSESSIONID），
    失败才降级 REST-login（仅 QCL 等无 Chrome 环境）。"""
    import json as _json
    import requests as _req
    from services.host_context import is_mini as _is_mini

    # ── 优先路径：级联到 mini proxy，由 lap 本机做 Chrome 解密 ──────────────
    mini_port = FRP_EXPECTED_PORTS.get("mini_proxy_port")
    if mini_port:
        try:
            r = _req.post(
                f"http://127.0.0.1:{mini_port}/proxy/jira/session/refresh",
                timeout=(2, 4),
            )
            if r.status_code == 200:
                info = r.json()
                if info.get("status") == "success":
                    # 同步将 lap 的 Chrome cookies 镜像到本机 /tmp/jira-session.json
                    # （Playwright 移动工单在本机执行，读本机 session 文件）
                    try:
                        ck_r = _req.get(
                            f"http://127.0.0.1:{mini_port}/proxy/jira/session/cookies",
                            timeout=5,
                        )
                        if ck_r.status_code == 200:
                            local_state = ck_r.json().get("state", {})
                            if local_state.get("cookies"):
                                from services.host_context import session_path as _session_path
                                _local_path = _session_path()
                                with open(_local_path, "w", encoding="utf-8") as _f:
                                    _json.dump(local_state, _f)
                                print(f"[refresh] 已将 lap Chrome cookies 镜像到本机 {_local_path}")
                    except Exception as _me:
                        print(f"[refresh] 镜像本机 session 失败（非致命）: {_me}")
                    return {
                        "status": "success",
                        "source": "chrome_decrypt_via_mini",
                        "message": f"已通过 lap Chrome 解密刷新（用户: {info.get('user', '?')}）",
                        "output": f"✓ cookies 数量: {info.get('cookies_count', '?')}",
                    }
            err_hint = r.text[:200]
            print(f"[refresh] mini Chrome 解密失败 HTTP {r.status_code}: {err_hint}")
            if _is_mini():
                raise HTTPException(status_code=500,
                    detail=f"Chrome 解密失败（HTTP {r.status_code}）：{err_hint}。请确认 lap Chrome 已登录 Jira，或手动粘贴 JSESSIONID。")
        except HTTPException:
            raise
        except Exception as e:
            print(f"[refresh] mini 刷新异常: {e}")
            if _is_mini():
                raise HTTPException(status_code=500,
                    detail=f"Chrome 解密服务无响应：{e}。请手动粘贴 JSESSIONID。")

    # ── 降级路径：REST-login（仅 QCL 等无 Chrome 环境）────────────────────────
    cfg = jira_svc.config_parser
    username = cfg.username
    password = cfg.password
    if not username or not password:
        raise HTTPException(status_code=500, detail="jira_api.md 中未配置 username/password，且 mini Chrome 解密也失败")

    base_url = jira_svc.base_url.rstrip("/")
    login_url = f"{base_url}/rest/auth/1/session"
    try:
        resp = _req.post(
            login_url,
            json={"username": username, "password": password},
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "curl/8.7.1"},
            verify=jira_svc.ssl_verify,
            timeout=10,
            proxies=jira_svc.proxies,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"登录请求失败: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Jira登录失败 HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    session = data.get("session", {})
    jsessionid = session.get("value", "")
    if not jsessionid:
        raise HTTPException(status_code=500, detail=f"响应中无JSESSIONID: {resp.text[:200]}")

    cookies_list = [
        {"name": "JSESSIONID", "value": jsessionid, "domain": "jira.example.com", "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "None", "expires": -1},
    ]
    state = {"cookies": cookies_list, "origins": []}
    from services.host_context import session_path as _session_path
    state_path = _session_path()
    with open(state_path, "w", encoding="utf-8") as f:
        _json.dump(state, f)

    login_info = data.get("loginInfo", {})
    return {
        "status": "success",
        "source": "rest_login_fallback",
        "message": f"已通过 REST-login 获取 JSESSIONID（降级模式，若附件仍失败请确保 lap Chrome 保持 Jira 登录状态）",
        "output": f"✓ JSESSIONID (len={len(jsessionid)})\n✓ 登录用户: {username}",
    }


@app.get("/api/board/move-targets/{issue_id}")
def get_move_targets(issue_id: str, raw_request: Request):
    """获取移动工单的目标项目列表"""
    try:
        jira_client = build_request_jira_client(raw_request)
        result = jira_client.get_move_targets(issue_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/board/move-field-options/{project_key}")
def get_move_field_options(project_key: str, raw_request: Request):
    """获取目标项目的领域模块(customfield_10123)选项，用于移动工单"""
    try:
        jira_client = build_request_jira_client(raw_request)
        url = f"{jira_client.base_url}/rest/api/2/issue/createmeta?projectKeys={project_key}&issuetypeIds=10400&expand=projects.issuetypes.fields"
        resp = requests.get(url, headers=jira_client.headers,
                           cookies=getattr(jira_client, 'cookies', {}),
                           verify=jira_client.ssl_verify, timeout=10,
                           proxies=getattr(jira_client, 'proxies', None))
        if not resp.ok:
            return {"success": False, "options": [], "message": f"HTTP {resp.status_code}"}

        data = resp.json()
        options = []
        for proj in data.get("projects", []):
            for it in proj.get("issuetypes", []):
                field = it.get("fields", {}).get("customfield_10123", {})
                for opt in field.get("allowedValues", []):
                    options.append({"id": str(opt.get("id", "")), "value": opt.get("value", "")})
        return {"success": True, "options": options}
    except Exception as e:
        return {"success": False, "options": [], "message": str(e)}


@app.get("/api/attachment/{attachment_id}")
def proxy_attachment(attachment_id: str, request: Request):
    """代理 Jira 附件下载。

    会话隔离：必须用当前用户的 JiraService（带自己的 JSESSIONID），
    不再走老的 /proxy/jira/attachment/{id} 端点（那条路径用的是 mini_proxy
    自己的 admin session，会暴露错人的附件权限）。
    QCL 上 jira_client.base_url 已自动指向 mini_proxy (frp 5001)，
    transparent_proxy 会透传用户 cookies 到真实 Jira。
    """
    try:
        jira_client = build_request_jira_client(request)

        # 1. 获取附件元数据（含完整content URL）
        meta_url = f"{jira_client.base_url}/rest/api/2/attachment/{attachment_id}"
        meta_resp = requests.get(
            meta_url,
            headers=jira_client.headers,
            cookies=getattr(jira_client, 'cookies', {}),
            verify=jira_client.ssl_verify,
            timeout=10,
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        content_url = meta.get("content", "")
        mime_type = meta.get("mimeType", "application/octet-stream")
        filename = meta.get("filename", f"attachment_{attachment_id}")

        if not content_url:
            raise HTTPException(status_code=404, detail="附件URL为空")

        # 2. 下载附件内容 (附件URL需要Web session, Basic Auth不够)
        # 优先使用 jira_client.cookies (已是当前用户的 session)
        import json as _json
        download_cookies = dict(getattr(jira_client, 'cookies', {}) or {})
        # per-user session 文件 → 全局兜底（避免让 user2 看到 admin 附件）
        from services.host_context import session_path as _session_path
        cur_user = get_current_user(request)
        session_candidates = []
        if cur_user:
            session_candidates.append(_session_path(user=cur_user['username'], prefix="jira"))
        session_candidates.append(_session_path())  # 全局兜底
        for state_path in session_candidates:
            try:
                if os.path.exists(state_path):
                    with open(state_path) as f:
                        state = _json.load(f)
                    _dl_host = __import__('urllib.parse', fromlist=['urlparse']).urlparse(
                        os.environ.get("JIRA_BASE_URL", "")).hostname or ""
                    for c in state.get("cookies", []):
                        if _dl_host and _dl_host in c.get("domain", ""):
                            download_cookies.setdefault(c["name"], c["value"])
                    break  # 用第一个存在的
            except Exception:
                continue

        resp = requests.get(
            content_url,
            headers=jira_client.headers,
            cookies=download_cookies,
            verify=jira_client.ssl_verify,
            stream=True,
            timeout=30,
        )
        resp.raise_for_status()

        # 根据文件扩展名修正mime (Jira经常返回错误的multipart/form-data)
        import mimetypes
        guessed_mime = mimetypes.guess_type(filename)[0]
        actual_mime = guessed_mime or mime_type or 'application/octet-stream'
        # 常见修正
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        mime_overrides = {
            'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'gif': 'image/gif', 'svg': 'image/svg+xml', 'webp': 'image/webp',
            'pdf': 'application/pdf', 'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'txt': 'text/plain', 'csv': 'text/csv', 'json': 'application/json',
        }
        if ext in mime_overrides:
            actual_mime = mime_overrides[ext]

        # 检测是否返回了 HTML 认证页（Jira 过期时 200+HTML 伪装成附件）
        resp_ct = resp.headers.get("content-type", "")
        if "text/html" in resp_ct and ext not in ("html", "htm"):
            raise HTTPException(
                status_code=401,
                detail="Jira session已失效，附件下载被重定向到认证页，请点击「刷新授权」按钮重试"
            )

        from urllib.parse import quote
        from fastapi.responses import StreamingResponse
        encoded_filename = quote(filename)
        return StreamingResponse(
            resp.iter_content(chunk_size=8192),
            media_type=actual_mime,
            headers={
                'Content-Disposition': f"inline; filename*=UTF-8''{encoded_filename}",
                'Cache-Control': 'private, max-age=3600',
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"附件获取失败: {str(e)}")


from enum import Enum

class JiraAction(str, Enum):
    """Jira操作类型枚举"""
    ASSIGN = "assign"
    REPLY = "reply"
    REPLY_AND_CLOSE = "reply_and_close"

class JiraActionRequest(BaseModel):
    issue_id: str = Field(
        ...,
        min_length=3,
        max_length=50,
        description="Jira工单编号，格式如：MYPROJECT-12345"
    )
    action: JiraAction = Field(
        ...,
        description="操作类型：assign(分配), reply(回复), reply_and_close(回复并关闭)"
    )
    value: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="操作值：分配时的用户名、回复时的评论内容等"
    )
    custom_fields: Optional[Dict[str, str]] = Field(
        None,
        description="自定义字段（如解决方案、回复方式等）"
    )
    extra: Optional[Dict[str, str]] = Field(
        None,
        description="额外参数（如comment用于分配时的评论）"
    )
    ai_fields: Optional[Dict[str, str]] = Field(
        None,
        description="AI相关字段（smart_result, ai_result, use_agent）"
    )

    @validator('issue_id')
    def validate_issue_id(cls, v):
        """验证工单ID格式"""
        import re
        if not re.match(r'^[A-Z][A-Z0-9]*-\d+$', v):
            raise ValueError('工单ID格式无效，应为：项目前缀-数字（如：MYPROJECT-12345）')
        return v

    @validator('value')
    def validate_value(cls, v, values):
        """根据操作类型验证value"""
        action = values.get('action')
        if action == JiraAction.ASSIGN and not v.strip():
            raise ValueError('分配操作时必须提供用户名')
        if action in (JiraAction.REPLY, JiraAction.REPLY_AND_CLOSE) and len(v.strip()) < 1:
            raise ValueError('回复操作时必须提供评论内容')
        return v

# 错误码定义
class ErrorCode:
    """API错误码定义"""
    SUCCESS = "0"
    INVALID_REQUEST = "1001"
    AUTHENTICATION_FAILED = "1002"
    PERMISSION_DENIED = "1003"
    RESOURCE_NOT_FOUND = "1004"
    JIRA_API_ERROR = "2001"
    JIRA_CONNECTION_ERROR = "2002"
    OPERATION_FAILED = "3001"
    PARTIAL_SUCCESS = "3002"
    INTERNAL_ERROR = "5001"

# 错误码对应的消息（面向用户）
ERROR_MESSAGES = {
    ErrorCode.SUCCESS: "操作成功",
    ErrorCode.INVALID_REQUEST: "请求参数无效",
    ErrorCode.AUTHENTICATION_FAILED: "认证失败",
    ErrorCode.PERMISSION_DENIED: "权限不足",
    ErrorCode.RESOURCE_NOT_FOUND: "资源不存在",
    ErrorCode.JIRA_API_ERROR: "Jira API返回错误",
    ErrorCode.JIRA_CONNECTION_ERROR: "无法连接到Jira服务器",
    ErrorCode.OPERATION_FAILED: "操作执行失败",
    ErrorCode.PARTIAL_SUCCESS: "操作部分成功",
    ErrorCode.INTERNAL_ERROR: "服务器内部错误"
}

@app.post("/api/jira/attachment")
async def upload_jira_attachment(issue_key: str = Form(...), file: UploadFile = File(...), raw_request: Request = None):
    """上传附件到Jira工单"""
    jira_client = build_request_jira_client(raw_request)

    MAX_SIZE = 20 * 1024 * 1024  # 20MB
    content = await file.read()
    if len(content) > MAX_SIZE:
        return {"status": "error", "message": f"文件过大({len(content)//1024//1024}MB)，最大20MB"}

    result = jira_client.upload_attachment(issue_key, file.filename, content)
    return {
        "status": "success" if result["success"] else "error",
        "message": result["message"],
        "attachment": result.get("attachment")
    }


@app.post("/api/jira/action")
def jira_action(request: JiraActionRequest, raw_request: Request):
    """
    对Jira工单执行操作

    支持的操作：
    - assign: 分配工单
    - reply: 回复工单
    - reply_and_close: 回复并关闭工单
    """
    if _IS_DEMO:
        return {"status": "demo_blocked", "message": "演示模式：Jira 写操作已屏蔽，不会写入真实工单"}
    import traceback

    try:
        jira_client = build_request_jira_client(raw_request)
        result = None

        if request.action == JiraAction.ASSIGN:
            # 支持分配时添加评论
            comment = request.extra.get('comment') if request.extra else None
            result = jira_client.assign_issue(request.issue_id, request.value, comment=comment)
        elif request.action == JiraAction.REPLY:
            # 传递自定义字段到reply_issue
            close = request.extra.get('close', 'false').lower() == 'true' if request.extra else False
            result = jira_client.reply_issue(
                request.issue_id,
                request.value,
                custom_fields=request.custom_fields,
                close=close
            )
        elif request.action == JiraAction.REPLY_AND_CLOSE:
            # 回复并关闭（通过"直接回复"工作流转换，原子操作）
            result = jira_client.reply_issue(
                request.issue_id,
                request.value,
                custom_fields=request.custom_fields,
                close=True,
                ai_fields=request.ai_fields
            )
        else:
            return {
                "status": "error",
                "code": ErrorCode.INVALID_REQUEST,
                "message": f"不支持的操作类型: {request.action}"
            }

        # 处理结果
        if not result:
            return {
                "status": "error",
                "code": ErrorCode.INTERNAL_ERROR,
                "message": ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]
            }

        if result.get('success'):
            # 反馈采集：回复成功后记录到训练器（reply 和 reply_and_close 均采集）
            if request.action in (JiraAction.REPLY, JiraAction.REPLY_AND_CLOSE) and request.extra:
                try:
                    ai_original = request.extra.get('ai_original', '')
                    user_final = request.value
                    if ai_original:
                        import difflib as _dl
                        _sim = _dl.SequenceMatcher(None, ai_original, user_final).ratio()
                        adopted = _sim >= 0.50
                        adoption_tier = "direct" if _sim >= 0.85 else "partial" if _sim >= 0.50 else "none"
                    else:
                        adopted, adoption_tier = True, "direct"
                    _action_user = get_current_user(raw_request)
                    board_service.reply_trainer.record_feedback(
                        issue_key=request.issue_id,
                        ticket_summary=request.extra.get('ticket_summary', ''),
                        ticket_desc=request.extra.get('ticket_desc', ''),
                        ai_original=ai_original,
                        user_final=user_final,
                        adopted=adopted,
                        adoption_tier=adoption_tier,
                        reply_method=request.custom_fields.get('reply_method', '') if request.custom_fields else '',
                        issue_type=request.custom_fields.get('issue_type_confirmed', '') if request.custom_fields else '',
                        module_l2=request.extra.get('module_l2', '') if request.extra else '',
                        user_id=_action_user["id"] if _action_user else "",
                    )
                except Exception as e:
                    print(f"[JiraAction] 反馈记录失败（不影响主流程）: {e}")

            return {
                "status": "success",
                "code": ErrorCode.SUCCESS,
                "message": result.get('message', '操作成功')
            }
        else:
            # 检查是否部分成功
            if result.get('partial_success'):
                return {
                    "status": "warning",
                    "code": ErrorCode.PARTIAL_SUCCESS,
                    "message": result.get('message', '操作部分成功'),
                    "warning": result.get('warning', '部分操作可能已成功，请检查工单状态'),
                    "completed_steps": result.get('completed_steps', [])
                }

            # 根据错误信息判断错误类型
            error_msg = result.get('message', '操作失败')
            if 'HTTP错误' in error_msg or 'HTTP' in error_msg:
                code = ErrorCode.JIRA_API_ERROR
            elif '超时' in error_msg:
                code = ErrorCode.JIRA_CONNECTION_ERROR
            else:
                code = ErrorCode.OPERATION_FAILED

            return {
                "status": "error",
                "code": code,
                "message": error_msg
            }

    except Exception as e:
        # 记录详细错误信息到服务端日志
        error_trace = traceback.format_exc()
        print(f"[JiraAction] 操作异常: {e}\n{error_trace}")

        # 返回给客户端的是标准化错误，不包含敏感信息
        return {
            "status": "error",
            "code": ErrorCode.INTERNAL_ERROR,
            "message": ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]
        }

# --- Crew (Personnel) Endpoints ---

@app.get("/api/crew/list")
def get_crew_list():
    """获取人员列表（按角色分组）"""
    try:
        return crew_service.get_grouped_personnel()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/crew/search")
def search_crew(q: str = Query(..., description="搜索关键词")):
    """搜索人员（支持中文名、用户名、角色）"""
    try:
        results = crew_service.search(q)
        return {
            "query": q,
            "count": len(results),
            "results": [
                {
                    "username": p.username,
                    "realname": p.realname,
                    "role": p.role,
                    "subrole": p.subrole
                }
                for p in results
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/crew/jira-search")
def search_jira_users(q: str = Query(..., min_length=1), issue_key: str = Query(None), raw_request: Request = None):
    """从Jira用户目录实时搜索（支持中文名/用户名），降级到crewlist。
    传入 issue_key 时使用 /user/assignable/search 只返回该工单可分配的用户，避免搜到分配后报错的用户。"""
    q = q.strip()
    if not q:
        return {"results": [], "source": "empty"}

    # 1. 从 Jira 搜索（使用请求方绑定的凭据，优先使用当前用户Jira账号）
    try:
        jira_client = build_request_jira_client(raw_request, require_binding=False) if raw_request else jira_svc
        if not jira_client:
            jira_client = jira_svc
        if issue_key:
            jira_results = jira_client.search_assignable_users(q, issue_key=issue_key.strip(), max_results=20)
        else:
            jira_results = jira_client.search_users(q, max_results=20)
        if jira_results:
            return {"results": jira_results, "source": "jira"}
    except Exception as e:
        print(f"[crew/jira-search] Jira搜索异常: {e}")

    # 2. 降级到 crewlist（去重：同一 username 只保留一条）
    crew_results = crew_service.search(q)
    seen = set()
    deduped = []
    for p in crew_results:
        key = (p.username or p.realname).lower()
        if key not in seen:
            seen.add(key)
            deduped.append({"username": p.username, "displayName": p.realname, "active": True})
    return {"results": deduped, "source": "crewlist"}


# --- Trainer Sync Endpoints ---

@app.get("/api/trainer/export-recent")
def export_trainer_recent(since_hours: int = 24):
    """
    导出最近 since_hours 小时内的反馈日志，供跨机器增量同步使用。
    Mini 在训练前调用 QCL 的此接口获取 QCL 的最新用户回复样本。
    """
    import time as _time
    from pathlib import Path as _Path
    log_path = _Path(__file__).parent / "data" / "reply_trainer" / "feedback_log.jsonl"
    if not log_path.exists():
        return {"entries": [], "count": 0}
    cutoff = _time.time() - since_hours * 3600
    entries = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # 解析时间戳
                    ts_str = entry.get("ts", "")
                    if ts_str:
                        import datetime as _dt
                        ts = _dt.datetime.fromisoformat(ts_str).timestamp()
                        if ts >= cutoff:
                            entries.append(entry)
                except Exception:
                    continue
    except Exception as e:
        return {"entries": [], "count": 0, "error": str(e)}
    return {"entries": entries, "count": len(entries), "since_hours": since_hours}


@app.get("/api/trainer/daily-report")
def trainer_daily_report():
    """
    生成智能回复训练器的每日进度报告（Markdown）。
    供 OpenClaw cron 调用后 announce 到飞书群。
    如果相比上次运行毫无变化，返回单字符串 "NO_CHANGE"（cron 侧识别为 NO_REPLY）。
    """
    import datetime as _dt
    from pathlib import Path as _Path
    BACKEND = _Path(__file__).parent
    TRAIN_DIR = BACKEND.parent.parent / "conclusion" / "_local" / "training"

    # 1. 读取训练器状态
    try:
        trainer_state = json.loads((TRAIN_DIR / "trainer_state.json").read_text(encoding="utf-8"))
    except Exception:
        trainer_state = {}

    session_count = trainer_state.get("session_count", 0)
    total_questions = trainer_state.get("total_questions", 0)
    b_lessons = trainer_state.get("b_cumulative_lessons", [])
    b_lessons_count = len(b_lessons) if isinstance(b_lessons, list) else 0

    # 2. 读取 Agent A 加工进度
    try:
        a_state = json.loads((TRAIN_DIR / "agent_a_index_state.json").read_text(encoding="utf-8"))
    except Exception:
        a_state = {}
    a_processed = a_state.get("processed", 0)
    a_total = a_state.get("total", 1)
    a_pct = round(a_processed / max(a_total, 1) * 100, 1)
    a_batches = a_state.get("batches_run", 0)

    # 3. 读取训练指标历史（最近 3 次）
    metrics_file = TRAIN_DIR / "training_metrics.jsonl"
    recent_metrics = []
    if metrics_file.exists():
        try:
            lines = metrics_file.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-3:]:
                try:
                    recent_metrics.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass

    # 4. 读取模式库
    try:
        pl = json.loads((TRAIN_DIR / "pattern_library.json").read_text(encoding="utf-8"))
        modes_count = len(pl.get("thinking_modes", []))
        rmp_keys = list(pl.get("reply_method_patterns", {}).keys())
        topic_count = len(pl.get("topic_handling", {}))
    except Exception:
        modes_count, rmp_keys, topic_count = 0, [], 0

    # 5. 风格规则文件大小（inference 实际读取的 _global.md）
    rules_file = BACKEND / "data" / "reply_style_rules" / "_global.md"
    rules_chars = rules_file.stat().st_size if rules_file.exists() else 0

    # 6. 读取昨日快照做对比；如完全相同则返回 NO_CHANGE
    snapshot_file = TRAIN_DIR / "daily_report_snapshot.json"
    current_snapshot = {
        "session_count": session_count,
        "total_questions": total_questions,
        "b_lessons_count": b_lessons_count,
        "a_processed": a_processed,
        "modes_count": modes_count,
        "rules_chars": rules_chars,
    }
    if snapshot_file.exists():
        try:
            prev_snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
            if prev_snapshot == current_snapshot:
                return {"markdown": "NO_CHANGE", "snapshot": current_snapshot}
            # 计算差异
            deltas = {
                k: current_snapshot[k] - prev_snapshot.get(k, 0)
                for k in current_snapshot
            }
        except Exception:
            deltas = {k: 0 for k in current_snapshot}
    else:
        deltas = {k: 0 for k in current_snapshot}

    # 7. 保存快照（下次对比用）
    try:
        snapshot_file.write_text(json.dumps(current_snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    # 8. 生成 Markdown 报告
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    md = [
        f"## 🧠 智能回复训练器日报 — {now}",
        "",
        "### Agent B 学习进度",
        f"- 完成训练会话: **{session_count} 期**（+{deltas['session_count']}）",
        f"- 累计答题: **{total_questions} 题**（+{deltas['total_questions']}）",
        f"- 沉淀教训数: **{b_lessons_count} 条**（+{deltas['b_lessons_count']}）",
    ]

    if recent_metrics:
        md.append("")
        md.append("### 最近训练质量")
        for m in recent_metrics:
            ts = m.get("timestamp", "")[:16].replace("T", " ")
            n = m.get("n", 0)
            avg = m.get("avg_score", 0)
            passr = m.get("pass_rate", 0) * 100
            md.append(f"- {ts}: {n}题 平均{avg}/10 通过率{passr:.0f}%")

    md += [
        "",
        "### Agent A 知识加工",
        f"- 已处理工单回复: **{a_processed}/{a_total}**（{a_pct}%，+{deltas['a_processed']} 今日）",
        f"- 归纳思维模式: **{modes_count} 种**（+{deltas['modes_count']}）",
        f"- 识别解决方式: {len(rmp_keys)} 类 ({', '.join(rmp_keys[:5])}{'...' if len(rmp_keys)>5 else ''})",
        f"- 话题细分: {topic_count} 类",
        "",
        "### 风格规则（在智能回复中生效）",
        f"- 文件大小: **{rules_chars} 字符**（+{deltas['rules_chars']}，上限 8000）",
        "- 实时接入: 每次生成智能回复时直接注入 system prompt",
        "",
        "### 📌 学习成果使用情况",
        "- ✅ 风格规则 → `_generate_styled_reply` system prompt（每次调用）",
        "- ✅ 历史范例 → `reply_trainer.search_examples` 语义检索（top_k=3）",
        "- ✅ 累计教训 → mem0 语义记忆（下次训练时复用）",
    ]

    return {"markdown": "\n".join(md), "snapshot": current_snapshot}


# --- Smart Reply Endpoints ---

@app.post("/api/board/generate-reply")
def generate_reply(request: GenerateReplyRequest, raw_request: Request, _quota=Depends(require_reply_quota)):
    """基于AI分析生成智能回复内容"""
    log_api_request(raw_request, _quota, issue_key=request.issue_key)
    try:
        _cu = get_current_user(raw_request)
        result = board_service.generate_reply_content(
            request.issue_key,
            force=request.force,
            user_id=_cu["id"] if _cu else "",
            project_key=getattr(raw_request.state, "project_key", "") or "",
            force_pass_gate1=request.force_pass_gate1,
            force_pass_gate2=request.force_pass_gate2,
        )
        if result.get("gate") == "completeness":
            _insuf_type = result.get("insufficient_type", "missing_fields")
            return {
                "status": "gate_blocked",
                "issue_key": request.issue_key,
                "gate": "completeness",
                "missing_fields": result.get("missing_fields", []),
                "inquiry_draft": result.get("inquiry_draft", ""),
                "reply_content": "",
                "solution_content": "",
                "ai_analysis": None,
                "gate_decisions": result.get("gate_decisions", {}),
                "info_insufficient": True,
                "insufficient_type": _insuf_type,
                "suggested_reply_method": {"id": "15702", "value": "退回支持"},
                "suggested_issue_type": {"id": "15321", "value": "无效问题"} if _insuf_type == "invalid_description" else None,
                "auto_returned": result.get("auto_returned", False),
                "operation_steps": result.get("operation_steps", []),
            }
        if result.get("gate") == "classification":
            return {
                "status": "gate_blocked",
                "issue_key": request.issue_key,
                "gate": "classification",
                "transfer_to": result.get("transfer_to"),
                "auto_moved": result.get("auto_moved", False),
                "reply_content": "",
                "solution_content": "",
                "ai_analysis": None,
                "gate_decisions": result.get("gate_decisions", {}),
            }
        if result.get("error"):
            return {
                "status": "warning",
                "issue_key": request.issue_key,
                "message": result["error"],
                "reply_content": "",
                "solution_content": "",
                "ai_analysis": None
            }
        return {
            "status": "success",
            "issue_key": request.issue_key,
            "reply_content": result["reply_content"],
            "solution_content": result.get("solution_content", ""),
            "ai_analysis": result["ai_analysis"],
            "word_count": result["word_count"],
            "cached": result.get("cached", False),
            "suggested_reply_method": result.get("suggested_reply_method"),
            "suggested_issue_type": result.get("suggested_issue_type"),
            "generation_method": result.get("generation_method", "unknown"),
            "kb_sources": result.get("kb_sources", []),
            "kb_evidence_count": result.get("kb_evidence_count", 0),
            "examples_used_count": result.get("examples_used_count", 0),
            "style_rules_applied": result.get("style_rules_applied", False),
            "gate": result.get("gate"),
            "gate_decisions": result.get("gate_decisions", {}),
            "missing_fields": result.get("missing_fields", []),
            "inquiry_draft": result.get("inquiry_draft", ""),
            "supervisor_audit": result.get("supervisor_audit"),
            "auto_reply_decision": result.get("auto_reply_decision"),
            "transfer_to": result.get("transfer_to"),
            "auto_moved": result.get("auto_moved", False),
            "grounded_confidence": result.get("grounded_confidence"),
            "kb_hits_scored": result.get("kb_hits_scored", []),
            "similar_issues_scored": result.get("similar_issues_scored", []),
            "reply_strategy": result.get("reply_strategy", ""),
            "final_action": _final_action_r,
            "blocked_by": result.get("blocked_by", []),
            "auto_dispatch": _dispatch_info,
            "reply_gateway": result.get("reply_gateway"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reply/context")
def build_reply_context(request: GenerateReplyRequest, raw_request: Request,
                        _user=Depends(require_authenticated_user)):
    """MCP 委托模式：跑完非 LLM 的上下文收集 + gate 判定，返回证据 + prompt 模板，
    **不调用 LLM 生成正文**。供调用方 Agent（OpenClaw/WorkBuddy/Claude Code）
    用各自的 LLM 生成回复。若某 gate 拦截，则返回该 gate 的阻断信息。"""
    log_api_request(raw_request, _user, issue_key=request.issue_key)
    try:
        _cu = get_current_user(raw_request)
        result = board_service.generate_reply_content(
            request.issue_key,
            force=request.force,
            user_id=_cu["id"] if _cu else "",
            project_key=getattr(raw_request.state, "project_key", "") or "",
            force_pass_gate1=request.force_pass_gate1,
            force_pass_gate2=request.force_pass_gate2,
            context_only=True,
        )
        return {"status": "success", "issue_key": request.issue_key, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CheckCompletenessRequest(BaseModel):
    issue_key: str
    project: str = ""
    issue_type_confirmed: str = ""
    description: str = ""


@app.post("/api/board/check-completeness")
def check_completeness_endpoint(request: CheckCompletenessRequest, raw_request: Request):
    """Gate 1：前端预检信息完整性（不生成回复）"""
    try:
        from services import completeness_checker
        result = completeness_checker.check(
            issue_key=request.issue_key,
            project=request.project,
            issue_type_confirmed=request.issue_type_confirmed,
            description=request.description,
            attachment_texts=[],
        )
        return {
            "status": "success",
            "passed": result.passed,
            "missing_fields": result.missing_fields,
            "inquiry_draft": result.inquiry_draft,
            "rule_matched": result.rule_matched,
            "gate_enabled": result.gate_enabled,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RollbackAutoMoveRequest(BaseModel):
    issue_key: str

@app.post("/api/board/rollback-auto-move")
def rollback_auto_move(request: RollbackAutoMoveRequest, raw_request: Request):
    """回滚 Gate 2 auto-move：将工单标记为已回滚，供坐席手动处理。"""
    try:
        import json as _json
        from pathlib import Path as _P
        log_path = _P("data/auto_move_log.json")
        if not log_path.exists():
            raise HTTPException(status_code=404, detail="auto_move_log not found")
        log = _json.loads(log_path.read_text())
        entry = log.get(request.issue_key)
        if not entry:
            raise HTTPException(status_code=404, detail=f"No auto-move record for {request.issue_key}")
        entry["rolled_back"] = True
        from datetime import datetime as _dt
        entry["rolled_back_at"] = _dt.now().isoformat()
        log[request.issue_key] = entry
        log_path.write_text(_json.dumps(log, ensure_ascii=False, indent=2))
        return {"status": "success", "issue_key": request.issue_key, "original_project": entry.get("original_project")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Gate Decision Endpoints ---

class BatchApproveRequest(BaseModel):
    approval_ids: list
    approver: str = "operator"

class BatchRejectRequest(BaseModel):
    approval_ids: list
    reason: str = ""
    approver: str = "operator"

class RollbackGateActionRequest(BaseModel):
    issue_key: str
    decision_id: str = ""
    reason: str = ""


@app.get("/api/board/gate-stats")
def gate_stats(hours: int = Query(24, ge=1, le=168)):
    """返回过去 hours 小时内的闸门决策统计摘要。"""
    try:
        from services.gate_decision_log import get_stats as _gate_get_stats
        return _gate_get_stats(hours=hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/board/tickets-by-gate-action")
def tickets_by_gate_action(hours: int = Query(72, ge=1, le=168)):
    """按 final_action 分组返回 issue_keys，供看板闸门视图使用。"""
    try:
        from services.gate_decision_log import get_tickets_by_action as _gate_tickets
        return _gate_tickets(hours=hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/board/gate-log/{issue_key}")
def get_gate_log(issue_key: str, raw_request: Request):
    """获取工单的历史 gate 决策记录（用于 skill/浏览查询）"""
    try:
        from services.gate_decision_log import get_recent_decision, get_gate_summary
        record = get_recent_decision(issue_key)
        summary = get_gate_summary(issue_key)
        if not record:
            return {"issue_key": issue_key, "has_data": False, "gate_summary": None, "reply_gateway": None}
        return {
            "issue_key": issue_key,
            "has_data": True,
            "ts": record.get("ts"),
            "final_action": record.get("final_action"),
            "gate_summary": summary,
            "reply_gateway": record.get("reply_gateway", {}),
            "auto_reply_decision": record.get("auto_reply_decision"),
        }
    except Exception as e:
        return {"issue_key": issue_key, "has_data": False, "error": str(e)}


_RETURN_KEYWORDS = ('报错', '失败', '异常', '错误', '不能', '无法', '故障', '提示')

def _classify_recommendation_type(issue: dict) -> str:
    """中等置信度（0.60-0.84）工单推荐动作：reply_suitable | move_or_assign | return"""
    summary = issue.get('summary', '') or ''
    desc = issue.get('description', '') or ''
    attachments = issue.get('attachment_count') or len(issue.get('attachments') or [])
    ai_conf = (issue.get('ai_analysis') or {}).get('confidence') or 0

    # 信息严重不足 → 推荐退回
    if len(desc) < 50 and attachments == 0:
        return 'return'
    if any(k in summary for k in _RETURN_KEYWORDS) and attachments == 0 and len(desc) < 120:
        return 'return'

    # 中低置信度 + 异常关键词 → 可能项目错配 → 推荐转交
    if any(k in summary for k in _RETURN_KEYWORDS) and 0.60 <= ai_conf <= 0.74:
        return 'move_or_assign'

    return 'reply_suitable'


@app.get("/api/board/gate-view-tickets")
def get_gate_view_tickets(request: Request, background_tasks: BackgroundTasks, hours: int = Query(720, ge=1), include_unscored: bool = True, project_key: str = Query("LCZX"), domain_modules: Optional[str] = Query(None), assignee: Optional[str] = Query(None, description="经办人过滤；空=当前用户，ALL=全部")):
    """
    闸门看板数据源：将当前可见的 Jira 工单与历史闸门决策缓存融合。
    无决策记录的工单默认归入 manual 列并标记 unscored=True。
    hours: gate_decisions.jsonl 回溯窗口（默认 30 天）
    include_unscored: True 时，未出现在决策日志中的工单以 manual+unscored 形式呈现
    """
    _ALL_ACTIONS = [
        "auto_returned", "auto_moved", "auto_replied_normal", "auto_replied_low_risk",
        "auto_assigned", "pending_batch_approve", "needs_decision", "manual",
    ]

    # 1. 读取历史决策缓存
    _decisions_raw: dict = {}
    try:
        from services.gate_decision_log import get_tickets_by_action as _gate_tickets
        _decisions_raw = _gate_tickets(hours=hours)
        by_key: dict = dict(_decisions_raw.get("by_key", {}))
    except Exception:
        by_key = {}

    # 2. 读取当前看板工单（优先实时 Jira，降级到本地缓存）
    all_issues = []
    try:
        jira_client = build_request_jira_client(request, require_binding=False)
        _gv_modules = [m.strip() for m in domain_modules.split(",") if m.strip()] if domain_modules else []
        board_data = board_service.get_board_data(jira_client=jira_client, project_key=project_key, domain_modules=_gv_modules)
        for col_issues in board_data.values():
            for iss in col_issues:
                if isinstance(iss, dict):
                    all_issues.append(iss)
    except Exception:
        # fallback to cache
        try:
            cached = jira_svc.load_board_cache()
            all_issues = [
                {
                    "key": iss.key,
                    "summary": iss.summary,
                    "due_date": iss.due_date or "",
                    "customer_name": iss.customer_name or "",
                    "priority": iss.priority or "",
                    "description": "",
                }
                for iss in cached
            ]
        except Exception:
            all_issues = []

    # 2b. 经办人过滤：默认只看当前用户名下的工单
    _cu = get_current_user(request)
    _me_names: set[str] = set()
    if assignee in (None, ""):
        if _cu:
            for _k in ("username", "display_name"):
                if _cu.get(_k):
                    _me_names.add(_cu[_k])
    elif assignee.upper() not in ("ALL", "*"):
        _me_names = {assignee}
    # ALL / * → _me_names 为空，不过滤
    if _me_names:
        all_issues = [iss for iss in all_issues if (iss.get("assignee") or "") in _me_names]

    # 3. customer tagger
    try:
        from services.customer_priority_tagger import is_key_customer as _is_key
    except Exception:
        def _is_key(name): return False

    # 3b. AI analysis cache — 一次性加载，供 ai_confidence fallback 使用
    _ai_cache: dict = {}
    try:
        import os as _os2
        _ac_path = _os2.path.join(_os2.path.dirname(__file__), "data_cache", "analysis_cache.json")
        if _os2.path.exists(_ac_path):
            with open(_ac_path, "r", encoding="utf-8") as _acf:
                _ai_cache = json.load(_acf)
    except Exception:
        _ai_cache = {}

    # 3c. Reply cache — 供看板列表展示预计算回复内容
    _reply_cache: dict = {}
    try:
        from reply_cache_service import CACHE_FILE as _rc_file
        from datetime import datetime as _dt_rc, timedelta as _td
        if _os2.path.exists(_rc_file):
            with open(_rc_file, "r", encoding="utf-8") as _rcf:
                _rc_raw = json.load(_rcf)
            _now = _dt_rc.now()
            for _rk, _rv in _rc_raw.items():
                try:
                    if _now - _dt_rc.fromisoformat(_rv["timestamp"]) <= _td(days=7):
                        _reply_cache[_rk] = _rv
                except Exception:
                    pass
    except Exception:
        _reply_cache = {}

    # 4. 融合：以当前工单池为主，补充历史决策记录
    _ACTIVE_STATUS = {"待分析"}
    grouped: dict = {a: [] for a in _ALL_ACTIONS}
    seen_keys: set = set()

    for issue in all_issues:
        k = issue.get("key", "")
        if not k:
            continue
        if project_key and not k.startswith(f"{project_key}-"):
            continue
        cur_status = (issue.get("status") or "").strip()
        if cur_status and cur_status not in _ACTIVE_STATUS:
            continue
        seen_keys.add(k)
        dec = by_key.get(k)
        _ai_a = _ai_cache.get(k) or {}
        _ai_conf = _ai_a.get("confidence")
        _priority = issue.get("priority", "")
        _rc_entry = _reply_cache.get(k, {})
        _reply_content = _rc_entry.get("reply_content", "")
        _reply_status = "cached" if _reply_content else "unavailable"
        if dec:
            action = dec.get("action", "manual")
            desc = issue.get("description", "") or ""
            attachments = len(issue.get("attachments") or [])
            _cs = dec.get("composite_score")
            by_key[k] = {
                **dec,
                "composite_score": None if (_cs is None or _cs == 0.0) else _cs,
                "ai_confidence": _ai_conf,
                "is_reused": _ai_a.get("is_reused", False),
                "priority": _priority,
                "description_length": len(desc),
                "attachment_count": attachments,
                "recommendation": _classify_recommendation_type(issue),
                "reply_content": _reply_content,
                "reply_cached_at": _rc_entry.get("timestamp", ""),
                "reply_status": _reply_status,
            }
            if action in grouped:
                grouped[action].append(k)
            else:
                grouped["manual"].append(k)
        elif include_unscored:
            customer_name = issue.get("customer_name", "")
            desc = issue.get("description", "") or ""
            attachments = len(issue.get("attachments") or [])
            by_key[k] = {
                "action": "manual",
                "summary": (issue.get("summary", "") or "")[:120],
                "due_date": issue.get("due_date", "") or "",
                "is_key_customer": _is_key(customer_name),
                "unscored": True,
                "ai_confidence": _ai_conf,
                "is_reused": _ai_a.get("is_reused", False),
                "priority": _priority,
                "description_length": len(desc),
                "attachment_count": attachments,
                "recommendation": _classify_recommendation_type(issue),
                "reply_content": _reply_content,
                "reply_cached_at": _rc_entry.get("timestamp", ""),
                "reply_status": _reply_status,
            }
            grouped["manual"].append(k)

    # 历史决策中已不在当前工单池（含跨项目、非待分析）的条目不在看板中展示
    by_key = {k: v for k, v in by_key.items() if k in seen_keys}

    _cached_count = sum(1 for k in seen_keys if _reply_cache.get(k, {}).get("reply_content"))
    grouped["precompute_progress"] = {
        "total": len(seen_keys),
        "cached": _cached_count,
        "missing": len(seen_keys) - _cached_count,
    }
    grouped["by_key"] = by_key
    # 后台入队：对本次可见工单触发 AI 分析（不阻塞响应）
    background_tasks.add_task(board_service._auto_submit_analysis, list(all_issues))
    return grouped


class PrecomputeRepliesRequest(BaseModel):
    project_key: str = "MYPROJECT"
    modules: list = []


_precompute_running: dict = {}  # lock_key → start_timestamp (float)


@app.post("/api/board/precompute-replies")
async def precompute_replies(
    request_data: PrecomputeRepliesRequest,
    raw_request: Request,
):
    """Fire-and-forget：批量预生成看板工单的智能回复并写入 reply_cache.json。
    幂等：同一 project+modules 组合同时只允许一个任务在跑。"""
    lock_key = f"{request_data.project_key}:{','.join(sorted(str(m) for m in request_data.modules))}"
    import time as _time_pc
    _lock_started = _precompute_running.get(lock_key)
    if _lock_started and (_time_pc.time() - _lock_started) < 1800:  # 30 分钟 max-age
        return {"status": "running", "message": "预计算任务已在运行中"}

    _reply_cache_keys: set = set()
    try:
        from reply_cache_service import CACHE_FILE as _rc_file2
        from datetime import datetime as _dt_pc, timedelta as _td2
        if os.path.exists(_rc_file2):
            with open(_rc_file2, "r", encoding="utf-8") as _rcf2:
                _rc_raw2 = json.load(_rcf2)
            _now2 = _dt_pc.now()
            for _rk2, _rv2 in _rc_raw2.items():
                try:
                    if _now2 - _dt_pc.fromisoformat(_rv2["timestamp"]) <= _td2(days=7):
                        if _rv2.get("reply_content"):
                            _reply_cache_keys.add(_rk2)
                except Exception:
                    pass
    except Exception:
        pass

    issue_keys: list = []
    try:
        jira_client = build_request_jira_client(raw_request, require_binding=False)
        _gv_mods = list(request_data.modules)
        board_data = board_service.get_board_data(
            jira_client=jira_client,
            project_key=request_data.project_key,
            domain_modules=_gv_mods,
        )
        _ACTIVE_ST = {"待分析"}
        for col_issues in board_data.values():
            for iss in col_issues:
                if not isinstance(iss, dict):
                    continue
                k = iss.get("key", "")
                if not k:
                    continue
                if request_data.project_key and not k.startswith(f"{request_data.project_key}-"):
                    continue
                cur_st = (iss.get("status") or "").strip()
                if cur_st and cur_st not in _ACTIVE_ST:
                    continue
                if k not in _reply_cache_keys:
                    issue_keys.append(k)
    except Exception as _e:
        print(f"[Precompute] 获取工单列表失败: {_e}")

    already_cached = len(_reply_cache_keys)
    if not issue_keys:
        return {"status": "done", "queued": 0, "already_cached": already_cached}

    import time as _time_pc2
    _precompute_running[lock_key] = _time_pc2.time()

    def _run_precompute(keys: list, proj_key: str, lk: str):
        from concurrent.futures import ThreadPoolExecutor as _TPE
        _max_conc = int(os.environ.get("PRECOMPUTE_MAX_CONCURRENT", "2"))
        success = 0
        failed = 0

        def _worker_for(_k):
            return board_service.generate_reply_content(
                _k, force=False, user_id="precompute",
                project_key=proj_key, force_pass_gate1=True,
            )

        try:
            with _TPE(max_workers=_max_conc, thread_name_prefix="precompute") as _pool:
                _futs = {_pool.submit(_worker_for, k): k for k in keys[:200]}
                for fut, k in _futs.items():
                    try:
                        fut.result(timeout=150)
                        success += 1
                    except Exception as _e:
                        print(f"[Precompute] {k}: {_e}")
                        failed += 1
        finally:
            _precompute_running.pop(lk, None)
            print(f"[Precompute] done lock={lk} success={success} failed={failed}")

    import threading as _threading
    _threading.Thread(target=_run_precompute, args=(issue_keys, request_data.project_key, lock_key), daemon=True).start()

    from datetime import datetime as _dt_pc2
    return {
        "status": "started",
        "queued": len(issue_keys),
        "already_cached": already_cached,
        "started_at": _dt_pc2.now().isoformat(),
    }


@app.get("/api/board/pending-approvals")
def list_pending_approvals(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    project_key: str = Query("", description="按项目前缀过滤；空=全部"),
    assignee: Optional[str] = Query(None, description="经办人过滤；空=当前用户，ALL=全部"),
):
    """列出 staging 中待批准的自动回复草稿。实时查 Jira 状态过滤掉已完成工单。"""
    try:
        from services.pending_approval_store import list_pending as _list_pending
        jira_client = build_request_jira_client(request, require_binding=False)
        _cu = get_current_user(request)
        _me: set[str] = set()
        if assignee in (None, ""):
            if _cu:
                for _k in ("username", "display_name"):
                    if _cu.get(_k):
                        _me.add(_cu[_k])
        elif assignee.upper() not in ("ALL", "*"):
            _me = {assignee}
        return {"items": _list_pending(
            limit=limit, offset=offset, jira_client=jira_client,
            project_key=project_key, assignee_filter=_me or None,
        )}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/board/batch-approve-replies")
def batch_approve_replies(request: BatchApproveRequest):
    """批量通过 staging 中的自动回复草稿，真正发送到 Jira。"""
    from services.pending_approval_store import approve as _approve
    completed, failed, results = 0, 0, []
    for aid in request.approval_ids:
        try:
            res = _approve(aid, approver=request.approver)
            results.append({"approval_id": aid, "status": "approved", **res})
            completed += 1
        except Exception as e:
            results.append({"approval_id": aid, "status": "failed", "error": str(e)})
            failed += 1
    return {"status": "success", "data": {"total": len(request.approval_ids), "completed": completed, "failed": failed, "results": results}}


@app.post("/api/board/batch-reject-replies")
def batch_reject_replies(request: BatchRejectRequest):
    """批量驳回 staging 中的自动回复草稿，转为人工处理。"""
    from services.pending_approval_store import reject as _reject
    completed, failed, results = 0, 0, []
    for aid in request.approval_ids:
        try:
            _reject(aid, approver=request.approver, reason=request.reason)
            results.append({"approval_id": aid, "status": "rejected"})
            completed += 1
        except Exception as e:
            results.append({"approval_id": aid, "status": "failed", "error": str(e)})
            failed += 1
    return {"status": "success", "data": {"total": len(request.approval_ids), "completed": completed, "failed": failed, "results": results}}


@app.post("/api/board/rollback-gate-action")
def rollback_gate_action(request: RollbackGateActionRequest):
    """撤销一次自动闸门动作（1h 内有效）。记录回滚日志，不真正反转 Jira 操作。"""
    try:
        from services.gate_decision_log import get_recent_decision as _get_recent
        from datetime import datetime, timezone, timedelta
        decision = _get_recent(request.issue_key)
        if not decision:
            raise HTTPException(status_code=404, detail=f"No gate decision found for {request.issue_key}")
        ts_str = decision.get("ts", "")
        if ts_str:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - ts > timedelta(hours=1):
                raise HTTPException(status_code=409, detail="Rollback window (1h) has expired")
        import json as _json, os as _os
        rollback_path = _os.path.join(_os.path.dirname(__file__), "data", "gate_rollback_log.jsonl")
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "issue_key": request.issue_key,
            "decision_id": request.decision_id,
            "reason": request.reason,
            "original_action": decision.get("final_action", ""),
        }
        _os.makedirs(_os.path.dirname(rollback_path), exist_ok=True)
        with open(rollback_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")
        return {"status": "success", "issue_key": request.issue_key, "original_action": decision.get("final_action", "")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/board/dismiss-gate-ticket/{issue_key}")
def dismiss_gate_ticket(issue_key: str):
    """将工单从人工决策列表移除（标记为 manual/已处理）。"""
    from services.gate_decision_log import log_gate_decision as _log
    _log(issue_key, final_action="manual", actor="human", force=True)
    return {"ok": True, "issue_key": issue_key}


@app.get("/api/board/processing-log")
def get_processing_log_endpoint(hours: int = Query(168, ge=1, le=720)):
    """返回过去 hours 小时内所有处理过的工单记录，供处理日志表格使用。"""
    from services.gate_decision_log import get_processing_log
    rows = get_processing_log(hours=hours)
    return {"total": len(rows), "hours": hours, "rows": rows}


# ── Final Action Schema CRUD ────────────────────────────────────────────────
@app.get("/api/config/final-actions")
def get_final_actions():
    """返回所有 final_action 定义（含统计）。"""
    from services.final_action_registry import list_actions
    from services.gate_decision_log import get_stats
    actions = list_actions()
    try:
        stats = get_stats(hours=24)
        by_action = stats.get("by_action", {})
    except Exception:
        by_action = {}
    for a in actions:
        a["count_24h"] = by_action.get(a["key"], 0)
    return {"actions": actions}


@app.post("/api/config/final-actions")
def create_final_action(body: dict, raw_request: Request):
    require_admin_user(raw_request)
    from services.final_action_registry import upsert_action
    try:
        result = upsert_action(body)
        return {"ok": True, "action": result}
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(400, str(e))


@app.put("/api/config/final-actions/{key}")
def update_final_action(key: str, body: dict, raw_request: Request):
    require_admin_user(raw_request)
    from services.final_action_registry import upsert_action
    body["key"] = key
    try:
        result = upsert_action(body)
        return {"ok": True, "action": result}
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(400, str(e))


@app.delete("/api/config/final-actions/{key}")
def delete_final_action_endpoint(key: str, raw_request: Request):
    require_admin_user(raw_request)
    from services.final_action_registry import delete_action
    ok = delete_action(key)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(400, "内置动作不可删除或动作不存在")
    return {"ok": True}


@app.post("/api/config/final-actions/{key}/toggle")
def toggle_final_action(key: str, body: dict, raw_request: Request):
    require_admin_user(raw_request)
    from services.final_action_registry import toggle_action
    result = toggle_action(key, body.get("enabled", True))
    if not result:
        from fastapi import HTTPException
        raise HTTPException(404, "动作不存在")
    return {"ok": True, "action": result}


# ── C1: Gate 1 信息完整性回复模板 CRUD ──────────────────────────────────────
import pathlib as _pathlib

_COMPLETENESS_SCHEMA_PATH = _pathlib.Path(__file__).parent / "data" / "completeness_schema.json"

_KNOWN_PROJECTS = [
    "流程中心", "业务流", "消息中心", "开发框架", "元数据",
    "规则", "公式", "打印", "权限", "组织", "档案和应用", "导入导出",
]
_KNOWN_ISSUE_TYPES = ["bug", "需求", "客开", "数据问题", "实施问题", "需求问题"]


def _load_completeness_schema() -> dict:
    with open(_COMPLETENESS_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_completeness_schema(schema: dict) -> None:
    with open(_COMPLETENESS_SCHEMA_PATH, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)


@app.get("/api/config/completeness-rules/preview")
def preview_completeness_rule(
    project: str = "",
    issue_type: str = "",
    missing_fields: str = "",
    raw_request: Request = None,
):
    require_admin_user(raw_request)
    schema = _load_completeness_schema()
    rules = schema.get("rules", [])
    matched_rule = None
    rule_label = "default_fallback"
    for r in rules:
        if r.get("project") == project and r.get("issue_type_confirmed") == issue_type:
            matched_rule = r
            rule_label = f"{project}/{issue_type}"
            break
    if matched_rule is None:
        matched_rule = schema.get("default_fallback", {})
    template = matched_rule.get("inquiry_template", "")
    field_list = [f.strip() for f in missing_fields.split(",") if f.strip()]
    fields_text = "\n".join(f"- {f}" for f in field_list) if field_list else "（无缺失字段）"
    inquiry_text = template.replace("{missing_fields_list}", fields_text)
    return {"inquiry_text": inquiry_text, "rule_matched": rule_label}


@app.get("/api/config/completeness-rules")
def get_completeness_rules(raw_request: Request):
    require_admin_user(raw_request)
    schema = _load_completeness_schema()
    return {
        "rules": schema.get("rules", []),
        "default_fallback": schema.get("default_fallback", {}),
        "known_projects": _KNOWN_PROJECTS,
        "known_issue_types": _KNOWN_ISSUE_TYPES,
    }


@app.post("/api/config/completeness-rules")
def create_completeness_rule(body: dict, raw_request: Request):
    require_admin_user(raw_request)
    project = body.get("project", "")
    issue_type = body.get("issue_type_confirmed", "")
    if project not in _KNOWN_PROJECTS:
        raise HTTPException(400, f"未知项目: {project}")
    if issue_type not in _KNOWN_ISSUE_TYPES:
        raise HTTPException(400, f"未知工单类型: {issue_type}")
    schema = _load_completeness_schema()
    rule = {
        "project": project,
        "issue_type_confirmed": issue_type,
        "required": body.get("required", []),
        "recommended": body.get("recommended", []),
        "optional": body.get("optional", []),
        "inquiry_template": body.get("inquiry_template", ""),
    }
    schema.setdefault("rules", []).append(rule)
    _save_completeness_schema(schema)
    idx = len(schema["rules"]) - 1
    return {"status": "ok", "idx": idx, "rule": rule}


@app.put("/api/config/completeness-rules/{idx}")
def update_completeness_rule(idx: int, body: dict, raw_request: Request):
    require_admin_user(raw_request)
    schema = _load_completeness_schema()
    rules = schema.get("rules", [])
    if idx < 0 or idx >= len(rules):
        raise HTTPException(404, f"规则索引 {idx} 不存在")
    project = body.get("project", rules[idx].get("project", ""))
    issue_type = body.get("issue_type_confirmed", rules[idx].get("issue_type_confirmed", ""))
    if project not in _KNOWN_PROJECTS:
        raise HTTPException(400, f"未知项目: {project}")
    if issue_type not in _KNOWN_ISSUE_TYPES:
        raise HTTPException(400, f"未知工单类型: {issue_type}")
    rule = {
        "project": project,
        "issue_type_confirmed": issue_type,
        "required": body.get("required", []),
        "recommended": body.get("recommended", []),
        "optional": body.get("optional", schema["rules"][idx].get("optional", [])),
        "inquiry_template": body.get("inquiry_template", ""),
    }
    schema["rules"][idx] = rule
    _save_completeness_schema(schema)
    return {"status": "ok", "rule": rule}


@app.delete("/api/config/completeness-rules/{idx}")
def delete_completeness_rule(idx: int, raw_request: Request):
    require_admin_user(raw_request)
    schema = _load_completeness_schema()
    rules = schema.get("rules", [])
    if idx < 0 or idx >= len(rules):
        raise HTTPException(404, f"规则索引 {idx} 不存在")
    deleted = rules.pop(idx)
    _save_completeness_schema(schema)
    return {"status": "ok", "deleted": deleted}


# ── C2: Gate 2 项目路由规则 CRUD ─────────────────────────────────────────────

_GATE2_ROUTING_PATH = _pathlib.Path(__file__).parent / "data" / "gate2_routing.json"


def _load_gate2_routing() -> dict:
    with open(_GATE2_ROUTING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_gate2_routing(data: dict) -> None:
    with open(_GATE2_ROUTING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


@app.get("/api/config/gate2-routing")
def get_gate2_routing(raw_request: Request):
    require_admin_user(raw_request)
    return _load_gate2_routing()


@app.post("/api/config/gate2-routing")
def create_gate2_routing_rule(body: dict, raw_request: Request):
    require_admin_user(raw_request)
    predicted_project = body.get("predicted_project", "")
    if predicted_project not in _KNOWN_PROJECTS:
        raise HTTPException(400, f"未知项目: {predicted_project}")
    data = _load_gate2_routing()
    rules = data.setdefault("rules", [])
    if any(r.get("predicted_project") == predicted_project for r in rules):
        raise HTTPException(409, f"项目路由规则已存在: {predicted_project}")
    rule = {
        "predicted_project": predicted_project,
        "target_board_id": body.get("target_board_id", ""),
        "target_swimlane": body.get("target_swimlane", ""),
        "sub_module_keywords": body.get("sub_module_keywords", []),
        "default_assignee": body.get("default_assignee", ""),
        "fallback_assignee": body.get("fallback_assignee", ""),
        "auto_move_enabled": bool(body.get("auto_move_enabled", False)),
        "min_confidence": float(body.get("min_confidence", 0.92)),
    }
    rules.append(rule)
    _save_gate2_routing(data)
    return {"status": "ok", "idx": len(rules) - 1, "rule": rule}


@app.put("/api/config/gate2-routing/{predicted_project}")
def update_gate2_routing_rule(predicted_project: str, body: dict, raw_request: Request):
    require_admin_user(raw_request)
    data = _load_gate2_routing()
    rules = data.get("rules", [])
    for i, r in enumerate(rules):
        if r.get("predicted_project") == predicted_project:
            rule = {
                "predicted_project": predicted_project,
                "target_board_id": body.get("target_board_id", r.get("target_board_id", "")),
                "target_swimlane": body.get("target_swimlane", r.get("target_swimlane", "")),
                "sub_module_keywords": body.get("sub_module_keywords", r.get("sub_module_keywords", [])),
                "default_assignee": body.get("default_assignee", r.get("default_assignee", "")),
                "fallback_assignee": body.get("fallback_assignee", r.get("fallback_assignee", "")),
                "auto_move_enabled": bool(body.get("auto_move_enabled", r.get("auto_move_enabled", False))),
                "min_confidence": float(body.get("min_confidence", r.get("min_confidence", 0.92))),
            }
            rules[i] = rule
            _save_gate2_routing(data)
            return {"status": "ok", "rule": rule}
    raise HTTPException(404, f"路由规则不存在: {predicted_project}")


@app.delete("/api/config/gate2-routing/{predicted_project}")
def delete_gate2_routing_rule(predicted_project: str, raw_request: Request):
    require_admin_user(raw_request)
    data = _load_gate2_routing()
    rules = data.get("rules", [])
    for i, r in enumerate(rules):
        if r.get("predicted_project") == predicted_project:
            deleted = rules.pop(i)
            _save_gate2_routing(data)
            return {"status": "ok", "deleted": deleted}
    raise HTTPException(404, f"路由规则不存在: {predicted_project}")


@app.patch("/api/config/gate2-routing/{predicted_project}/toggle")
def toggle_gate2_routing_rule(predicted_project: str, raw_request: Request):
    require_admin_user(raw_request)
    data = _load_gate2_routing()
    rules = data.get("rules", [])
    for r in rules:
        if r.get("predicted_project") == predicted_project:
            r["auto_move_enabled"] = not r.get("auto_move_enabled", False)
            _save_gate2_routing(data)
            return {"status": "ok", "auto_move_enabled": r["auto_move_enabled"]}
    raise HTTPException(404, f"路由规则不存在: {predicted_project}")


# --- C4: 重点客户 KPI 动态追加 ---

def _load_customer_tags() -> dict:
    import json as _json
    tags_path = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "customer_tags.json"))
    try:
        return _json.loads(tags_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}

def _save_customer_tags(tags: dict) -> None:
    import json as _json
    tags_path = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "customer_tags.json"))
    tags_path.write_text(_json.dumps(tags, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/config/key-customers")
def get_key_customers(raw_request: Request):
    require_admin_user(raw_request)
    return _load_customer_tags()


@app.post("/api/config/key-customers/pin")
def pin_key_customer(body: dict, raw_request: Request):
    require_admin_user(raw_request)
    name = (body.get("customer_name") or "").strip()
    reason = body.get("reason", "")
    if not name:
        raise HTTPException(400, "customer_name 不能为空")
    from datetime import date as _date
    today = _date.today().isoformat()
    tags = _load_customer_tags()
    existing = tags.get(name)
    if isinstance(existing, dict) and existing.get("source") == "kpi_seed":
        existing["source"] = "manual"
        existing["tag"] = "重点客户"
        existing["pinned_at"] = today
        existing["pin_reason"] = reason
        existing.pop("_suggested_demote", None)
    elif isinstance(existing, str):
        pass  # already tagged
    else:
        tags[name] = {
            "tag": "重点客户",
            "source": "manual",
            "pinned_at": today,
            "pin_reason": reason,
        }
    _save_customer_tags(tags)
    return {"status": "ok", "customer_name": name}


@app.post("/api/config/key-customers/seed-now")
def seed_key_customers_now(raw_request: Request):
    require_admin_user(raw_request)
    try:
        from services.kpi_key_customer_seeder import seed_from_latest_reports
        result = seed_from_latest_reports()
        return {"status": "ok", **result}
    except Exception as exc:
        logger.warning("[key-customers] seed failed: %s", exc)
        raise HTTPException(500, f"seed 失败: {exc}")


@app.get("/api/config/key-customers/underperformers")
def get_key_customers_underperformers(raw_request: Request, period_type: str = "weekly", n: int = 4):
    require_admin_user(raw_request)
    n = min(max(n, 1), 52)
    try:
        from services.kpi_key_customer_seeder import list_underperformers
        return list_underperformers(period_type=period_type, n=n)
    except Exception as exc:
        logger.warning("[key-customers] underperformers failed: %s", exc)
        raise HTTPException(500, f"查询失败: {exc}")


@app.post("/api/config/key-customers/promote-suggestion")
def promote_customer_suggestion(body: dict, raw_request: Request):
    require_admin_user(raw_request)
    name = (body.get("customer_name") or "").strip()
    if not name:
        raise HTTPException(400, "customer_name 不能为空")
    tags = _load_customer_tags()
    entry = tags.get(name)
    if not isinstance(entry, dict):
        raise HTTPException(404, f"客户不存在或非 dict 格式: {name}")
    if isinstance(entry, dict) and entry.get("source") != "kpi_seed":
        raise HTTPException(status_code=400, detail="该客户非 KPI 自动来源，请使用手动固定接口")
    entry["tag"] = "重点客户"
    entry.pop("_suggested_demote", None)
    _save_customer_tags(tags)
    return {"status": "ok", "customer_name": name}


@app.post("/api/config/key-customers/demote")
def demote_key_customer(body: dict, raw_request: Request):
    require_admin_user(raw_request)
    name = (body.get("customer_name") or "").strip()
    if not name:
        raise HTTPException(400, "customer_name 不能为空")
    tags = _load_customer_tags()
    if name not in tags:
        raise HTTPException(404, f"客户不存在: {name}")
    del tags[name]
    _save_customer_tags(tags)
    return {"status": "ok", "customer_name": name}


@app.delete("/api/config/key-customers/{customer_name}")
def delete_key_customer(customer_name: str, raw_request: Request):
    require_admin_user(raw_request)
    tags = _load_customer_tags()
    if customer_name not in tags:
        raise HTTPException(404, f"客户不存在: {customer_name}")
    del tags[customer_name]
    _save_customer_tags(tags)
    return {"status": "ok", "customer_name": customer_name}


# ── C7: 移动/分配特征学习反馈 ─────────────────────────────────────────────────

_LEARNED_PATTERNS_PATH = _pathlib.Path(__file__).parent / "data" / "learned_patterns.json"
_CLASSIFICATION_PROMPT_PATH = _pathlib.Path(__file__).parent / "data" / "gate_prompts" / "classification.md"


def _load_learned_patterns() -> dict:
    if not _LEARNED_PATTERNS_PATH.exists():
        return {
            "version": 1,
            "last_run": None,
            "gate2_routing_suggestions": [],
            "gate2_prompt_few_shot_suggestions": [],
            "classification_keyword_patterns": [],
            "assignee_drift_alerts": [],
            "blacklist": [],
        }
    with open(_LEARNED_PATTERNS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_learned_patterns(data: dict) -> None:
    with open(_LEARNED_PATTERNS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


@app.get("/api/config/learned-patterns")
def get_learned_patterns(raw_request: Request):
    require_admin_user(raw_request)
    return _load_learned_patterns()


@app.post("/api/config/learned-patterns/run-now")
def run_pattern_learning_now(raw_request: Request):
    require_admin_user(raw_request)
    import subprocess as _sp
    script_path = _pathlib.Path(__file__).parent / "scripts" / "pattern_learning_agent.py"
    if not script_path.exists():
        raise HTTPException(500, "pattern_learning_agent.py 不存在")
    import sys as _sys
    proc = _sp.Popen(
        [_sys.executable, str(script_path)],
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
        start_new_session=True,
    )
    logger.info("[learned-patterns] run-now triggered, pid=%d", proc.pid)
    return {"status": "started", "pid": proc.pid}


@app.post("/api/config/learned-patterns/accept-routing/{idx}")
def accept_routing_suggestion(idx: int, raw_request: Request):
    require_admin_user(raw_request)
    data = _load_learned_patterns()
    suggestions = data.get("gate2_routing_suggestions", [])
    if idx < 0 or idx >= len(suggestions):
        raise HTTPException(404, f"建议索引不存在: {idx}")
    suggestion = suggestions[idx]

    # 构造 gate2_routing 规则（复用已有 helper）
    routing_data = _load_gate2_routing()
    rules = routing_data.setdefault("rules", [])
    predicted_project = suggestion.get("predicted_project", "")

    # 若已存在同项目规则则合并关键词，否则新增
    existing = next((r for r in rules if r.get("predicted_project") == predicted_project), None)
    if existing:
        new_kw = suggestion.get("trigger_keywords", [])
        existing_kw = existing.get("sub_module_keywords", [])
        merged_kw = list(dict.fromkeys(existing_kw + [kw for kw in new_kw if kw not in existing_kw]))
        existing["sub_module_keywords"] = merged_kw
        if suggestion.get("target_swimlane") and not existing.get("target_swimlane"):
            existing["target_swimlane"] = suggestion["target_swimlane"]
        if suggestion.get("default_assignee") and not existing.get("default_assignee"):
            existing["default_assignee"] = suggestion["default_assignee"]
        rule = existing
    else:
        rule = {
            "predicted_project": predicted_project,
            "target_board_id": suggestion.get("target_board_id", ""),
            "target_swimlane": suggestion.get("target_swimlane", ""),
            "sub_module_keywords": suggestion.get("trigger_keywords", []),
            "default_assignee": suggestion.get("default_assignee", ""),
            "fallback_assignee": "",
            "auto_move_enabled": False,
            "min_confidence": float(suggestion.get("confidence", 0.92)),
        }
        rules.append(rule)
    _save_gate2_routing(routing_data)

    # 从建议列表移除已接受项
    suggestions.pop(idx)
    data["gate2_routing_suggestions"] = suggestions
    _save_learned_patterns(data)

    return {"status": "ok", "rule": rule}


@app.post("/api/config/learned-patterns/accept-few-shot/{idx}")
def accept_few_shot_suggestion(idx: int, raw_request: Request):
    require_admin_user(raw_request)
    data = _load_learned_patterns()
    suggestions = data.get("gate2_prompt_few_shot_suggestions", [])
    if idx < 0 or idx >= len(suggestions):
        raise HTTPException(404, f"建议索引不存在: {idx}")
    suggestion = suggestions[idx]

    snippet = suggestion.get("snippet", "")
    confidence_pct = int(float(suggestion.get("confidence", 0)) * 100)
    new_line = f"- {snippet} （支持度: {confidence_pct}%）"

    # 读取或创建 classification.md
    _CLASSIFICATION_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _CLASSIFICATION_PROMPT_PATH.exists():
        content = _CLASSIFICATION_PROMPT_PATH.read_text(encoding="utf-8")
    else:
        content = ""

    learned_start = "<!-- LEARNED START -->"
    learned_end = "<!-- LEARNED END -->"

    if learned_start in content and learned_end in content:
        # 在 LEARNED END 前插入新行
        content = content.replace(
            learned_end,
            f"{new_line}\n{learned_end}",
        )
    else:
        # 在文件末尾追加 LEARNED 块
        block = (
            f"\n{learned_start}\n"
            f"# 历史学习规则（自动生成，勿手动编辑）\n"
            f"{new_line}\n"
            f"{learned_end}\n"
        )
        content = content.rstrip("\n") + "\n" + block

    _CLASSIFICATION_PROMPT_PATH.write_text(content, encoding="utf-8")

    # 移除已接受项
    suggestions.pop(idx)
    data["gate2_prompt_few_shot_suggestions"] = suggestions
    _save_learned_patterns(data)

    return {"status": "ok", "appended": new_line}


@app.post("/api/config/learned-patterns/reject/{idx}")
def reject_learned_suggestion(idx: int, raw_request: Request, type: str = "routing"):
    require_admin_user(raw_request)
    data = _load_learned_patterns()

    if type == "few-shot":
        suggestions = data.get("gate2_prompt_few_shot_suggestions", [])
    else:
        suggestions = data.get("gate2_routing_suggestions", [])

    if idx < 0 or idx >= len(suggestions):
        raise HTTPException(404, f"建议索引不存在: {idx}")

    rejected = suggestions.pop(idx)
    blacklist = data.setdefault("blacklist", [])

    # 记录黑名单（以关键词集合为唯一标识）
    from datetime import datetime, timezone
    blacklist_entry = {
        "type": type,
        "keywords": rejected.get("trigger_keywords") or [rejected.get("snippet", "")],
        "rejected_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    blacklist.append(blacklist_entry)

    if type == "few-shot":
        data["gate2_prompt_few_shot_suggestions"] = suggestions
    else:
        data["gate2_routing_suggestions"] = suggestions

    _save_learned_patterns(data)
    return {"status": "ok", "rejected": rejected}


# --- Jira Field Options Endpoint ---

from typing import List
from pydantic import BaseModel

class FieldOptionsRequest(BaseModel):
    issue_id: str
    field_ids: List[str]

@app.post("/api/jira/field-options")
def get_jira_field_options(request: FieldOptionsRequest, raw_request: Request):
    """
    获取Jira自定义字段的枚举值选项

    请求: {
        "issue_id": "MYPROJECT-12345",
        "field_ids": ["customfield_10410", "customfield_10729"]
    }

    响应: {
        "status": "success",
        "data": {
            "customfield_10410": [
                {"id": "10001", "value": "指导解决"},
                {"id": "10002", "value": "自行解决"}
            ]
        }
    }
    """
    try:
        # Validate issue_id format (e.g., MYPROJECT-12345)
        if not request.issue_id or not re.match(r'^[A-Z]+-\d+$', request.issue_id):
            raise HTTPException(status_code=400, detail="Invalid issue_id format. Expected format: PROJECT-12345")

        jira_client = build_request_jira_client(raw_request)
        result = jira_client.get_field_options(request.issue_id, request.field_ids)
        if not result:
            return {
                "status": "warning",
                "message": "无法获取字段选项，请检查Jira连接和权限",
                "data": {}
            }
        return {
            "status": "success",
            "data": result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RefreshFieldOptionsRequest(BaseModel):
    project_key: str = "MYPROJECT"
    issue_type_id: str = "10001"  # Support类型

@app.post("/api/jira/field-options-refresh")
def refresh_jira_field_options(request: RefreshFieldOptionsRequest, raw_request: Request):
    """
    刷新Jira字段选项缓存

    请求: {
        "project_key": "MYPROJECT",
        "issue_type_id": "10001"
    }

    响应: {
        "status": "success",
        "message": "字段选项缓存已刷新",
        "fields_count": 5
    }
    """
    try:
        jira_client = build_request_jira_client(raw_request)
        result = jira_client.refresh_field_options_cache(
            project_key=request.project_key,
            issue_type_id=request.issue_type_id
        )

        if result and result.get('fields'):
            return {
                "status": "success",
                "message": "字段选项缓存已刷新",
                "fields_count": len(result['fields']),
                "timestamp": result.get('timestamp')
            }
        else:
            return {
                "status": "warning",
                "message": "刷新完成，但未获取到字段选项"
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新字段选项失败: {str(e)}")


@app.get("/api/jira/field-options-admin")
def get_field_options_admin(request: Request):
    """
    查看当前字段选项缓存（管理接口）

    响应: {
        "status": "success",
        "data": {
            "timestamp": "2026-02-28 10:00:00",
            "fields": { ... }
        }
    }
    """
    try:
        require_admin_user(request)
        cache = jira_svc._load_field_options_cache()
        if cache:
            return {
                "status": "success",
                "data": cache
            }
        else:
            return {
                "status": "warning",
                "message": "缓存为空，请先调用刷新接口"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取缓存失败: {str(e)}")


@app.post("/api/jira/session-established")
async def jira_session_established(
    background_tasks: BackgroundTasks,
    project_key: str = Query(default="MYPROJECT"),
    scope_modules: list[str] = Query(default=[])
):
    """Jira 会话建立后触发全量基线扫描（异步，不阻塞响应）"""
    def _baseline_scan():
        try:
            tickets = jira_svc.load_board_cache()
            ticket_dicts = [
                {
                    "key": t.key,
                    "summary": t.summary or "",
                    "description": t.description or "",
                    "status": t.status or "",
                    "priority": t.priority or "",
                    "due_date": t.due_date or "",
                    "domain_module": getattr(t, "domain_module", None),
                }
                for t in tickets
            ]
            if scope_modules:
                ticket_dicts = [t for t in ticket_dicts if t.get("domain_module") in scope_modules]
            # 按 due_date 升序，前30张高优先级
            ticket_dicts.sort(key=lambda x: x.get("due_date") or "9999-99-99")
            high_prio = ticket_dicts[:30]
            rest = ticket_dicts[30:]
            board_service._auto_submit_analysis(high_prio)
            board_service._auto_submit_analysis(rest)
            print(f"[SessionEstablished] 基线扫描: 共 {len(ticket_dicts)} 张工单入队, 高优先级 {len(high_prio)} 张")
        except Exception as e:
            print(f"[SessionEstablished] 基线扫描失败: {e}")

    background_tasks.add_task(_baseline_scan)
    return {"baseline_started": True, "message": "基线扫描已在后台启动"}


# --- Reply Training Endpoints (回复训练器) ---

@app.get("/api/training/stats")
def get_training_stats():
    """返回回复训练器统计数据"""
    return {"status": "success", "data": board_service.reply_trainer.get_stats()}

@app.post("/api/training/evolve")
def evolve_style_rules():
    """手动触发风格规则重新提炼"""
    try:
        config = board_service._load_llm_config()
        def llm_fn(prompt):
            return board_service.llm_service.call_llm(
                prompt, api_key=config.get("api_key", ""),
                provider=config.get("provider", "gemini"),
                model_name=config.get("model_name", "gemini-2.0-flash"),
                base_url=config.get("base_url", ""))

        rules = board_service.reply_trainer.evolve_style_rules(llm_fn)
        stats = board_service.reply_trainer.get_stats()
        return {
            "status": "success",
            "message": "风格规则已更新",
            "data": {"stats": stats, "rules_preview": rules[:500]}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"风格规则提炼失败: {str(e)}")


# --- Network Cache and Monitoring Endpoints (三节点架构) ---

@app.get("/api/network/health")
def network_health():
    """
    网络健康检查接口

    返回网络服务状态
    """
    cache_enabled = jira_cache_service is not None
    monitor_enabled = network_monitor is not None

    return {
        "status": "success",
        "data": {
            "cache_service": {
                "enabled": cache_enabled,
                "status": "running" if cache_enabled else "disabled"
            },
            "network_monitor": {
                "enabled": monitor_enabled,
                "status": "running" if network_monitor else "disabled"
            }
        }
    }

@app.get("/api/network/metrics")
def network_metrics():
    """
    获取网络缓存指标

    返回缓存和节点的性能指标
    """
    if not jira_cache_service:
        raise HTTPException(status_code=503, detail="缓存服务未启用")

    return jira_cache_service.get_metrics()

@app.get("/api/network/summary")
def network_summary():
    """
    获取网络监控摘要

    返回整体网络状态摘要
    """
    if not network_monitor:
        raise HTTPException(status_code=503, detail="网络监控未启用")

    return network_monitor.get_summary()

@app.get("/api/network/nodes")
def network_nodes():
    """
    获取所有节点健康状态

    返回每个节点的健康状态
    """
    if not network_monitor:
        raise HTTPException(status_code=503, detail="网络监控未启用")

    health = network_monitor.get_node_health()
    return {"status": "success", "data": health}

@app.get("/api/network/node/{node_name}")
def network_node(node_name: str):
    """
    获取指定节点的健康状态

    Args:
        node_name: 节点名称
    """
    if not network_monitor:
        raise HTTPException(status_code=503, detail="网络监控未启用")

    health = network_monitor.get_node_health(node_name)
    if not health:
        raise HTTPException(status_code=404, detail=f"节点不存在: {node_name}")

    return {"status": "success", "data": health}

@app.get("/api/network/alerts")
def network_alerts(level: str = None, acknowledged: bool = None):
    """
    获取告警列表

    Args:
        level: 告警级别过滤 (info, warning, error, critical)
        acknowledged: 是否已确认过滤 (true/false)
    """
    if not network_monitor:
        raise HTTPException(status_code=503, detail="网络监控未启用")

    alerts = network_monitor.get_alerts(level, acknowledged)
    return {"status": "success", "data": alerts}

@app.post("/api/network/alerts/{alert_id}/acknowledge")
def acknowledge_network_alert(alert_id: str):
    """
    确认告警

    Args:
        alert_id: 告警 ID
    """
    if not network_monitor:
        raise HTTPException(status_code=503, detail="网络监控未启用")

    success = network_monitor.acknowledge_alert(alert_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"告警不存在: {alert_id}")

    return {"status": "success", "message": "告警已确认"}

@app.get("/api/network/performance/{node_name}")
def network_performance(node_name: str, minutes: int = 60):
    """
    获取节点性能统计

    Args:
        node_name: 节点名称
        minutes: 统计时长（分钟）
    """
    if not network_monitor:
        raise HTTPException(status_code=503, detail="网络监控未启用")

    stats = network_monitor.get_performance_stats(node_name, minutes)
    if 'error' in stats:
        raise HTTPException(status_code=404, detail=stats['error'])

    return {"status": "success", "data": stats}

class ClearCacheRequest(BaseModel):
    key_type: Optional[str] = None  # 缓存类型，None 表示清理全部

@app.post("/api/network/cache/clear")
def clear_network_cache(request: ClearCacheRequest = None):
    """
    清理缓存

    请求体:
    {
        "key_type": "fields"  // 可选，不指定则清理全部
    }
    """
    if not jira_cache_service:
        raise HTTPException(status_code=503, detail="缓存服务未启用")

    key_type = request.key_type if request else None
    jira_cache_service.clear_cache(key_type)

    return {
        "status": "success",
        "message": f"缓存已清理: {key_type or '全部'}"
    }


# --- Lifecycle Management ---


def _reap_zombie_running_tasks():
    """启动时把上一次进程残留的 running 任务标记为 failed。
    waiting_theme_confirm 任务超过 90 分钟也标失败（60 分钟为最大等待，超过即 daemon 已死）。"""
    import sqlite3 as _sqlite3
    import json as _json
    try:
        from services.agent_task_store import AgentTaskStore
        db_path = AgentTaskStore.get_instance()._db_path
        with _sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, result_json FROM agent_tasks
                WHERE status='running'
                  AND (started_at IS NULL
                       OR started_at < datetime('now', '-5 minutes', 'localtime'))
            """)
            rows = cur.fetchall()
            reaped = []
            for tid, result_raw in rows:
                try:
                    stage = (_json.loads(result_raw) if result_raw else {}).get("pipeline_stage")
                except Exception:
                    stage = None
                # waiting_theme_confirm 保留，直到超过 90 分钟才认为 daemon 已死
                if stage == "waiting_theme_confirm":
                    cur.execute("""SELECT 1 FROM agent_tasks WHERE id=?
                        AND started_at < datetime('now', '-90 minutes', 'localtime')""", (tid,))
                    if not cur.fetchone():
                        continue  # 90 分钟内的主题确认闸门，继续等
                cur.execute(
                    "UPDATE agent_tasks SET status='failed',"
                    " finished_at=datetime('now','localtime'),"
                    " log_tail=COALESCE(log_tail,'')||? WHERE id=?",
                    ("\n[startup-reap] 后端重启，残留任务标记失败\n", tid),
                )
                reaped.append(tid)
            conn.commit()
        if reaped:
            print(f"[startup-reap] reaped {len(reaped)} zombie running tasks")
    except Exception as e:
        print(f"[startup-reap] 清理失败（不影响启动）: {e}")


def _kb_startup_sync_and_warn():
    """Lifespan startup: incremental KB sync + warn for domains present on disk but not compiled."""
    try:
        logger.info("[KB] lifespan startup: incremental sync starting")
        kb_runtime_service.sync(force_refresh=False)
        logger.info("[KB] lifespan startup: incremental sync done")
    except Exception as e:
        logger.warning("[KB] lifespan startup: incremental sync failed (non-fatal): %s", e)

    # Compare converted/ directories vs compiled entries in documents table
    try:
        import os as _os
        converted_root = Path(__file__).parent.parent.parent / "KB" / "OUTPUT" / "converted"
        if converted_root.exists():
            disk_domains = {
                d for d in _os.listdir(str(converted_root))
                if (converted_root / d).is_dir() and not d.startswith("__") and not d.startswith(".")
            }
            # Fetch compiled names from documents table, strip "综合解析：" prefix
            compiled_names: set[str] = set()
            try:
                rows = kb_runtime_service.hybrid_index.conn.execute(
                    "SELECT name FROM documents WHERE source_kind = 'kb_compiled'"
                ).fetchall()
                for row in rows:
                    name = row[0] if isinstance(row, (list, tuple)) else row["name"]
                    stripped = name.replace("综合解析：", "").strip()
                    compiled_names.add(stripped)
            except Exception as db_e:
                logger.warning("[KB] startup domain-check: DB query failed: %s", db_e)

            for domain in sorted(disk_domains):
                if domain not in compiled_names:
                    logger.warning("[KB] domain '%s' not compiled, trigger refresh", domain)
    except Exception as e:
        logger.warning("[KB] startup domain-check failed (non-fatal): %s", e)


@app.on_event("startup")
async def startup_event():
    import threading as _threading
    import anyio
    # 默认 total_tokens=40，提到 64 以支持更多并发 handler 线程（每线程 ~8MB stack）
    anyio.to_thread.current_default_thread_limiter().total_tokens = 64
    # 限制 torch/BLAS 内部线程数为 1，防止多线程并发调用 encode() 时 OpenMP 死锁
    try:
        import torch as _torch
        _torch.set_num_threads(1)
    except Exception:
        pass
    _reap_zombie_running_tasks()
    _threading.Thread(target=_kb_startup_sync_and_warn, daemon=True).start()


@app.on_event("shutdown")
def shutdown_event():
    """服务关闭时清理资源"""
    print("[Shutdown] 正在关闭服务...")
    try:
        board_service.cleanup()
    except Exception as e:
        print(f"[Shutdown Error] {e}")

    # 关闭网络服务
    try:
        if network_monitor:
            network_monitor.stop()
            print("[Shutdown] 网络监控已停止")
    except Exception as e:
        print(f"[Shutdown Error] {e}")

    try:
        if jira_cache_service:
            jira_cache_service.shutdown()
            print("[Shutdown] 缓存服务已停止")
    except Exception as e:
        print(f"[Shutdown Error] {e}")

# --- Board Configuration Endpoints ---

@app.get("/api/config/board")
def get_board_config():
    """Get board configuration (read-only, no auth required)"""
    return board_service._load_board_config()

@app.post("/api/config/board")
def save_board_config(config: Dict[str, Any]):
    """Save board configuration (no auth required — personal workspace config)"""
    result = board_service.save_board_config(config)
    if not result:
        raise HTTPException(status_code=500, detail="Failed to save config")
    return {"status": "success"}

# --- Reply Gates Configuration Endpoints ---

@app.get("/api/config/reply-gates")
def get_reply_gates_config():
    """读取 reply_gates.yaml 并以 JSON 形式返回。"""
    return reply_gates_mgr.load()


@app.patch("/api/config/reply-gates")
def update_reply_gates_config(update: Dict[str, Any], raw_request: Request):
    """深合并写回 reply_gates.yaml，通过 PipelineConfigManager。"""
    require_admin_user(raw_request)
    try:
        result = reply_gates_mgr.patch(update)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Configuration Endpoints ---

@app.get("/api/config/jira")
def get_jira_config(request: Request):
    """Get all sections from jira_api.md"""
    require_admin_user(request)
    jira_svc.reload_config()
    return jira_svc.config_parser.sections

class ConfigUpdateRequest(BaseModel):
    title: str
    content: str

@app.post("/api/config/jira")
def update_jira_config(request: ConfigUpdateRequest, raw_request: Request):
    """Update a section in jira_api.md and reload"""
    require_admin_user(raw_request)
    jira_svc.config_parser.update_section(request.title, request.content)
    jira_svc.reload_config()
    return {"status": "success"}

# --- LLM Configuration Endpoints ---

import json

LLM_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "llm_config.json")
LLM_CONFIG_SETTING_KEY = "llm_config"

def load_llm_config():
    """Load LLM config from system settings with file fallback."""
    system_config = auth_service.get_system_setting(LLM_CONFIG_SETTING_KEY)
    if isinstance(system_config, dict) and system_config:
        return system_config

    if os.path.exists(LLM_CONFIG_FILE):
        try:
            with open(LLM_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_llm_config(config: dict, updated_by: str = None):
    """Save LLM config to system settings and file."""
    auth_service.set_system_setting(LLM_CONFIG_SETTING_KEY, config, updated_by=updated_by)
    with open(LLM_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def resolve_default_llm_runtime() -> Dict[str, str]:
    config = load_llm_config()
    provider = config.get("last_provider") or "none"
    provider_config = config.get(provider, {}) if provider != "none" else {}
    return {
        "provider": provider,
        "api_key": provider_config.get("api_key", ""),
        "model_name": provider_config.get("model_name", ""),
        "base_url": provider_config.get("base_url", ""),
    }


LLM_FEATURE_ROUTING_KEY = "llm_feature_routing"

SYSTEM_LLM_FEATURES = [
    {"id": "req_analysis", "name": "需求分析"},
    {"id": "spec_gen", "name": "Spec/PRD 生成"},
    {"id": "competitive", "name": "竞品搜索分析"},
    {"id": "classification", "name": "工单分类"},
    {"id": "smart_reply", "name": "智能回复生成"},
    {"id": "reply_supervisor", "name": "回复质量监督"},
    {"id": "reply_confidence_scoring", "name": "置信度评分"},
]


def resolve_feature_llm_runtime(feature: str, *, exclude_providers: list = None) -> Dict[str, str]:
    """系统级功能的 LLM 路由：按 feature 查找指定 provider，支持 list 降级链。"""
    routing = auth_service.get_system_setting(LLM_FEATURE_ROUTING_KEY) or {}
    val = routing.get(feature) or routing.get("_default") or ""
    providers = val if isinstance(val, list) else ([val] if val else [])

    skip = set(exclude_providers or [])
    config = load_llm_config()

    for provider_name in providers:
        if provider_name in skip:
            continue
        provider_config = config.get(provider_name, {})
        if provider_config.get("api_key"):
            return {
                "provider": provider_name,
                "api_key": provider_config["api_key"],
                "model_name": provider_config.get("model_name", ""),
                "base_url": provider_config.get("base_url", ""),
            }

    return resolve_default_llm_runtime()


def resolve_effective_llm_runtime(
    *,
    feature: str = "",
    provider: str = "",
    api_key: str = "",
    model_name: str = "",
    base_url: str = "",
) -> Dict[str, str]:
    # 无显式 api_key 时：优先走功能路由，无 feature 则走全局默认
    if not api_key and feature:
        defaults = resolve_feature_llm_runtime(feature)
    else:
        defaults = resolve_default_llm_runtime()
    effective_api_key = api_key or defaults["api_key"]
    effective_provider = provider or defaults["provider"]
    effective_model_name = model_name or defaults["model_name"]
    effective_base_url = base_url or defaults["base_url"]
    if not effective_api_key:
        return {
            "provider": effective_provider or "none",
            "api_key": "",
            "model_name": effective_model_name,
            "base_url": effective_base_url,
        }
    return {
        "provider": effective_provider or defaults["provider"] or "none",
        "api_key": effective_api_key,
        "model_name": effective_model_name,
        "base_url": effective_base_url,
    }

@app.get("/api/config/llm")
def get_llm_config_endpoint():
    """Get LLM configuration (read-only, no auth)"""
    return load_llm_config()

class LLMConfigRequest(BaseModel):
    provider: str
    api_key: str = ""
    model_name: str = ""
    base_url: str = ""

@app.post("/api/config/llm")
def set_llm_config(request: LLMConfigRequest):
    """Set LLM configuration for board AI analysis (no auth, personal tool)"""
    config = load_llm_config()
    config[request.provider] = {
        "api_key": request.api_key,
        "model_name": request.model_name,
        "base_url": request.base_url
    }
    config["last_provider"] = request.provider
    save_llm_config(config)

    # 更新BoardService的LLM配置
    board_service.update_llm_config(
        provider=request.provider,
        api_key=request.api_key,
        model_name=request.model_name,
        base_url=request.base_url
    )

    return {"status": "success", "provider": request.provider}


@app.get("/api/config/llm/features")
def get_feature_routing():
    """获取功能级 LLM 路由配置"""
    routing = auth_service.get_system_setting(LLM_FEATURE_ROUTING_KEY) or {}
    config = load_llm_config()
    available_providers = [k for k in config if k != "last_provider"]
    return {
        "routing": routing,
        "features": SYSTEM_LLM_FEATURES,
        "available_providers": available_providers,
    }


class FeatureRoutingRequest(BaseModel):
    routing: Dict[str, str] = {}


@app.post("/api/config/llm/features")
def set_feature_routing(body: FeatureRoutingRequest, request: Request):
    """设置功能级 LLM 路由（需管理员）"""
    admin = require_admin_user(request)
    auth_service.set_system_setting(LLM_FEATURE_ROUTING_KEY, body.routing, updated_by=admin["username"])
    routing_file = os.path.join(os.path.dirname(__file__), "llm_feature_routing.json")
    with open(routing_file, "w", encoding="utf-8") as f:
        json.dump(body.routing, f, ensure_ascii=False, indent=2)
    return {"status": "success"}


class LLMTestRequest(BaseModel):
    provider: str
    api_key: str
    model_name: str = ""
    base_url: str = ""


@app.post("/api/llm/test")
def test_llm_connection(request: LLMTestRequest):
    """测试LLM API连接是否可用"""
    try:
        if not request.api_key:
            return {"status": "error", "message": "API Key 不能为空"}

        # 使用LLM服务测试连接
        # 发送一个简单的测试请求
        test_prompt = "Hello, please respond with 'OK' only."

        if request.provider == "gemini":
            from google import genai
            client = genai.Client(api_key=request.api_key)
            model = request.model_name or "gemini-2.0-flash"

            response = client.models.generate_content(
                model=model,
                contents=test_prompt
            )
            if response.text:
                return {"status": "success", "message": "Gemini API 连接正常"}
        else:
            # OpenAI兼容接口
            from openai import OpenAI
            base_url = request.base_url or "https://api.openai.com/v1"
            print(f"[LLM Test] 使用OpenAI兼容接口: provider={request.provider}, base_url={base_url}, model={request.model_name}")
            # timeout=10/max_retries=0：base_url 不可达时快速失败，避免浏览器 fetch 显示 "Load failed"
            client = OpenAI(api_key=request.api_key, base_url=base_url, timeout=10.0, max_retries=0)
            model = request.model_name or "gpt-3.5-turbo"

            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": test_prompt}],
                    max_tokens=500
                )
                content = response.choices[0].message.content if response.choices else ""
                if not content:
                    content = getattr(response.choices[0].message, "reasoning_content", "") if response.choices else ""
                if content:
                    return {"status": "success", "message": f"{request.provider} API 连接正常"}
                else:
                    return {"status": "error", "message": "API 返回空响应"}
            except Exception as api_error:
                print(f"[LLM Test] API调用失败: {api_error}")
                raise api_error

        return {"status": "error", "message": "API 返回异常，请检查配置"}

    except Exception as e:
        error_msg = str(e)
        low = error_msg.lower()
        # 优先尝试从 OpenAI SDK 异常里抽取干净的服务端 message
        clean = None
        try:
            body = getattr(e, 'body', None) or {}
            if isinstance(body, dict):
                clean = (body.get('error') or {}).get('message')
        except Exception:
            clean = None
        if not clean:
            import re as _re
            m = _re.search(r"'message':\s*'([^']+)'", error_msg)
            if m:
                clean = m.group(1)
        clean = (clean or error_msg)[:200]
        # 简化常见错误信息
        if "额度" in error_msg or "quota" in low or "balance" in low or "insufficient" in low:
            return {"status": "error", "message": f"账户额度不足：{clean}"}
        if "authentication" in low or "auth" in low or "api key" in low:
            return {"status": "error", "message": "API Key 无效或已过期"}
        if "rate limit" in low or "too many requests" in low:
            return {"status": "error", "message": "API 调用频率超限，请稍后重试"}
        if "model" in low and ("not found" in low or "does not exist" in low):
            return {"status": "error", "message": "模型名称不存在，请检查模型配置"}
        if "connection" in low or "timeout" in low or "timed out" in low or "unreachable" in low:
            return {"status": "error", "message": "网络连接失败或超时，请检查 Base URL"}
        return {"status": "error", "message": f"测试失败: {clean}"}




# ==================== 操作引导相关API ====================

from services.guide_generator import GuideGenerator, GuideRequest
from services.environment_detector import detect_environment, get_detector

# 引导生成器实例
guide_generator = GuideGenerator(llm_service)

class GuideGenerateRequest(BaseModel):
    """引导生成请求"""
    issue_key: str
    issue_summary: str
    issue_description: str = ""
    tenant_info: Dict[str, Any] = {}
    screenshots: List[str] = []  # Base64编码的图片列表
    user_question: str = ""


@app.get("/api/index/status")
async def get_index_status(project_key: str):
    """查询某项目的 Chroma 历史工单索引进度（供前端轮询）"""
    from services.project_index_service import get_project_index_service
    return get_project_index_service().get_status(project_key)


@app.get("/guide.html")
def read_guide_page():
    """操作引导页面"""
    return FileResponse(os.path.join(FRONTEND_DIR, "guide.html"))

@app.post("/api/guide/generate")
def generate_guide(request: GuideGenerateRequest):
    """
    生成操作引导

    根据工单信息和截图，生成实时操作指引

    请求格式:
    {
        "issue_key": "MYPROJECT-59031",
        "issue_summary": "如何添加审批节点",
        "issue_description": "详细描述...",
        "tenant_info": {
            "deployment_type": "private",
            "system_version": "3.5.2"
        },
        "screenshots": ["base64编码的图片..."],
        "user_question": "用户的具体问题"
    }

    返回格式:
    {
        "request_id": "guide_20260301...",
        "status": "success",
        "env_info": {...},
        "matched_scenario": "flow-add-approval",
        "scenario_confidence": 0.85,
        "steps": [...],
        "annotated_images": [...],
        "text_guide": "## 操作指南..."
    }
    """
    try:
        # 构建引导请求
        guide_request = GuideRequest(
            issue_key=request.issue_key,
            issue_summary=request.issue_summary,
            issue_description=request.issue_description,
            tenant_info=request.tenant_info,
            screenshots=[],  # 将base64字符串转换为bytes
            user_question=request.user_question
        )

        # 处理截图
        import base64
        screenshot_bytes = []
        for img_base64 in request.screenshots:
            try:
                img_data = base64.b64decode(img_base64)
                screenshot_bytes.append(img_data)
            except Exception as e:
                print(f"解析截图失败: {e}")

        guide_request.screenshots = screenshot_bytes

        # 生成引导
        result = guide_generator.generate(guide_request)

        return result.to_dict()

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "request_id": "",
            "status": "failed",
            "error_message": str(e)
        }

@app.get("/api/guide/scenarios")
def list_guide_scenarios():
    """
    获取可用的引导场景列表

    返回所有预设的操作场景模板
    """
    import os as os_module
    import json as json_module

    scenarios_path = os_module.path.join(
        os_module.path.dirname(os_module.path.abspath(__file__)),
        "..", "..", "..", "data", "guide_templates", "scenarios.json"
    )

    try:
        if os_module.path.exists(scenarios_path):
            with open(scenarios_path, 'r', encoding='utf-8') as f:
                data = json_module.load(f)
            return {
                "status": "success",
                "scenarios": data.get("templates", []),
                "categories": data.get("categories", [])
            }
        else:
            return {
                "status": "error",
                "message": "场景模板文件不存在"
            }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

@app.get("/api/guide/ui-rules/versions")
def list_ui_rules_versions():
    """
    获取可用的UI规则版本列表

    用于版本适配管理
    """
    from services.version_adapter import get_version_adapter

    adapter = get_version_adapter()
    versions = adapter.list_available_versions()

    result = []
    for version in versions:
        modules = adapter.list_modules(version)
        result.append({
            "version": version,
            "modules": modules
        })

    return {
        "status": "success",
        "versions": result
    }

@app.get("/api/guide/ui-rules/{version}/{module}")
def get_ui_rules(version: str, module: str):
    """
    获取特定版本和模块的UI规则

    参数:
    - version: 版本key (latest, v3.2, fallback)
    - module: 模块名 (flow_designer, form_builder, permission)
    """
    from services.version_adapter import get_version_adapter

    adapter = get_version_adapter()
    rules = adapter.get_module_rules(module, version)

    if rules:
        return {
            "status": "success",
            "version": version,
            "module": module,
            "rules": rules
        }
    else:
        return {
            "status": "error",
            "message": f"未找到版本 {version} 的模块 {module} 的规则"
        }

@app.post("/api/guide/detect-environment")
def detect_environment_api(tenant_info: Dict[str, Any]):
    """
    环境检测API

    根据租户信息检测环境类型

    请求格式:
    {
        "deployment_type": "public|dedicated|private",
        "system_version": "3.5.2"
    }

    返回格式:
    {
        "env_type": "public_cloud|dedicated_cloud|private_cloud",
        "version": "...",
        "access_method": "agent|plugin|screenshot",
        "ui_rules_version": "latest|v3.2|fallback",
        "strategy": {...}
    }
    """
    env_info = detect_environment(tenant_info)
    detector = get_detector()
    strategy = detector.get_strategy_for_environment(env_info)

    return {
        "status": "success",
        "env_info": env_info.to_dict(),
        "strategy": strategy
    }

# ============== PM 协作任务看板初始化 ==============

# 注册PM路由（guard：pm_router 可能因依赖缺失而为 None）
if _pm_router_available and pm_router is not None:
    app.include_router(pm_router)

# 注册记忆管理路由 (v4.0, 需要 mem0 依赖)
if _memory_router_available:
    app.include_router(memory_router)

# 注册飞书互动路由 (v4.0)
if _feishu_router_available:
    app.include_router(feishu_router)

# 注册定时调度路由 (v4.0)
if _scheduler_router_available:
    app.include_router(scheduler_router)

# 注册多用户通道管理路由 (v4.0)
if _channel_router_available:
    app.include_router(channel_router)

# 注册智能体调度路由
if _agents_router_available:
    app.include_router(agents_router)
    app.include_router(agents_user_router)

try:
    from api.reply_module_router import router as reply_module_router
    app.include_router(reply_module_router)
except Exception as _rme:
    import logging as _rml
    _rml.getLogger(__name__).warning(f"reply_module_router 加载失败: {_rme}")

try:
    from api.hook_bridge_router import router as hook_bridge_router
    app.include_router(hook_bridge_router)
except Exception as _hbe:
    import logging as _hbl
    _hbl.getLogger(__name__).warning(f"hook_bridge_router 加载失败: {_hbe}")
# RUN_BACKGROUND_JOBS=1 时才在本进程内启调度器/自动化轮询。
_RUN_BG = os.environ.get("RUN_BACKGROUND_JOBS") == "1"

# 启动PM调度器（如果启用，且在后台任务进程中）
ENABLE_PM_SCHEDULER = os.environ.get("ENABLE_PM_SCHEDULER", "true").lower() == "true"
if _RUN_BG and ENABLE_PM_SCHEDULER and _pm_scheduler_available and start_pm_scheduler is not None:
    try:
        pm_scheduler = start_pm_scheduler(
            sync_interval=5,
            process_interval=10,
            overdue_interval=60,
        )
        print("[PM] 协作任务看板调度器已启动")
    except Exception as e:
        print(f"[PM] 调度器启动失败: {e}")


# 启动自动化规则定时轮询（每 10 分钟）
import threading

def _automation_poll_loop():
    """后台线程：定时执行所有 enabled 自动化规则"""
    import time as _time
    INTERVAL = 600  # 10 分钟
    _time.sleep(30)  # 启动后等 30s 再开始首次轮询
    while True:
        try:
            result = board_service.run_all_enabled_rules()
            if result.get("ran", 0) > 0:
                print(f"[AutomationPoll] 完成: {result['ran']}规则, 执行{result.get('total_executed',0)}条")
        except Exception as e:
            print(f"[AutomationPoll] 轮询异常: {e}")
        _time.sleep(INTERVAL)

_automation_thread = threading.Thread(target=_automation_poll_loop, daemon=True, name="automation-poll")
_automation_thread.start()
print("[Automation] 自动化规则轮询已启动 (间隔 10 分钟)")

# 启动 v4.0 定时调度器（compact 版：单进程，不依赖 jobmaster daemon，直接随服务启动）
ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "true").lower() == "true"
if ENABLE_SCHEDULER:
    try:
        from services.scheduler_service import start_scheduler, register_task_handler

        # 注册通用 script 类型处理器（供 hourly-adopted-facts-consume 等 task_type:script 调度使用）
        import sys as _sys
        import subprocess as _subprocess

        def _task_run_script(_schedule_command: list = None, _schedule_id: str = "", **kwargs):
            """通用 script handler：执行 schedule.command 列表，__PYTHON__ 替换为当前解释器。"""
            cmd = _schedule_command or kwargs.get("command") or []
            cmd = [_sys.executable if c == "__PYTHON__" else c for c in cmd]
            if not cmd:
                raise ValueError(f"script schedule '{_schedule_id}' 无 command 字段")
            r = _subprocess.run(
                cmd,
                cwd=str(Path(__file__).resolve().parent),
                capture_output=True,
                text=True,
                timeout=900,
            )
            if r.returncode != 0:
                raise RuntimeError(f"script '{_schedule_id}' 失败 (rc={r.returncode}): {r.stderr[:300]}")
            return {"stdout": r.stdout[-500:]}

        register_task_handler("script", _task_run_script)
        print("[Scheduler] script handler 已注册")

        # nightly_exploration handler 已删除（exploration_agent.py 已移除，dead code）

        def _task_reply_training(questions: int = 300, stop_hour: int = 7,
                                 pull_qcl: bool = True, run_backfill: bool = False,
                                 backfill_limit: int = 50, **kwargs):
            """夜间 04:00 回复优化训练 + 差异分析 backfill（最多到 stop_hour:00 停止）"""
            import subprocess
            from services.local_llm_lifecycle import with_fallback, shutdown_if_started_by_us
            from services.feishu_notifier import get_notifier
            notifier = get_notifier()
            provider = with_fallback("reply_training")
            _script = str(Path(__file__).parent / "scripts" / "reply_optimization_trainer.py")
            _proj_root = str(Path(__file__).parent)
            _cmd = [sys.executable, _script, "--questions", str(questions), "--stop-hour", str(stop_hour)]
            _env = {**os.environ, "LLM_PROVIDER_OVERRIDE": provider}
            try:
                result = subprocess.run(
                    _cmd, capture_output=True, text=True,
                    timeout=4 * 3600, cwd=_proj_root, env=_env,
                )
                if result.returncode == 0:
                    logger.info("[Scheduler] Reply training done")
                    notifier.send_message(f"✅ 夜间回复训练完成（{questions}题）")
                else:
                    logger.error(f"[Scheduler] Reply training failed: {result.stderr[:300]}")
                    notifier.send_message(f"❌ 夜间回复训练失败\n{result.stderr[:300]}")
            except Exception as exc:
                logger.error(f"[Scheduler] Reply training exception: {exc}")
                notifier.send_message(f"❌ 夜间回复训练异常: {exc}")
            finally:
                shutdown_if_started_by_us("reply_training")

            if run_backfill:
                try:
                    _fb_input = str(Path(__file__).parent / "data" / "reply_trainer" / "feedback_log.jsonl")
                    _diff_script = str(Path(__file__).parent / "reply_diff_analyzer.py")
                    _bcmd = [sys.executable, _diff_script, "--backfill", "--input", _fb_input,
                             "--limit", str(backfill_limit)]
                    subprocess.run(_bcmd, timeout=3600, cwd=_proj_root, env=_env)
                    logger.info("[Scheduler] Diff analyzer backfill done")
                except Exception as exc:
                    logger.warning(f"[Scheduler] Diff backfill failed (non-critical): {exc}")

        register_task_handler("reply_training", _task_reply_training)


        def _task_daily_summary(**kwargs):
            from datetime import date as _date
            try:
                from agents.daily_summary_agent import DailySummaryAgent
                result = DailySummaryAgent().run_task(payload=kwargs, trigger_src="schedule:daily")
                logger.info(f"[Scheduler] daily_summary done: {result}")
            except Exception as exc:
                logger.exception(f"[Scheduler] daily_summary failed: {exc}")
                try:
                    from services.feishu_notifier import get_notifier as _gn
                    _gn().send_message(f"❌ 日报生成失败 {_date.today()}\n{exc}\n请在 agents.html 检查")
                except Exception:
                    pass

        def _task_daily_summary_watchdog(**kwargs):
            from datetime import date as _date, timedelta as _td
            from pathlib import Path as _Path
            yesterday = _date.today() - _td(days=1)
            archive = _Path(__file__).parent.parent.parent / "conclusion" / "daily_reports" / f"{yesterday}.md"
            if not archive.exists():
                logger.warning("[Scheduler] watchdog: archive missing, re-running daily_summary")
                _task_daily_summary(date=str(yesterday))
                return
            try:
                from services.agent_task_store import AgentTaskStore as _ATS
                import json as _json
                store = _ATS()
                failed = [
                    t for t in store.list_recent(agent_name="daily_summary", limit=20)
                    if t.status.value in ("awaiting_human_review",)
                    and _json.loads(t.payload_json or "{}").get("kind") == "daily_report_failed"
                    and str(yesterday) in (t.payload_json or "")
                ]
                if failed:
                    from services.feishu_notifier import get_notifier as _gn
                    md = archive.read_text(encoding="utf-8")
                    if _gn().send_message(md):
                        for t in failed:
                            store.update_status(t.id, "succeeded")
                        logger.info("[Scheduler] watchdog: re-send succeeded")
                    else:
                        logger.error("[Scheduler] watchdog: re-send also failed")
            except Exception as exc:
                logger.error(f"[Scheduler] watchdog error: {exc}")

        register_task_handler("daily_summary", _task_daily_summary)
        register_task_handler("daily_summary_watchdog", _task_daily_summary_watchdog)

        def _task_vacation_schedule_cleanup(purpose: str = "vacation_window_2026_05", **kwargs):
            """假期结束后自动 disable 所有临时调度（防止污染正常工作日）。"""
            import glob as _glob
            _schedules_dir = Path(__file__).parent / "data" / "schedules"
            disabled = []
            for p in _schedules_dir.glob("*.json"):
                try:
                    cfg = json.loads(p.read_text())
                    if cfg.get("purpose") == purpose and cfg.get("enabled", False):
                        cfg["enabled"] = False
                        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
                        disabled.append(cfg.get("id", p.stem))
                except Exception:
                    continue
            msg = f"✅ 假期调度已清理：{', '.join(disabled) or '无'}" if disabled else "ℹ️ 无需清理假期调度"
            logger.info(f"[Scheduler] vacation cleanup: {msg}")
            try:
                from services.feishu_notifier import get_notifier as _gn
                _gn().send_message(msg)
            except Exception:
                pass

        register_task_handler("vacation_schedule_cleanup", _task_vacation_schedule_cleanup)

        # 注册任务处理器：KB 增量 sync（每 30 分钟）
        def _task_kb_refresh_incremental(**kwargs):
            """每 30 分钟：增量 sync KB（build + sync），仅处理变更文件"""
            from services.feishu_notifier import get_notifier
            notifier = get_notifier()
            try:
                result = kb_runtime_service.sync()
                logger.info(
                    f"[Scheduler] KB incremental sync done: chunks={result.get('chunk_count')}, "
                    f"local_manifest={result.get('local_manifest_count')}"
                )
            except Exception as e:
                logger.error(f"[Scheduler] KB incremental sync failed: {e}")
                try:
                    notifier.send_message(
                        f"⚠️ KB 增量 sync 失败\n"
                        f"原因：{str(e)[:300]}\n"
                        f"任务：kb_refresh_incremental"
                    )
                except Exception:
                    pass

        register_task_handler("kb_refresh_incremental", _task_kb_refresh_incremental)

        # 注册任务处理器：KB 全量 sync + compile（每日凌晨 2:00）
        def _task_kb_refresh_full(**kwargs):
            """每日凌晨 2:00：全量 force_refresh sync + compile_all，防 KB 漂移"""
            from services.feishu_notifier import get_notifier
            notifier = get_notifier()
            try:
                sync_result = kb_runtime_service.sync(force_refresh=True)
                logger.info(
                    f"[Scheduler] KB full sync done: chunks={sync_result.get('chunk_count')}, "
                    f"local_manifest={sync_result.get('local_manifest_count')}"
                )
            except Exception as e:
                logger.error(f"[Scheduler] KB full sync failed: {e}")
                try:
                    notifier.send_message(
                        f"⚠️ KB 全量 sync 失败\n"
                        f"原因：{str(e)[:300]}\n"
                        f"任务：kb_refresh_full"
                    )
                except Exception:
                    pass
                return  # sync 失败不继续 compile
            try:
                from kb_compile_service import get_compile_service
                compile_svc = get_compile_service()
                if compile_svc is not None:
                    compiled = compile_svc.compile_all()
                    logger.info(f"[Scheduler] KB full compile done: {len(compiled)} topics compiled")
                else:
                    logger.warning("[Scheduler] KB full compile skipped: compile_service not initialized")
            except Exception as e:
                logger.error(f"[Scheduler] KB full compile failed: {e}")
                try:
                    notifier.send_message(
                        f"⚠️ KB 全量 compile 失败\n"
                        f"原因：{str(e)[:300]}\n"
                        f"任务：kb_refresh_full"
                    )
                except Exception:
                    pass

        register_task_handler("kb_refresh_full", _task_kb_refresh_full)

        start_scheduler()
        print("[Scheduler] v4.0 定时调度服务已启动")
    except Exception as e:
        print(f"[Scheduler] 启动失败: {e}")

# 始终注册 session_harvester，确保 /api/schedules/.../trigger 可用（daemon 拥有真实执行权，主进程提供 API 触发入口）
try:
    from services.scheduler_service import register_task_handler as _reg_handler2, _task_handlers as _th2
    if "session_harvester" not in _th2:
        def _session_harvester_fallback(dry_run: bool = True, **kwargs):
            try:
                from agents.session_harvester_agent import SessionHarvesterAgent
                result = SessionHarvesterAgent().execute(dry_run=dry_run)
                logger.info(f"[task:session_harvester] done: {result}")
            except Exception as _exc:
                logger.exception(f"[task:session_harvester] failed: {_exc}")
        _reg_handler2("session_harvester", _session_harvester_fallback)
        print("[Scheduler] 已注册 session_harvester 处理器（fallback）")
except Exception as _e:
    print(f"[Scheduler] session_harvester fallback 注册失败: {_e}")

# ── JIRA Session 自动刷新（每 30 分钟，Chrome 解密优先，REST fallback）────────
def _auto_refresh_jira_session():
    """初始化 JiraSessionRefresher 单例并启动后台周期刷新（30 分钟）。
    macOS: Chrome Keychain 解密 → web UI 可用。Linux/fallback: REST JSESSIONID（仅 REST API）。"""
    try:
        from services.jira_session_refresher import JiraSessionRefresher
        cfg = jira_svc.config_parser
        refresher = JiraSessionRefresher.get_instance()
        refresher.configure(
            jira_base_url=jira_svc.base_url,
            ssl_verify=jira_svc.ssl_verify,
            proxies=getattr(jira_svc, "proxies", None) or {},
            username=cfg.username,
            password=cfg.password,
        )
        refresher.start_background(interval_sec=1800)
        print("[JiraAutoRefresh] 已注册 JiraSessionRefresher，每 30 分钟刷新")
    except Exception as e:
        print(f"[JiraAutoRefresh] 初始化失败: {e}")

# ── 注册 Agents ─────────────────────────────────────────────────────────────
try:
    from agents.registry import validate_agent_identities as _validate_identities
    _validate_identities(strict=False)   # warn-only；改 strict=True 可阻断启动
except Exception as _vi_err:
    print(f"[IdentitySchema] 校验异常: {_vi_err}")

try:
    from agents.registry import AgentRegistry as _AgentRegistry
    _reg = _AgentRegistry.get_instance()

    # ReplyAgent
    try:
        from agents.reply_agent import ReplyAgent
        _reg.register(ReplyAgent(board_service))
        print("[AgentRegistry] ReplyAgent 注册成功")
    except Exception as _ae:
        print(f"[AgentRegistry] register ReplyAgent failed: {_ae}")

    # AdoptedAgent
    try:
        from agents.adopted_agent import AdoptedAgent
        _reg.register(AdoptedAgent())
        print("[AgentRegistry] AdoptedAgent 注册成功")
    except Exception as _ae:
        print(f"[AgentRegistry] register AdoptedAgent failed: {_ae}")

    # HandoverSuggestAgent
    try:
        from agents.handover_suggest_agent import HandoverSuggestAgent
        _reg.register(HandoverSuggestAgent(board_service))
        print("[AgentRegistry] HandoverSuggestAgent 注册成功")
    except Exception as _ae:
        print(f"[AgentRegistry] register HandoverSuggestAgent failed: {_ae}")

    # ReplySupervisorAgent
    try:
        import importlib as _il_rs
        _rs_mod = _il_rs.import_module("agents.reply_supervisor_agent")
        _reg.register(_rs_mod.ReplySupervisorAgent(board_service))
        print("[AgentRegistry] ReplySupervisorAgent 注册成功")
    except Exception as _ae:
        print(f"[AgentRegistry] register ReplySupervisorAgent failed: {_ae}")

    # DailySummaryAgent
    try:
        import importlib as _il_ds
        _ds_mod = _il_ds.import_module("agents.daily_summary_agent")
        _reg.register(_ds_mod.DailySummaryAgent())
        print("[AgentRegistry] DailySummaryAgent 注册成功")
    except Exception as _ae:
        print(f"[AgentRegistry] register DailySummaryAgent failed: {_ae}")

    # OMC subagent bridge — 扫描 ~/.claude/plugins/cache 并注册固定角色
    try:
        from agents.omc_bridge import register_all as _omc_register_all
        _omc_count = _omc_register_all(_reg)
        print(f"[AgentRegistry] OMC subagents 注册完成: {_omc_count} 个")
    except Exception as _ae:
        print(f"[AgentRegistry] OMC bridge 注册失败（非致命）: {_ae}")

    print(f"[AgentRegistry] 已注册 {len(_reg.list())} 个 Agent")
except Exception as _e:
    print(f"[AgentRegistry] 启动注册失败: {_e}")

_auto_refresh_jira_session()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000)

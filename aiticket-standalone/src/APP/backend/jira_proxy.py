#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jira 代理服务 (主机一端)

功能：
- 接收来自 QCL 的代理请求
- 调用 jira_service 获取 Jira 数据
- 返回统一格式响应

部署位置: 主机一 (通过 VPN 连接内网 Jira)
启动方式: python jira_proxy.py --port 5001
"""

import os
import subprocess as _subp
import sys
import json
import time
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, request, jsonify, Response

# 添加项目根目录到路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from jira_service import JiraService

# 配置日志
def setup_logging(config: Dict[str, Any]) -> logging.Logger:
    """设置日志配置"""
    log_level = getattr(logging, config.get('level', 'INFO').upper())
    log_file = config.get('file', 'logs/jira_proxy.log')
    max_bytes = config.get('max_bytes', 10 * 1024 * 1024)
    backup_count = config.get('backup_count', 5)

    # 确保日志目录存在
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # 配置根日志记录器
    logger = logging.getLogger('jira_proxy')
    logger.setLevel(log_level)

    # 文件处理器
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(file_format)
    logger.addHandler(console_handler)

    return logger

# 导入 logging.handlers
import logging.handlers

# 初始化应用
app = Flask(__name__)

# 加载配置
def load_config(config_path: str = 'config/network_config_host1.yaml') -> Dict[str, Any]:
    """加载配置文件"""
    import yaml

    # 尝试多个路径
    paths = [
        config_path,
        os.path.join(BASE_DIR, config_path),
        os.path.join(BASE_DIR, 'config', 'network_config_host1.yaml'),
    ]

    for path in paths:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)

    # 返回默认配置
    logger.warning(f"配置文件未找到，使用默认配置: {config_path}")
    return {
        'proxy_server': {'host': '0.0.0.0', 'port': 5001, 'debug': False},
        'logging': {'level': 'INFO', 'file': 'logs/jira_proxy.log'},
        'security': {'rate_limit': {'enabled': True, 'requests_per_minute': 60}},
        'jira': {'timeout': 30}
    }

# 加载配置 (这里使用简单的字典，避免 yaml 依赖问题)
# 实际使用时可以安装 PyYAML: pip install pyyaml
config = {
    'proxy_server': {
        'host': '0.0.0.0',
        'port': 5001,
        'debug': False
    },
    'logging': {
        'level': 'INFO',
        'file': 'logs/jira_proxy.log'
    },
    'security': {
        'rate_limit': {
            'enabled': True,
            'requests_per_minute': 60
        }
    },
    'jira': {
        'timeout': 30
    }
}

# 尝试从环境变量加载配置
if os.getenv('PROXY_PORT'):
    config['proxy_server']['port'] = int(os.getenv('PROXY_PORT'))
if os.getenv('PROXY_DEBUG'):
    config['proxy_server']['debug'] = os.getenv('PROXY_DEBUG').lower() == 'true'
if os.getenv('LOG_LEVEL'):
    config['logging']['level'] = os.getenv('LOG_LEVEL')

# 设置日志
logger = setup_logging(config['logging'])

# 初始化 Jira 服务
logger.info("正在初始化 Jira 服务...")
jira_service = JiraService()
logger.info("Jira 服务初始化完成")

# 请求计数器 (用于限流)
request_counts = {}

# 速率限制装饰器
def rate_limit(max_requests: int, period: int = 60):
    """速率限制装饰器"""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not config['security']['rate_limit']['enabled']:
                return f(*args, **kwargs)

            client_ip = request.remote_addr
            current_time = time.time()

            # 清理过期记录
            for ip in list(request_counts.keys()):
                if current_time - request_counts[ip]['first_time'] > period:
                    del request_counts[ip]

            # 检查速率
            if client_ip not in request_counts:
                request_counts[client_ip] = {'count': 1, 'first_time': current_time}
            else:
                request_counts[client_ip]['count'] += 1
                if request_counts[client_ip]['count'] > max_requests:
                    logger.warning(f"速率限制触发: IP={client_ip}, 请求次数={request_counts[client_ip]['count']}")
                    return jsonify({
                        'status': 'error',
                        'code': 'RATE_LIMIT_EXCEEDED',
                        'message': f'请求过于频繁，请稍后再试。限制: {max_requests}次/{period}秒'
                    }), 429

            return f(*args, **kwargs)
        return wrapped
    return decorator

# 统一响应格式
def success_response(data: Any = None, message: str = "成功") -> Dict[str, Any]:
    """成功响应"""
    return {
        'status': 'success',
        'code': '0',
        'message': message,
        'data': data,
        'timestamp': datetime.now().isoformat(),
        'node': 'mini'
    }

def error_response(code: str, message: str, details: Any = None) -> Dict[str, Any]:
    """错误响应"""
    return {
        'status': 'error',
        'code': code,
        'message': message,
        'details': details,
        'timestamp': datetime.now().isoformat(),
        'node': 'mini'
    }

# API 路由

@app.route('/proxy/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    return jsonify(success_response({
        'service': 'jira_proxy',
        'version': '1.0.0',
        'status': 'healthy',
        'uptime': time.time()
    }))

@app.route('/proxy/jira/fields', methods=['GET'])
@rate_limit(max_requests=30)
def get_fields():
    """获取 Jira 字段列表"""
    try:
        logger.info("获取 Jira 字段列表")
        fields = jira_service.get_fields()

        if 'error' in fields:
            logger.error(f"获取字段失败: {fields['error']}")
            return jsonify(error_response(
                'JIRA_API_ERROR',
                '获取 Jira 字段失败',
                fields['error']
            )), 500

        # 简化返回数据，只保留必要字段
        simplified_fields = [
            {
                'id': f['id'],
                'name': f['name'],
                'custom': f.get('custom', False),
                'schema': f.get('schema', {})
            }
            for f in fields
        ]

        logger.info(f"成功获取 {len(simplified_fields)} 个字段")
        return jsonify(success_response(simplified_fields))

    except Exception as e:
        logger.exception(f"获取字段异常: {e}")
        return jsonify(error_response(
            'INTERNAL_ERROR',
            f'服务器内部错误: {str(e)}'
        )), 500

@app.route('/proxy/jira/search', methods=['GET'])
@rate_limit(max_requests=60)
def search_issues():
    """搜索 Jira 工单"""
    try:
        # 获取请求参数
        jql = request.args.get('jql', '')
        start_at = int(request.args.get('startAt', '0'))
        max_results = int(request.args.get('maxResults', '50'))

        if not jql:
            return jsonify(error_response(
                'INVALID_REQUEST',
                '缺少 jql 参数'
            )), 400

        logger.info(f"搜索工单: JQL={jql}, startAt={start_at}, maxResults={max_results}")

        # 调用 Jira 服务
        result = jira_service.search_issues_rest_api(jql, start_at, max_results)

        if 'error' in result:
            logger.error(f"搜索工单失败: {result['error']}")
            return jsonify(error_response(
                'JIRA_API_ERROR',
                '搜索工单失败',
                result['error']
            )), 500

        logger.info(f"成功搜索 {len(result.get('issues', []))} 个工单")
        return jsonify(success_response(result))

    except ValueError as e:
        logger.warning(f"参数验证失败: {e}")
        return jsonify(error_response(
            'INVALID_REQUEST',
            f'参数错误: {str(e)}'
        )), 400
    except Exception as e:
        logger.exception(f"搜索工单异常: {e}")
        return jsonify(error_response(
            'INTERNAL_ERROR',
            f'服务器内部错误: {str(e)}'
        )), 500

@app.route('/proxy/jira/issue/<issue_key>', methods=['GET'])
@rate_limit(max_requests=60)
def get_issue(issue_key: str):
    """获取单个工单详情（含附件、评论、变更历史）"""
    try:
        logger.info(f"获取工单详情: {issue_key}")

        # 直接调用 Jira REST API，请求附件+评论+变更历史
        url = f"{jira_service.base_url}/rest/api/2/issue/{issue_key}"
        params = {"expand": "changelog", "fields": "summary,description,attachment,comment,customfield_13529"}
        resp = requests.get(
            url,
            headers=jira_service.headers,
            params=params,
            verify=jira_service.ssl_verify,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return jsonify(error_response(
                'ISSUE_NOT_FOUND',
                f'工单不存在: {issue_key}'
            )), 404

        logger.info(f"成功获取工单: {issue_key}")
        return jsonify(success_response(data))

    except Exception as e:
        logger.exception(f"获取工单异常: {e}")
        return jsonify(error_response(
            'INTERNAL_ERROR',
            f'服务器内部错误: {str(e)}'
        )), 500


def _load_browser_session_cookies(user_id: Optional[str] = None) -> dict:
    """从 Playwright storageState JSON 加载浏览器会话 cookies。

    查找顺序（按优先级）：
      1. /tmp/jira-session-{user_id}.json （指定用户的 per-user session）
      2. /tmp/jira-session.json            （全局回退 session）
    失败返回空字典。
    """
    import json as _json
    import re as _re

    from services.host_context import session_path as _session_path
    paths = []
    if user_id:
        paths.append(_session_path(user=user_id, prefix="jira"))
    # strict 模式不允许回落到全局 session（qiangxiao），仅用 per-user session
    from role_guard import is_strict_role
    if not is_strict_role():
        paths.append(_session_path())

    for state_path in paths:
        if not os.path.exists(state_path):
            continue
        try:
            with open(state_path) as f:
                state = _json.load(f)
            cookies = {}
            for c in state.get("cookies", []):
                _jira_host = __import__('urllib.parse', fromlist=['urlparse']).urlparse(
                    __import__('os').environ.get("JIRA_BASE_URL", "")).hostname or ""
                if _jira_host and _jira_host in c.get("domain", ""):
                    cookies[c["name"]] = c["value"]
            if cookies:
                return cookies
        except Exception as e:
            logger.warning(f"读取browser session失败 path={state_path}: {e}")
            continue
    return {}


def _run_refresh_script() -> tuple[bool, str]:
    """调用 refresh_jira_session.sh 从本机 Chrome 解密真实浏览器 cookies。
    返回 (success, message)。"""
    script = os.path.join(os.path.dirname(__file__), 'scripts', 'refresh_jira_session.sh')
    if not os.path.exists(script):
        return False, f'refresh_jira_session.sh 不存在: {script}'
    try:
        r = _subp.run(['bash', script], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, f'脚本返回非零: {r.stderr[-500:]}'
        return True, r.stdout[-300:] if r.stdout else 'ok'
    except _subp.TimeoutExpired:
        return False, '刷新脚本超时 30s'
    except Exception as e:
        return False, str(e)


@app.route('/proxy/jira/session/refresh', methods=['POST'])
@rate_limit(max_requests=10)
def refresh_session_chrome():
    """在 lap 本机调用 refresh_jira_session.sh，从 Chrome 解密真实浏览器 cookies，
    写入 /tmp/jira-session.json。成功后所有后续附件请求自动命中新 cookie。"""
    ok, msg = _run_refresh_script()
    if not ok:
        return jsonify(error_response('SHELL_ERR', f'刷新失败: {msg}')), 500
    cookies = _load_browser_session_cookies()
    if not cookies:
        return jsonify(error_response('NO_COOKIES', '脚本执行成功但未产出 cookies')), 500
    try:
        probe = requests.get(
            f"{jira_service.base_url}/rest/auth/1/session",
            cookies=cookies,
            headers={"Accept": "application/json"},
            verify=jira_service.ssl_verify,
            timeout=5,
        )
        if probe.status_code == 200 and probe.json().get("name"):
            return jsonify({"status": "success",
                            "user": probe.json().get("name"),
                            "cookies_count": len(cookies)})
        if probe.status_code in (401, 403):
            return jsonify(error_response('SESSION_EXPIRED',
                'Chrome 中的 Jira session 已失效（Jira 服务端不认）。'
                '请在本机 Chrome 访问一次 Jira 页面（让 Chrome 刷新 JSESSIONID）后再重试')), 500
        return jsonify(error_response('PROBE_FAIL',
            f'cookie 验证请求返回意外状态: HTTP {probe.status_code}')), 500
    except Exception as e:
        return jsonify(error_response('PROBE_ERR', str(e))), 500


@app.route('/proxy/jira/session/cookies', methods=['GET'])
@rate_limit(max_requests=30)
def get_session_state():
    """返回本机 session 完整 state，供远程主机镜像（解决多机 session 不同步问题）。"""
    import json as _json
    from services.host_context import session_path as _session_path
    state_path = _session_path()
    if not os.path.exists(state_path):
        return jsonify(error_response('NOT_FOUND', '未找到 session 文件，请先调用 /proxy/jira/session/refresh')), 404
    try:
        with open(state_path) as f:
            state = _json.load(f)
        if not state.get("cookies"):
            return jsonify(error_response('EMPTY', 'session 文件中无 cookies')), 404
        return jsonify({"status": "success", "state": state})
    except Exception as e:
        return jsonify(error_response('READ_ERR', str(e))), 500


@app.route('/proxy/jira/attachment/<attachment_id>', methods=['GET'])
@rate_limit(max_requests=120)
def proxy_attachment_binary(attachment_id: str):
    """代理附件二进制下载（解决 QCL 无法直连 Jira 内网的问题）。

    认证流程：
    1. 元数据用 Basic Auth + config cookies（REST API 支持）
    2. 二进制下载需要浏览器 session cookie（/secure/attachment/ 路径）：
       优先从 /tmp/jira-session.json 读取 Playwright storageState cookies，
       否则回退到 jira_service.cookies（通常过期）
    """
    import mimetypes
    from urllib.parse import quote
    try:
        logger.info(f"代理附件下载: {attachment_id}")

        # 1. 获取附件元数据（REST API，Basic Auth 即可）
        meta_url = f"{jira_service.base_url}/rest/api/2/attachment/{attachment_id}"
        meta_resp = requests.get(
            meta_url,
            headers=jira_service.headers,
            cookies=jira_service.cookies,
            verify=jira_service.ssl_verify,
            timeout=10,
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()

        content_url = meta.get("content", "")
        mime_type = meta.get("mimeType", "application/octet-stream")
        filename = meta.get("filename", f"attachment_{attachment_id}")

        if not content_url:
            return jsonify(error_response('NOT_FOUND', '附件URL为空')), 404

        # 2. 根据扩展名修正 MIME
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        mime_overrides = {
            'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'gif': 'image/gif', 'svg': 'image/svg+xml', 'webp': 'image/webp',
            'pdf': 'application/pdf',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'txt': 'text/plain', 'csv': 'text/csv', 'json': 'application/json',
        }
        actual_mime = mime_overrides.get(ext) or mimetypes.guess_type(filename)[0] or mime_type

        # 3. 级联 cookies：优先 browser session file → config cookies
        download_cookies = _load_browser_session_cookies()
        if not download_cookies:
            download_cookies = dict(jira_service.cookies or {})
            logger.warning("session 文件不存在，回退使用 config cookies（可能过期）")

        # 4. 流式下载
        dl_resp = requests.get(
            content_url,
            headers=jira_service.headers,
            cookies=download_cookies,
            verify=jira_service.ssl_verify,
            stream=True,
            timeout=30,
            allow_redirects=True,
        )
        dl_resp.raise_for_status()

        # 5. 校验实际返回是否为 HTML 认证页（Jira 过期时会返回200+HTML）
        # 检测到 HTML → 先尝试自动刷新 + 重试一次，再放弃
        ct = dl_resp.headers.get("content-type", "")
        if "text/html" in ct and ext not in ("html", "htm"):
            logger.warning(f"附件下载返回HTML认证页，尝试自动刷新后重试: {attachment_id}")
            dl_resp.close()
            ok, msg = _run_refresh_script()
            if ok:
                new_cookies = _load_browser_session_cookies()
                if new_cookies:
                    dl_resp = requests.get(
                        content_url,
                        headers=jira_service.headers,
                        cookies=new_cookies,
                        verify=jira_service.ssl_verify,
                        stream=True,
                        timeout=30,
                        allow_redirects=True,
                    )
                    ct = dl_resp.headers.get("content-type", "")
                    if "text/html" in ct and ext not in ("html", "htm"):
                        logger.error(f"自动刷新后仍为HTML，放弃: {attachment_id}")
                        return jsonify(error_response(
                            'AUTH_EXPIRED',
                            'Jira session 已失效，自动刷新无效。请确认 lap Chrome 保持 Jira 登录状态'
                        )), 401
                    logger.info(f"自动刷新后重试成功: {attachment_id}")
                else:
                    return jsonify(error_response('REFRESH_FAILED', '自动刷新脚本执行后未产出 cookies')), 401
            else:
                return jsonify(error_response('REFRESH_ERROR', f'自动刷新失败: {msg}')), 401

        encoded_filename = quote(filename)
        return Response(
            dl_resp.iter_content(chunk_size=8192),
            content_type=actual_mime,
            headers={
                'Content-Disposition': f"inline; filename*=UTF-8''{encoded_filename}",
                'Cache-Control': 'private, max-age=3600',
            },
        )

    except Exception as e:
        logger.exception(f"附件代理异常: {e}")
        return jsonify(error_response('INTERNAL_ERROR', f'附件下载失败: {str(e)}')), 500

@app.route('/proxy/jira/assign', methods=['POST'])
@rate_limit(max_requests=20)
def assign_issue():
    """分配工单"""
    try:
        data = request.get_json()
        issue_key = data.get('issue_key')
        assignee = data.get('assignee')
        comment = data.get('comment')

        if not issue_key or not assignee:
            return jsonify(error_response(
                'INVALID_REQUEST',
                '缺少 issue_key 或 assignee 参数'
            )), 400

        logger.info(f"分配工单: {issue_key} -> {assignee}")

        result = jira_service.assign_issue(issue_key, assignee, comment=comment)

        if result.get('success'):
            try:
                from services.operation_event_log import log_event as _log_event
                _log_event('transfer', issue_key, 'unknown',
                           to_assignee=assignee, comment=comment, source='ui_modal')
            except Exception:
                pass
            return jsonify(success_response({
                'issue_key': issue_key,
                'assignee': assignee
            }, '分配成功'))
        else:
            return jsonify(error_response(
                'OPERATION_FAILED',
                result.get('message', '分配失败')
            )), 500

    except Exception as e:
        logger.exception(f"分配工单异常: {e}")
        return jsonify(error_response(
            'INTERNAL_ERROR',
            f'服务器内部错误: {str(e)}'
        )), 500

@app.route('/proxy/jira/comment', methods=['POST'])
@rate_limit(max_requests=20)
def add_comment():
    """添加评论"""
    try:
        data = request.get_json()
        issue_key = data.get('issue_key')
        comment = data.get('comment')
        close = data.get('close', False)

        if not issue_key or not comment:
            return jsonify(error_response(
                'INVALID_REQUEST',
                '缺少 issue_key 或 comment 参数'
            )), 400

        logger.info(f"添加评论: {issue_key}, close={close}")

        result = jira_service.reply_issue(issue_key, comment, close=close)

        if result.get('success'):
            return jsonify(success_response({
                'issue_key': issue_key,
                'closed': close
            }, '操作成功'))
        else:
            return jsonify(error_response(
                'OPERATION_FAILED',
                result.get('message', '操作失败')
            )), 500

    except Exception as e:
        logger.exception(f"添加评论异常: {e}")
        return jsonify(error_response(
            'INTERNAL_ERROR',
            f'服务器内部错误: {str(e)}'
        )), 500

@app.route('/proxy/jira/field-options', methods=['POST'])
@rate_limit(max_requests=30)
def get_field_options():
    """获取字段选项"""
    try:
        data = request.get_json()
        issue_id = data.get('issue_id')
        field_ids = data.get('field_ids', [])

        if not issue_id or not field_ids:
            return jsonify(error_response(
                'INVALID_REQUEST',
                '缺少 issue_id 或 field_ids 参数'
            )), 400

        logger.info(f"获取字段选项: {issue_id}, fields={field_ids}")

        result = jira_service.get_field_options(issue_id, field_ids)

        return jsonify(success_response(result))

    except Exception as e:
        logger.exception(f"获取字段选项异常: {e}")
        return jsonify(error_response(
            'INTERNAL_ERROR',
            f'服务器内部错误: {str(e)}'
        )), 500

@app.route('/proxy/metrics', methods=['GET'])
def get_metrics():
    """获取服务指标"""
    return jsonify(success_response({
        'node': 'mini',
        'uptime': time.time(),
        'request_counts': request_counts,
        'jira_connected': jira_service.base_url is not None
    }))

# 透明代理路由：将标准 JIRA REST API 请求原样转发到真实 JIRA 服务器
# 供 QCL backend 的 JiraService（JIRA_BASE_URL=http://127.0.0.1:5001）使用

@app.route('/rest/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/secure/<path:subpath>', methods=['GET', 'POST'])
def transparent_proxy(subpath):
    """透明代理：将 /rest/* 和 /secure/* 请求转发到真实 JIRA 服务器

    会话隔离关键：优先使用请求方 (QCL 用户) 带来的 JSESSIONID，
    只有当请求方完全没带 Jira cookies 时才回退到 Mini 本地 (qiangxiao) 的 session。
    这样 QCL 上每个用户用自己的 session，不会串到 Mini 主人的 Jira 账号。
    """
    target_url = f"{jira_service.base_url}/{request.path.lstrip('/')}"
    try:
        # 构建转发头：使用 jira_service 认证头（UA/Referer），但保留原始请求的 Content-Type（multipart 等）
        forward_headers = {k: v for k, v in jira_service.headers.items() if k.lower() != 'content-type'}
        if 'Content-Type' in request.headers:
            forward_headers['Content-Type'] = request.headers['Content-Type']
        # 透传附件上传必需的 X-Atlassian-Token
        if 'X-Atlassian-Token' in request.headers:
            forward_headers['X-Atlassian-Token'] = request.headers['X-Atlassian-Token']

        # ─ 会话选择：请求方 cookies 优先 ─
        # QCL 的 JiraService 发请求时会带上用户自己的 JSESSIONID
        incoming = {k: v for k, v in request.cookies.items()
                    if k in ('JSESSIONID', 'atlassian.xsrf.token', 'tenant_info')}
        if incoming.get('JSESSIONID'):
            forward_cookies = incoming
            # 关键：用户带了自己的 session → 必须去掉 Authorization header
            # 否则 Basic Auth (Mini 主人 qiangxiao 的) 会兜底，session 无效时串用户
            forward_headers.pop('Authorization', None)
            logger.info(f"[session-iso] {request.path} → USER session (JSESSIONID={incoming['JSESSIONID'][:8]}...), Authorization removed")
        else:
            # 请求方未带 session（如 Mini 本地 fallback），回退到 jira_service 本地 cookies
            from role_guard import is_strict_role
            if is_strict_role():
                return jsonify(error_response('NO_USER_SESSION', '匿名 Jira 代理在 strict 模式下被拒绝')), 401
            forward_cookies = jira_service.cookies
            logger.info(f"[session-iso] {request.path} → FALLBACK to Mini local (incoming cookies: {list(request.cookies.keys())})")

        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            cookies=forward_cookies,
            params=request.args,
            data=request.get_data(),
            verify=jira_service.ssl_verify,
            timeout=60,
            stream=True,
        )
        excluded_headers = {'content-encoding', 'transfer-encoding', 'connection'}
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded_headers}
        logger.debug(f"透明代理 {request.method} {request.path} -> {resp.status_code}")
        return Response(resp.content, status=resp.status_code, headers=headers)
    except Exception as e:
        logger.warning(f"透明代理失败 {request.path}: {e}")
        return jsonify(error_response('PROXY_ERROR', f'代理请求失败: {str(e)}')), 502


@app.route('/pmf_token_get', methods=['GET'])
def pm_token_get():
    """返回本机最新 PM token（供 QCL 通过 frpc 隧道同步）。
    仅在 localhost 可访问，通过 frpc TCP 隧道对 QCL 暴露，安全可接受。
    """
    from pathlib import Path
    import json as _json
    pm_token_path = Path(__file__).parent / 'data_cache' / 'pm_token.json'
    if not pm_token_path.exists():
        return jsonify(error_response('PM_TOKEN_NOT_FOUND', 'PM token 文件不存在，请先在 Mini 运行 refresh_pm_token.sh')), 404
    try:
        with open(pm_token_path) as f:
            token_data = _json.load(f)
        if not token_data.get('yht_access_token'):
            return jsonify(error_response('PM_TOKEN_EMPTY', 'PM token 为空')), 404
        return jsonify({'status': 'success', 'data': token_data})
    except Exception as e:
        return jsonify(error_response('PM_TOKEN_READ_ERROR', str(e))), 500


def _load_pm_cookies(username=None):
    """加载用户 PM cookies；用户指定但未绑定时返回 (空dict, '__not_bound__')。"""
    from pathlib import Path as _Path
    import json as _json
    base = _Path(__file__).parent / 'data_cache'

    if username:
        wallet_path = base / 'pm_tokens' / f'{username}.json'
        if wallet_path.is_file():
            try:
                data = _json.loads(wallet_path.read_text(encoding='utf-8'))
                from datetime import datetime
                expires = data.get('expires_at', '')
                if expires and datetime.fromisoformat(expires) < datetime.now():
                    logger.info(f"[pm_wallet] {username} token expired, removing")
                    wallet_path.unlink(missing_ok=True)
                else:
                    cookies = {
                        'yht_access_token': data.get('yht_access_token', ''),
                        'tenant_info': data.get('tenant_info', '0000'),
                    }
                    cookies.update(data.get('extra_cookies', {}))
                    logger.debug(f"[pm_forward] 使用 {username} 的钱包 token")
                    return cookies, username
            except Exception as e:
                logger.warning(f"[pm_wallet] {username} 钱包读取失败: {e}")
        # User specified but wallet missing or expired — do NOT fall back to admin
        return {}, '__not_bound__'

    return {}, '__none__'


@app.route('/pmf_wallet_save', methods=['POST'])
def pm_wallet_save():
    """QCL 通过隧道将用户钱包数据同步到 Mini（钱包文件必须在 Mini 本地）。"""
    from pathlib import Path as _Path
    import json as _json
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    if not username:
        return jsonify(error_response('MISSING_USERNAME', 'username 必填')), 400
    token_data = data.get('token_data', {})
    if not token_data.get('yht_access_token'):
        return jsonify(error_response('MISSING_TOKEN', 'yht_access_token 必填')), 400
    wallet_dir = _Path(__file__).parent / 'data_cache' / 'pm_tokens'
    wallet_dir.mkdir(parents=True, exist_ok=True)
    path = wallet_dir / f'{username}.json'
    path.write_text(_json.dumps(token_data, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        path.chmod(0o600)
    except Exception:
        pass
    logger.info(f"[pm_wallet] saved wallet for {username} via /pmf_wallet_save")
    return jsonify({'status': 'success', 'message': f'wallet saved for {username}'})


@app.route('/pmf_forward/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def pm_forward(subpath):
    """透明代理：将 /pmf_forward/* 请求转发到 pm.example.com/*

    Cookie 策略（M4 多用户钱包路由）：
    - 读取 X-PM-User 头 → 查找 data_cache/pm_tokens/{user}.json
    - 用户钱包不存在或过期 → 降级到 data_cache/pm_token.json（默认管理账号）
    - Origin/Referer 固定为 https://pm.example.com
    """
    target_url = f"https://pm.example.com/{subpath}"
    try:
        pm_user = request.headers.get('X-PM-User', '').strip() or None
        forward_cookies, source = _load_pm_cookies(pm_user)
        if pm_user and source == '__not_bound__':
            return jsonify(error_response('PM_NOT_BOUND', 'PM session 未绑定，请先上传 PM session')), 401
        if not forward_cookies:
            forward_cookies = dict(request.cookies)
            source = '__passthrough__'

        skip_headers = {'host', 'content-length', 'transfer-encoding', 'connection',
                        'origin', 'referer', 'cookie', 'x-pm-user'}
        forward_headers = {k: v for k, v in request.headers if k.lower() not in skip_headers}
        forward_headers['Origin'] = 'https://pm.example.com'
        forward_headers['Referer'] = 'https://pm.example.com/'

        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            cookies=forward_cookies,
            params=request.args,
            data=request.get_data(),
            verify=False,
            timeout=30,
        )
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        logger.debug(f"[pm_forward] {request.method} /pmf_forward/{subpath} user={pm_user} source={source} -> {resp.status_code}")
        return Response(resp.content, status=resp.status_code, headers=resp_headers)
    except Exception as e:
        logger.error(f"[pm_forward] 转发失败: {e}")
        return jsonify(error_response('PM_PROXY_ERROR', f'PM 代理请求失败: {str(e)}')), 502


# 错误处理

@app.errorhandler(404)
def not_found(error):
    return jsonify(error_response('NOT_FOUND', '接口不存在')), 404

@app.errorhandler(500)
def internal_error(error):
    logger.exception(f"内部错误: {error}")
    return jsonify(error_response('INTERNAL_ERROR', '服务器内部错误')), 500

def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='Jira 代理服务')
    parser.add_argument('--host', default=config['proxy_server']['host'],
                        help='绑定地址')
    parser.add_argument('--port', type=int, default=config['proxy_server']['port'],
                        help='绑定端口')
    parser.add_argument('--debug', action='store_true',
                        help='启用调试模式')

    args = parser.parse_args()

    # 更新配置
    if args.debug:
        config['proxy_server']['debug'] = True
        logger.setLevel(logging.DEBUG)

    logger.info(f"启动 Jira 代理服务: {args.host}:{args.port}")
    logger.info(f"Jira 服务器: {jira_service.base_url}")

    # 启动服务
    app.run(
        host=args.host,
        port=args.port,
        debug=config['proxy_server']['debug'],
        threaded=True
    )

if __name__ == '__main__':
    main()

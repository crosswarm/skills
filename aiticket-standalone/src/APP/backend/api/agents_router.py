"""
agents_router — /api/agents/* REST + SSE

端点：
  GET  /api/agents                     — 所有 Agent 状态卡片
  GET  /api/agents/{name}              — 单 Agent 详情
  GET  /api/agents/tasks               — 近 48h 任务列表
  GET  /api/agents/tasks/{id}          — 任务详情（含 log_tail / children）
  POST /api/agents/tasks/{id}/cancel   — 取消任务
  POST /api/agents/{name}/trigger      — 手动触发 Agent
  GET  /api/agents/stream              — SSE 实时推送
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agents.base import AgentStatus, AgentTask
from agents.registry import AgentRegistry
from auth_deps import require_admin_user, require_authenticated_user
from services.agent_task_store import AgentTaskStore
from services.host_context import HOST, PEER_BRIDGE_URL, is_mini, is_qcl, proxy_to_peer
from services.jobmaster_service import get_schedule_summary, get_schedules

router = APIRouter(prefix="/api/agents", tags=["agents"],
                   dependencies=[Depends(require_admin_user)])

_PLIST = Path.home() / "Library/LaunchAgents/com.aiticket.supergemma4.plist"
_LOCAL_MODEL_PORT = 8090
_TRANSITION_FILE = Path(__file__).resolve().parent.parent / "data" / "local_model_transition.json"
_TRANSITION_TTL_SEC = 120

def _gui_target() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else 501  # 501 = macOS default first-user UID
    return f"gui/{uid}/com.aiticket.supergemma4"

def _set_transition(action: str) -> None:
    _TRANSITION_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TRANSITION_FILE.write_text(
        json.dumps({"action": action, "started_at": time.time()}),
        encoding="utf-8",
    )

def _get_transition() -> Optional[dict]:
    if not _TRANSITION_FILE.exists():
        return None
    try:
        d = json.loads(_TRANSITION_FILE.read_text(encoding="utf-8"))
        elapsed = time.time() - float(d.get("started_at", 0))
        if elapsed > _TRANSITION_TTL_SEC:
            _TRANSITION_FILE.unlink(missing_ok=True)
            return None
        d["elapsed_seconds"] = int(elapsed)
        return d
    except Exception:
        return None

def _clear_transition() -> None:
    try:
        _TRANSITION_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def _resolve_transition_against_state(is_online: bool) -> Optional[dict]:
    """Auto-clears transition marker when reality matches expectation."""
    t = _get_transition()
    if not t:
        return None
    action = t.get("action")
    if (action == "start" and is_online) or (action == "stop" and not is_online):
        _clear_transition()
        return None
    return t

# SSE 广播队列（轻量级 in-process pub/sub）
_sse_subscribers: list[asyncio.Queue] = []


def _broadcast(event: str, data: dict) -> None:
    msg = {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


# ── 工具函数 ────────────────────────────────────────────────────────

def _check_peer_online() -> bool:
    """快速检查对端 bridge 是否可达（HEAD /api/board/stats，超时 2s）。"""
    if not PEER_BRIDGE_URL:
        return False
    try:
        import requests as _req
        r = _req.head(f"{PEER_BRIDGE_URL.rstrip('/')}/api/board/stats", timeout=2)
        return r.status_code < 500
    except Exception:
        return False


def _task_to_dict(t: AgentTask, include_detail: bool = False) -> dict:
    d = t.to_dict()
    if not include_detail:
        # 保留 log_snippet（前 80 字符）供卡片副标题显示
        log_tail = d.pop("log_tail", None) or ""
        d["log_snippet"] = log_tail[:80].strip() if log_tail else ""
        d.pop("payload_json", None)
        d.pop("result_json", None)
    return d


# ── GET /api/agents ──────────────────────────────────────────────────

@router.get("")
def list_agents(request: Request, include_hidden: bool = False):
    from services.agent_task_store import AgentTaskStore
    reg = AgentRegistry.get_instance()
    agents = reg.list()
    if not include_hidden:
        agents = [a for a in agents if not getattr(a, "hidden", False)]
    user_id = getattr(request.state, "current_user", {}) or {}
    uid = user_id.get("id") if isinstance(user_id, dict) else None
    # Batch-fetch today stats + running tasks + schedules once instead of N× per-agent
    store = AgentTaskStore.get_instance()
    try:
        from services.jobmaster_service import get_schedules as _get_schedules
        _schedules = _get_schedules()
    except Exception:
        _schedules = []
    try:
        from auth_service import get_auth_service as _gas
        _nicknames = _gas().get_user_nicknames(uid) if uid else {}
    except Exception:
        _nicknames = {}
    prefetch = {
        "stats": store.today_stats_all(),
        "running": store.all_running_task_ids(),
        "schedules": _schedules,
        "user_nicknames": _nicknames,
    }
    return [reg.build_agent_summary(a.name, user_id=uid, prefetch=prefetch) for a in agents]


# ── GET /api/agents/schedules ─────────────────────────────────────────  (jobmaster endpoints removed)


@router.get("/schedules")
def list_schedules():
    return get_schedule_summary()


# ── GET /api/agents/schedules/{id}/tasks ─────────────────────────────

@router.get("/schedules/{schedule_id}/tasks")
def schedule_tasks(
    schedule_id: str,
    limit: int = Query(30, ge=1, le=100),
    days: int = Query(30, ge=1, le=365),
):
    store = AgentTaskStore.get_instance()
    tasks = store.list_by_schedule(schedule_id, limit=limit, days=days)
    return [_task_to_dict(t) for t in tasks]


# ── GET /api/agents/tasks ────────────────────────────────────────────

def _add_host_badge(tasks: list, host_label: str) -> list:
    for t in tasks:
        t["host"] = host_label
    return tasks


@router.get("/tasks")
def list_tasks(
    agent: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    since: Optional[str] = Query(None),
    host: Optional[str] = Query(None),  # mini | qcl | all
):
    target = (host or HOST).lower()

    if target == "all":
        store = AgentTaskStore.get_instance()
        local = _add_host_badge(
            [_task_to_dict(t) for t in store.list_recent(agent_name=agent, status=status, limit=limit, since=since)],
            HOST,
        )
        try:
            peer_tasks = proxy_to_peer(
                f"/api/agents/tasks?limit={limit}" + (f"&agent={agent}" if agent else "") + (f"&status={status}" if status else ""),
                method="GET",
            )
            peer_label = "mini" if is_qcl() else "qcl"
            peer_tasks = _add_host_badge(peer_tasks if isinstance(peer_tasks, list) else [], peer_label)
        except HTTPException:
            peer_tasks = []
        merged = sorted(local + peer_tasks, key=lambda x: x.get("started_at") or "", reverse=True)
        return merged[:limit]

    if target != HOST:
        try:
            peer = proxy_to_peer(
                f"/api/agents/tasks?limit={limit}" + (f"&agent={agent}" if agent else "") + (f"&status={status}" if status else ""),
                method="GET",
            )
            peer_label = target
            return _add_host_badge(peer if isinstance(peer, list) else [], peer_label)
        except HTTPException as exc:
            raise exc

    store = AgentTaskStore.get_instance()
    if agent or status:
        # 有过滤条件时走精确查询
        tasks = store.list_recent(agent_name=agent, status=status, limit=limit, since=since)
    else:
        # 无过滤时用 diverse 查询，每 agent 最多 15 条，防止 claude CLI 独占视图
        tasks = store.list_recent_diverse(limit=limit * 2, since=since, limit_per_agent=15)
    return _add_host_badge([_task_to_dict(t) for t in tasks], HOST)


# ── GET /api/agents/tasks/{id} ───────────────────────────────────────

@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    store = AgentTaskStore.get_instance()
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    d = _task_to_dict(task, include_detail=True)
    def _child_dict(c):
        cd = _task_to_dict(c, include_detail=True)
        cd.pop("log_tail", None)
        cd.pop("payload_json", None)
        return cd
    d["children"] = [_child_dict(c) for c in store.list_children(task_id)]
    d["messages"] = store.get_messages(task_id)
    return d


# ── POST /api/agents/tasks/{id}/cancel ──────────────────────────────

@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    store = AgentTaskStore.get_instance()
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    ok = store.cancel(task_id)
    if not ok:
        raise HTTPException(409, f"task already terminal: {task.status.value}")
    updated = store.get(task_id)
    _broadcast("task_updated", _task_to_dict(updated))
    return {"ok": True}


# ── POST /api/agents/tasks/{id}/approve ─────────────────────────────

class ApproveBody(BaseModel):
    comment: str = ""

@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: str, body: ApproveBody = ApproveBody()):
    store = AgentTaskStore.get_instance()
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    terminal = {AgentStatus.SUCCEEDED, AgentStatus.FAILED, AgentStatus.CANCELLED}
    if task.status in terminal:
        raise HTTPException(409, f"task already terminal: {task.status.value}")
    store.update_status(task_id, AgentStatus.RUNNING)
    comment = body.comment or "已批准，等待执行"
    store.append_message(task_id, "system", comment)
    updated = store.get(task_id)
    _broadcast("task_updated", _task_to_dict(updated))

    # 若 agent 覆盖了 run_task，后台线程中派工（team_dispatch 协调任务除外）
    try:
        from agents.registry import AgentRegistry
        import json as _json
        _src = task.trigger_src or ""
        _is_team_dispatch = _src.startswith("team_dispatch:") or _src.startswith("hook:")
        reg = AgentRegistry.get_instance()
        agent = reg.get(task.agent_name)
        BaseAgent = __import__("agents.base", fromlist=["BaseAgent"]).BaseAgent
        if agent and not _is_team_dispatch and type(agent).run_task is not BaseAgent.run_task:
            import threading
            from datetime import datetime as _dt
            _tid = task_id

            def _exec():
                try:
                    result = agent.run_task(store.get(_tid))
                    store.update_status(
                        _tid, AgentStatus.SUCCEEDED,
                        finished_at=_dt.utcnow(),
                        result_json=_json.dumps(result or {}, ensure_ascii=False, default=str),
                        progress=100,
                    )
                except Exception as exc:
                    store.update_status(
                        _tid, AgentStatus.FAILED,
                        finished_at=_dt.utcnow(),
                        result_json=_json.dumps({"error": str(exc)}, ensure_ascii=False),
                    )
                    store.append_log(_tid, f"ERROR: {exc}")
                finally:
                    _broadcast("task_updated", _task_to_dict(store.get(_tid)))

            threading.Thread(target=_exec, daemon=True).start()
    except Exception:
        pass

    return {"ok": True, "status": AgentStatus.RUNNING.value, "agent": task.agent_name}


# ── POST /api/agents/tasks/{id}/reject ──────────────────────────────

class RejectBody(BaseModel):
    reason: str = ""

@router.post("/tasks/{task_id}/reject")
def reject_task(task_id: str, body: RejectBody = RejectBody()):
    store = AgentTaskStore.get_instance()
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    terminal = {AgentStatus.SUCCEEDED, AgentStatus.FAILED, AgentStatus.CANCELLED}
    if task.status in terminal:
        raise HTTPException(409, f"task already terminal: {task.status.value}")
    store.update_status(task_id, AgentStatus.CANCELLED)
    if body.reason:
        store.append_message(task_id, "system", f"已驳回：{body.reason}")
    updated = store.get(task_id)
    _broadcast("task_updated", _task_to_dict(updated))
    return {"ok": True, "status": AgentStatus.CANCELLED.value}


# ── GET /api/agents/tasks/{id}/messages ─────────────────────────────

@router.get("/tasks/{task_id}/messages")
def get_task_messages(task_id: str, since: str = None):
    store = AgentTaskStore.get_instance()
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    msgs = store.get_messages(task_id)
    if since:
        msgs = [m for m in msgs if m.get("ts", "") > since]
    return {"messages": msgs, "task_id": task_id, "status": task.status.value}


# ── POST /api/agents/tasks/{id}/chat ────────────────────────────────

class ChatBody(BaseModel):
    text: str = ""
    intent: str = "comment"  # comment | adjust | confirm | reject

@router.post("/tasks/{task_id}/chat")
def chat_task(task_id: str, body: ChatBody):
    store = AgentTaskStore.get_instance()
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"task not found: {task_id}")
    txt = (body.text or "").strip()
    intent = body.intent or "comment"

    terminal = {AgentStatus.SUCCEEDED, AgentStatus.FAILED, AgentStatus.CANCELLED}

    # intent shortcuts
    if intent == "confirm":
        if task.status not in terminal:
            store.update_status(task_id, AgentStatus.RUNNING)
            store.append_message(task_id, "system", f"已确认{('：' + txt) if txt else ''}")
        _broadcast("task_updated", _task_to_dict(store.get(task_id)))
        return {"messages": store.get_messages(task_id)}

    if intent == "reject":
        if task.status not in terminal:
            store.update_status(task_id, AgentStatus.CANCELLED)
            store.append_message(task_id, "system", f"已驳回{('：' + txt) if txt else ''}")
        _broadcast("task_updated", _task_to_dict(store.get(task_id)))
        return {"messages": store.get_messages(task_id)}

    if not txt:
        raise HTTPException(400, "text is empty")

    store.append_message(task_id, "user", txt)

    if intent == "adjust" and task.status not in terminal:
        # 记录到 payload_json.adjustments[]
        try:
            import json as _json
            payload = _json.loads(task.payload_json or "{}")
            adjustments = payload.get("adjustments", [])
            adjustments.append({"text": txt, "ts": datetime.utcnow().isoformat()})
            payload["adjustments"] = adjustments
            with store._write_lock, store._connect() as conn:
                conn.execute(
                    "UPDATE agent_tasks SET payload_json=? WHERE id=?",
                    (_json.dumps(payload, ensure_ascii=False), task_id),
                )
        except Exception:
            pass
        store.append_message(task_id, "system", "调整指令已记录，等待 agent 处理")
    elif task.status in terminal:
        store.append_message(task_id, "agent", f"任务已结束（{task.status.value}），消息已记录。")

    msgs = store.get_messages(task_id)
    _broadcast("task_updated", {"id": task_id, "messages_updated": True})
    return {"messages": msgs}


# ── POST /api/agents/alert (broadcast system_alert to all SSE clients) ──

class AlertBody(BaseModel):
    title: str
    body: str = ""
    level: str = "warning"   # "warning" | "critical" | "info"
    kind: str = "system_alert"

@router.post("/alert", status_code=200)
def broadcast_alert(req: AlertBody):
    """供 JobMaster / 后端内部调用：向所有在线页面推送 system_alert SSE 事件
    同时在 agent_tasks 里写一行便于 agents.html 追溯。"""
    _broadcast("system_alert", {
        "kind": req.kind,
        "title": req.title,
        "body": req.body,
        "level": req.level,
    })
    return {"ok": True, "subscribers": len(_sse_subscribers)}


# ── GET /api/agents/stream (SSE) — must be registered before /{name} ──

@router.get("/stream")
async def sse_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_subscribers.append(queue)

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = queue.get_nowait()
                    yield {"event": msg["event"], "data": msg["data"]}
                except asyncio.QueueEmpty:
                    yield {"event": "ping", "data": "{}"}
                    await asyncio.sleep(20)
        finally:
            try:
                _sse_subscribers.remove(queue)
            except ValueError:
                pass

    return EventSourceResponse(
        event_gen(),
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── GET /api/agents/registry ────────────────────────────────────────

@router.get("/registry")
async def get_agents_registry(request: Request):
    """返回已注册 agent 列表，供 Pipeline Drawer agent 下拉使用。"""
    from agents.registry import AgentRegistry
    registry = AgentRegistry.get_instance()
    result = []
    for agent in registry.list():
        entry = {
            "name": agent.name,
            "description": getattr(agent, "description", "") or "",
            "hidden": getattr(agent, "hidden", False),
        }
        try:
            from agents.identity_schema import load_identity
            identity = load_identity(agent.name)
            if identity:
                entry["default_nickname"] = identity.default_nickname
                entry["llm_feature_key"] = identity.llm_feature_key
        except Exception:
            pass
        result.append(entry)
    return {"agents": result}


# ── GET /api/agents/{name} ───────────────────────────────────────────

@router.get("/{name}")
def get_agent(name: str, request: Request):
    reg = AgentRegistry.get_instance()
    agent = reg.get(name)
    if not agent:
        raise HTTPException(404, f"agent not found: {name}")
    user_id = getattr(request.state, "current_user", {}) or {}
    uid = user_id.get("id") if isinstance(user_id, dict) else None
    summary = reg.build_agent_summary(name, user_id=uid)
    store = AgentTaskStore.get_instance()
    summary["recent_tasks"] = [
        _task_to_dict(t) for t in store.list_recent(agent_name=name, limit=20)
    ]
    return summary


# ── POST /api/agents/{name}/trigger ─────────────────────────────────

class TriggerBody(BaseModel):
    title: Optional[str] = None
    payload: Optional[dict] = None
    force: bool = False


@router.post("/{name}/trigger", status_code=202)
def trigger_agent(name: str, body: TriggerBody = TriggerBody()):
    reg = AgentRegistry.get_instance()
    agent = reg.get(name)
    if not agent:
        raise HTTPException(404, f"agent not found: {name}")

    # OMC subagent：必须通过父 agent authorize_subagent()，不允许直接 dispatch
    try:
        from agents.identity_schema import load_identity
        identity = load_identity(name)
        if identity and identity.kind == "omc_subagent":
            parent_name = identity.parent_agent
            parent = reg.get(parent_name)
            if not parent:
                raise HTTPException(503, detail={
                    "detail": f"父 agent '{parent_name}' 未注册，无法授权 {name}",
                    "status": "rejected",
                })
            from agents.parent_mixin import ParentAgentMixin
            if not isinstance(parent, ParentAgentMixin):
                raise HTTPException(503, detail={
                    "detail": f"父 agent '{parent_name}' 未实现授权协议",
                    "status": "rejected",
                })
            result = parent.authorize_subagent(
                child_name=name,
                payload={"title": body.title, **(body.payload or {})},
                requester="api:manual",
            )
            _broadcast("task_created", {"type": "omc_authorized", "child": name, "parent": parent_name})
            return result
    except HTTPException:
        raise
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(f"[trigger] identity check failed for {name}: {_e}")

    store = AgentTaskStore.get_instance()
    running_id = store.get_running_task_id(name)
    if running_id and not body.force:
        raise HTTPException(409, detail={
            "detail": "agent busy",
            "running_task_id": running_id,
        })

    import json as _json
    task = AgentTask.new(
        agent_name=name,
        title=body.title or f"手动触发 {agent.display_name}",
        trigger_src="api:manual",
        payload_json=_json.dumps(body.payload or {}, ensure_ascii=False),
    )
    store.insert(task)
    _broadcast("task_created", _task_to_dict(task))

    # 若 Agent 覆盖了 run_task，后台线程中执行主体逻辑
    if type(agent).run_task is not __import__("agents.base", fromlist=["BaseAgent"]).BaseAgent.run_task:
        import threading
        from datetime import datetime as _dt
        _tid = task.id

        def _exec():
            store.update_status(_tid, AgentStatus.RUNNING, started_at=_dt.utcnow())
            _broadcast("task_updated", _task_to_dict(store.get(_tid)))
            try:
                result = agent.run_task(store.get(_tid))
                store.update_status(
                    _tid, AgentStatus.SUCCEEDED,
                    finished_at=_dt.utcnow(),
                    result_json=_json.dumps(result or {}, ensure_ascii=False, default=str),
                    progress=100,
                )
            except Exception as exc:
                store.update_status(
                    _tid, AgentStatus.FAILED,
                    finished_at=_dt.utcnow(),
                    result_json=_json.dumps({"error": str(exc)}, ensure_ascii=False),
                )
                store.append_log(_tid, f"ERROR: {exc}")
            _broadcast("task_updated", _task_to_dict(store.get(_tid)))

        threading.Thread(target=_exec, daemon=True).start()

    return {"task_id": task.id}


# ─── Agent 记忆与配置治理端点 ────────────────────────────────────────────────

class _IdentityPatch(BaseModel):
    """PUT /{name}/identity 请求体 — 仅允许覆盖以下字段（内存级，重启丢失）"""
    personality: Optional[str] = None
    behavioral_guidelines: Optional[list] = None
    memory_write_trigger: Optional[str] = None


@router.get("/{name}/identity", summary="读取 agent L5 身份配置")
def get_agent_identity(name: str):
    """返回 agents/identity/{name}.yaml 内容（含运行时覆盖字段）。"""
    reg = AgentRegistry.get_instance()
    agent = reg.get(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' 不存在")
    identity = agent.get_identity()
    if not identity:
        raise HTTPException(status_code=404, detail=f"agents/identity/{name}.yaml 不存在")
    return {"agent": name, "identity": identity, "has_override": bool(getattr(agent, "_identity_override", {}))}


@router.get("/{name}/memory-summary", summary="读取 agent L2/L3 记忆健康度")
def get_agent_memory_summary(name: str):
    """返回 L2 近期任务数 + L3 private/shared 记忆数 + L5 校验状态。"""
    reg = AgentRegistry.get_instance()
    agent = reg.get(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' 不存在")
    try:
        summary = agent.memory_summary()
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/{name}/identity",
    summary="临时覆盖 agent L5 身份配置（内存级，重启丢失）",
    dependencies=[Depends(require_admin_user)],
)
def patch_agent_identity(name: str, patch: _IdentityPatch):
    """
    运行时临时调整 personality / behavioral_guidelines / memory_write_trigger。
    更改仅存于内存，重启后回到 YAML 原值。
    永久修改请直接编辑 agents/identity/{name}.yaml 并重启后端。
    """
    reg = AgentRegistry.get_instance()
    agent = reg.get(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' 不存在")

    override: dict = {}
    if patch.personality is not None:
        override["personality"] = patch.personality
    if patch.behavioral_guidelines is not None:
        override["behavioral_guidelines"] = patch.behavioral_guidelines
    if patch.memory_write_trigger is not None:
        override["memory_write_trigger"] = patch.memory_write_trigger

    if not override:
        raise HTTPException(status_code=422, detail="未提供任何可覆盖字段")

    agent._identity_override = {**getattr(agent, "_identity_override", {}), **override}

    import logging
    logging.getLogger(__name__).info(
        f"[IdentityPatch] {name} 覆盖字段: {list(override.keys())}"
    )
    return {
        "applied": True,
        "agent": name,
        "overridden_fields": list(override.keys()),
        "persistent": False,
        "expires_on_restart": True,
    }


@router.post(
    "/{name}/memory/clear",
    summary="清空 agent L3 私有记忆（admin，事故降级工具）",
    dependencies=[Depends(require_admin_user)],
)
def clear_agent_memory(name: str, scope: str = "private"):
    """
    清空指定 scope 记忆。scope=shared 需走 CLI 双确认，此端点拒绝 shared 清理。
    """
    if scope == "shared":
        raise HTTPException(
            status_code=403,
            detail="shared scope 清理需走 CLI: agent_memory_cli.py clear <agent> --scope shared --confirm-twice",
        )
    reg = AgentRegistry.get_instance()
    if not reg.get(name):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' 不存在")
    try:
        from services.memory_service import MemoryService
        removed = MemoryService.get_instance().clear_scope(f"agent:{name}")
        return {"agent": name, "scope": scope, "cleared": removed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 用户级 nickname 管理（所有登录用户可用）────────────────────────────

user_router = APIRouter(
    prefix="/api/agents",
    tags=["agents"],
    dependencies=[Depends(require_authenticated_user)],
)


class NicknameBody(BaseModel):
    nickname: str


@user_router.put("/{code}/nickname", summary="设置当前用户对某 agent 的昵称")
def set_nickname(code: str, body: NicknameBody, request: Request):
    reg = AgentRegistry.get_instance()
    if not reg.get(code):
        raise HTTPException(404, f"agent not found: {code}")
    user = getattr(request.state, "current_user", None) or {}
    uid = user.get("id") if isinstance(user, dict) else None
    if not uid:
        raise HTTPException(401, "未登录")
    try:
        from auth_service import get_auth_service
        get_auth_service().set_user_nickname(uid, code, body.nickname)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "code": code, "nickname": body.nickname}


@user_router.delete("/{code}/nickname", summary="恢复某 agent 默认昵称")
def delete_nickname(code: str, request: Request):
    reg = AgentRegistry.get_instance()
    if not reg.get(code):
        raise HTTPException(404, f"agent not found: {code}")
    user = getattr(request.state, "current_user", None) or {}
    uid = user.get("id") if isinstance(user, dict) else None
    if not uid:
        raise HTTPException(401, "未登录")
    from auth_service import get_auth_service
    get_auth_service().delete_user_nickname(uid, code)
    return {"ok": True, "code": code, "nickname": "restored_to_default"}


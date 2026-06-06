"""
AgentMemoryMixin — 五层记忆体系接入

L1 工作记忆:  task.log_tail / payload_json  (BaseAgent 基类 append_log/checkpoint 提供)
L2 情节记忆:  agent_tasks SQLite 近期成功任务摘要
L3 语义向量:  MemoryService / ChromaDB (公共 + 私有作用域)
L4 知识库:    KbHybridIndex — 各 agent 按需直接调用，不经此 mixin
L5 身份记忆:  agents/identity/{name}.yaml — 性格 + 工具链 + 行为准则

Hermes 借鉴增强（2026-04-27）:
  - prefetch(query)         异步预热 L3，下一轮 recall 走 cache，减少推理延迟
  - reflect_and_remember()  长任务结束前的 memory-flush（独立 LLM call，只暴露 remember 工具）
  - is_available() gate     recall 前检查 MemoryService，不可用时降级返回 []，不抛异常
  - _identity_override      PUT /api/agents/{name}/identity 内存覆盖支持（重启丢失）
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

IDENTITY_DIR = Path(__file__).resolve().parent / "identity"


class AgentMemoryMixin:
    """
    五层记忆体系 Mixin。

    使用方式:
        class MyAgent(AgentMemoryMixin, BaseAgent):
            name = "my_agent"
            ...

    注意: Mixin 方法均 fail-open — 依赖不可用时返回空值，不抛异常。

    内存覆盖:
        通过 PUT /api/agents/{name}/identity 设置的临时覆盖存放在
        self._identity_override，重启后丢失。get_identity() 自动合并。
    """

    # PUT /api/agents/{name}/identity 的内存覆盖（重启丢失）
    _identity_override: Dict[str, Any] = {}

    # prefetch 缓存（query → results），由 prefetch() 预热，recall() 优先读取
    _prefetch_cache: Dict[str, List[Dict]] = {}
    _prefetch_lock: threading.Lock = threading.Lock()

    # ─── L2: 情节记忆 ────────────────────────────────────────────────────────

    def recall_recent(self, n: int = 5) -> List[Dict]:
        """
        读取本 agent 最近 n 次成功任务摘要（L2 情节记忆）。
        返回: [{"id", "title", "result_json", "finished_at"}, ...]
        """
        try:
            from services.agent_task_store import AgentTaskStore
            return AgentTaskStore.get_instance().recent_succeeded(self.name, n)
        except Exception as e:
            logger.debug(f"[Memory-L2:{self.name}] recall_recent: {e}")
            return []

    # ─── L3: 语义向量记忆 ────────────────────────────────────────────────────

    def recall(
        self,
        query: str,
        scope: str = "private",
        min_confidence: float = 0.6,
    ) -> List[Dict]:
        """
        检索 L3 语义向量记忆。
        scope: "private" = f"agent:{self.name}", "shared" = 全局公共池

        Hermes 借鉴:
        - 调用前通过 is_available() 门控降级，ChromaDB 宕机时返回 [] 而非 500
        - 优先命中 prefetch_cache（由上一轮 prefetch() 预热）
        """
        try:
            from services.memory_service import MemoryService
            svc = MemoryService.get_instance()
            if not svc.is_available():
                logger.warning(f"[Memory-L3:{self.name}] MemoryService 不可用，降级返回 []")
                return []
            uid = "shared" if scope == "shared" else f"agent:{self.name}"
            cache_key = f"{uid}:{query}"
            with self._prefetch_lock:
                if cache_key in self._prefetch_cache:
                    cached = self._prefetch_cache.pop(cache_key)
                    logger.debug(f"[Memory-L3:{self.name}] prefetch cache hit: {query[:40]}")
                    return cached
            results = svc.get_context(uid, query)
            return [
                r for r in results
                if (r.get("metadata") or {}).get("confidence", 0) >= min_confidence
            ]
        except Exception as e:
            logger.debug(f"[Memory-L3:{self.name}] recall({scope}): {e}")
            return []

    def recall_both(self, query: str) -> List[Dict]:
        """同时检索 shared + private L3，按 confidence 降序去重合并"""
        shared  = self.recall(query, scope="shared")
        private = self.recall(query, scope="private")
        seen: set = set()
        merged = []
        for m in shared + private:
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                merged.append(m)
        return sorted(
            merged,
            key=lambda m: (m.get("metadata") or {}).get("confidence", 0),
            reverse=True,
        )

    def remember(
        self,
        content: str,
        source_id: str,
        scope: str = "private",
        extra_meta: Optional[Dict] = None,
    ) -> Optional[str]:
        """
        写入 L3 语义记忆（带防幻觉校验）。
        source_id: ticket_id（MYPROJECT-xxx）或 kb_id（KB-xxx / kb-xxx）
        scope: "private" | "shared"
        返回: mem_id 或 None（被过滤/写入失败）
        """
        try:
            from services.memory_service import MemoryService
            uid = "shared" if scope == "shared" else f"agent:{self.name}"
            meta: Dict[str, Any] = {"agent": self.name, **(extra_meta or {})}
            sid_lower = source_id.lower()
            if sid_lower.startswith("kb") or sid_lower.startswith("doc"):
                meta["source_kb_id"] = source_id
            else:
                meta["source_ticket_id"] = source_id
            return MemoryService.get_instance().add_learning(uid, content, metadata=meta)
        except Exception as e:
            logger.debug(f"[Memory-L3:{self.name}] remember: {e}")
            return None

    def memory_feedback(self, mem_id: str, adopted: bool) -> bool:
        """采纳/拒绝反馈，更新 L3 记忆置信度（+0.1 / -0.2）"""
        try:
            from services.memory_service import MemoryService
            delta = +0.1 if adopted else -0.2
            return MemoryService.get_instance().update_confidence(mem_id, delta)
        except Exception as e:
            logger.debug(f"[Memory-L3:{self.name}] feedback: {e}")
            return False

    def prefetch(self, query: str, scope: str = "private") -> None:
        """
        异步预热 L3 recall 缓存（借鉴 Hermes prefetch/queue_prefetch 对称 API）。
        在当前 LLM 调用返回后、下一轮开始前调用，下一轮 recall(query) 直接命中 cache。

        用法:
            results = self.recall_both(query)
            # … 处理 results，生成回复 …
            self.prefetch(next_query)   # 预热下一轮（异步，不阻塞）
        """
        def _warm():
            try:
                from services.memory_service import MemoryService
                svc = MemoryService.get_instance()
                if not svc.is_available():
                    return
                uid = "shared" if scope == "shared" else f"agent:{self.name}"
                results = svc.get_context(uid, query)
                cache_key = f"{uid}:{query}"
                with self._prefetch_lock:
                    self._prefetch_cache[cache_key] = results
                logger.debug(f"[Memory-L3:{self.name}] prefetch ok: {query[:40]}")
            except Exception as e:
                logger.debug(f"[Memory-L3:{self.name}] prefetch err: {e}")

        threading.Thread(target=_warm, daemon=True).start()

    def reflect_and_remember(
        self,
        task_summary: str,
        source_id: str,
        scope: str = "private",
    ) -> Optional[str]:
        """
        长任务结束前的 memory-flush（借鉴 Hermes pre-compression flush）。

        调用时机: run_task() 中步骤数 > 5 时，在 return result 前调用一次。
        行为: 调用 LLM，让其从 task_summary 中提炼最值得记住的一条事实，
              仅当 LLM 判断"有记忆价值"时才调用 self.remember()。
        失败安全: LLM 调用失败时仅 log，不影响主任务返回。

        用法:
            self.reflect_and_remember(
                task_summary=f"分析了需求 {req_id}，结论={conclusion}",
                source_id=req_id,
            )
        """
        try:
            import json
            from services.llm_caller import call_llm  # 项目内 LLM 调用工具
            prompt = (
                f"你是记忆整理助手。以下是一个 agent 刚完成的任务摘要：\n\n"
                f"{task_summary}\n\n"
                f"判断：这份摘要中是否有值得长期记忆的新事实（不是通用知识，必须有具体来源）？\n"
                f"如果有，用一句话提炼出来（≤100字）。\n"
                f"如果没有，回复 NULL。\n"
                f"只回复提炼的一句话或 NULL，不要其他内容。"
            )
            response = call_llm(prompt, max_tokens=150)
            if response and response.strip().upper() != "NULL":
                content = response.strip()
                mem_id = self.remember(content, source_id=source_id, scope=scope,
                                       extra_meta={"reflect_source": "memory_flush"})
                logger.info(f"[Memory-L3:{self.name}] reflect_and_remember: {content[:60]}")
                return mem_id
        except Exception as e:
            logger.debug(f"[Memory-L3:{self.name}] reflect_and_remember failed: {e}")
        return None

    # ─── L5: 身份/性格记忆 ───────────────────────────────────────────────────

    def get_identity(self) -> Dict[str, Any]:
        """
        读取 agents/identity/{name}.yaml 中的身份配置。
        如果 PUT /api/agents/{name}/identity 设置了临时覆盖，
        则覆盖字段优先（重启后丢失）。
        """
        try:
            import yaml
            p = IDENTITY_DIR / f"{self.name}.yaml"
            base: Dict[str, Any] = {}
            if p.exists():
                base = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if self._identity_override:
                base = {**base, **self._identity_override}
            return base
        except Exception as e:
            logger.debug(f"[Memory-L5:{self.name}] get_identity: {e}")
        return {}

    def get_personality_prompt(self) -> str:
        """返回性格+行为准则 system prompt 片段，直接注入 LLM system message"""
        identity = self.get_identity()
        parts = []
        personality = identity.get("personality", "")
        if personality:
            parts.append(f"【角色定位】{personality}")
        guidelines = identity.get("behavioral_guidelines", [])
        if guidelines:
            parts.append("【行为准则】\n" + "\n".join(f"- {g}" for g in guidelines))
        return "\n\n".join(parts)

    def get_tool_chain(self) -> List[str]:
        """返回当前 agent 的工具链列表"""
        return self.get_identity().get("tool_chain", [])

    def get_memory_write_trigger(self) -> str:
        """返回 L3 写入触发条件: on_adoption | on_evaluation | on_discovery | always"""
        return self.get_identity().get("memory_write_trigger", "on_adoption")

    # ─── 诊断 ────────────────────────────────────────────────────────────────

    def memory_summary(self) -> Dict[str, Any]:
        """返回当前 agent 五层记忆状态摘要，供 JobMaster 审计"""
        summary: Dict[str, Any] = {"agent": self.name}
        try:
            summary["l2_recent_tasks"] = len(self.recall_recent(5))
        except Exception:
            summary["l2_recent_tasks"] = -1
        try:
            from services.memory_service import MemoryService
            svc = MemoryService.get_instance()
            summary["l3_private_count"] = len(svc.list_memories(f"agent:{self.name}"))
            summary["l3_shared_count"]  = len(svc.list_memories("shared"))
        except Exception:
            summary["l3_private_count"] = summary["l3_shared_count"] = -1
        identity = self.get_identity()
        summary["l5_loaded"]      = bool(identity)
        summary["l5_personality"] = bool(identity.get("personality"))
        summary["l5_tool_chain"]  = identity.get("tool_chain", [])
        summary["write_trigger"]  = identity.get("memory_write_trigger", "unset")
        return summary

"""
记忆服务 — 三层记忆架构 + 安全机制
Layer 1: Mem0 向量存储 (即时记忆 + 置信度评分)
Layer 2: 聚合规则文件 (由 cleanup job 生成)
Layer 3: 行为调整 (通过 get_context() 注入 prompt)
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from mem0 import Memory
from mem0.configs.base import MemoryConfig, VectorStoreConfig, LlmConfig, EmbedderConfig

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLM_CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
CHROMA_PATH = os.environ.get("MEM0_CHROMA_DIR") or os.path.join(BASE_DIR, "chroma_db")

# 推测性语言词列表 — 含此类词的内容不存储为记忆（防幻觉）
HEDGING_WORDS = ["可能", "也许", "不确定", "大概", "或许", "似乎", "应该是", "猜测"]


def _load_llm_config() -> dict:
    with open(LLM_CONFIG_PATH) as f:
        cfg = json.load(f)
    provider = cfg.get("last_provider", "minimax")
    return cfg.get(provider, cfg.get("minimax", {}))


def _build_mem0_config() -> MemoryConfig:
    llm_cfg = _load_llm_config()
    return MemoryConfig(
        vector_store=VectorStoreConfig(
            provider="chroma",
            config={
                "collection_name": "aiticket_memory",
                "path": CHROMA_PATH,
            }
        ),
        llm=LlmConfig(
            provider="openai",
            config={
                "model": llm_cfg.get("model_name", "MiniMax-M2.7"),
                "api_key": llm_cfg.get("api_key", ""),
                "openai_base_url": llm_cfg.get("base_url", ""),
                "max_tokens": 2000,
            }
        ),
        embedder=EmbedderConfig(
            provider="huggingface",
            config={
                "model": "paraphrase-multilingual-MiniLM-L12-v2",
                "embedding_dims": 384,
            }
        ),
    )


class MemoryService:
    """
    记忆服务 — 单例使用，在 main.py lifespan 中初始化

    安全机制:
    - 幻觉防护: 无出处或含推测性语言的内容拒绝存储
    - 置信度门槛: 只有 confidence >= 0.6 的记忆才注入上下文
    - 注入上限: 每次最多 10 条，总 token ≤ 约 2000
    - 用户隔离: 通过 user_id scope 严格隔离
    - 时效衰减: 超过 90 天未引用则按 0.9/月 衰减
    """

    CONFIDENCE_THRESHOLD = 0.6    # 注入上下文的最低置信度
    MAX_INJECT = 10               # 每次最多注入条数
    DECAY_DAYS = 90               # 超过此天数开始衰减
    DECAY_COEFF = 0.9             # 每月衰减系数（衰减后 confidence *= 0.9^months）

    _instance: "MemoryService | None" = None
    _lock = __import__("threading").Lock()

    @classmethod
    def get_instance(cls) -> "MemoryService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.memory = Memory(config=_build_mem0_config())
        logger.info("MemoryService 初始化成功 (Chroma backend)")

    # ─── 写入 ────────────────────────────────────────────────────────────────

    def add_learning(
        self,
        user_id: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        添加一条学习记忆。

        前置条件（防幻觉规则）:
        1. metadata 必须提供 source_ticket_id 或 source_kb_id
        2. content 不能含推测性语言

        初始置信度 = 0.5；人工采纳后调用 update_confidence(+0.1) 提升。
        """
        # 规则1: 来源验证
        src = (metadata or {})
        if not src.get("source_ticket_id") and not src.get("source_kb_id"):
            logger.warning("[Memory] 拒绝存储无出处的记忆 (缺少 source_ticket_id/source_kb_id)")
            return None

        # 规则2: 推测性语言过滤
        if any(w in content for w in HEDGING_WORDS):
            logger.warning(f"[Memory] 过滤推测性内容: {content[:60]}…")
            return None

        meta = {
            "confidence": 0.5,
            "created_at": datetime.utcnow().isoformat(),
            "last_used_at": datetime.utcnow().isoformat(),
            "use_count": 0,
            **src,
        }

        result = self.memory.add(
            messages=[{"role": "user", "content": content}],
            user_id=user_id,
            metadata=meta,
            infer=False,   # 精确存储，不做 LLM 事实提取（避免内容被改写）
        )

        # 兼容不同版本的返回结构
        if isinstance(result, dict):
            results_list = result.get("results", [])
            mem_id = results_list[0].get("id") if results_list else result.get("id")
        else:
            mem_id = str(result)

        logger.info(f"[Memory] 记忆添加成功 [{user_id}]: {mem_id}")
        return mem_id

    # ─── 检索注入 ─────────────────────────────────────────────────────────────

    def get_context(self, user_id: str, query: str) -> list:
        """
        检索相关记忆注入上下文。

        - 只返回 confidence >= CONFIDENCE_THRESHOLD 的记忆
        - 最多返回 MAX_INJECT 条
        - 自动更新 last_used_at 和 use_count（用于健康度统计）
        """
        raw = self.memory.search(
            query=query,
            user_id=user_id,
            limit=self.MAX_INJECT + 5,  # 多取几条，过滤后保留 MAX
        )
        memories = raw.get("results", raw) if isinstance(raw, dict) else raw

        filtered = [
            m for m in memories
            if (m.get("metadata") or {}).get("confidence", 0) >= self.CONFIDENCE_THRESHOLD
        ][:self.MAX_INJECT]

        # 更新使用统计（fire-and-forget，失败不影响主流程）
        for m in filtered:
            mem_id = m.get("id")
            if not mem_id:
                continue
            old = m.get("metadata") or {}
            try:
                self.memory.update(
                    mem_id,
                    data=m.get("memory", ""),
                    metadata={
                        **old,
                        "last_used_at": datetime.utcnow().isoformat(),
                        "use_count": old.get("use_count", 0) + 1,
                    },
                )
            except Exception:
                pass

        return filtered

    # ─── 置信度反馈 ──────────────────────────────────────────────────────────

    def update_confidence(self, mem_id: str, delta: float) -> bool:
        """
        更新记忆置信度。
        采纳反馈: delta = +0.1
        驳回反馈: delta = -0.2
        """
        try:
            # 优先用 get(id) 直接获取，避免 get_all() 用户作用域问题
            target = self.memory.get(mem_id)
            if not target:
                logger.warning(f"[Memory] 未找到记忆: {mem_id}")
                return False

            # mem0 get() 返回单条记忆 dict
            old_conf = (target.get("metadata") or {}).get("confidence", 0.5)
            new_conf = round(max(0.0, min(1.0, old_conf + delta)), 3)
            new_meta = {**(target.get("metadata") or {}), "confidence": new_conf}
            self.memory.update(mem_id, data=target.get("memory", ""), metadata=new_meta)
            logger.info(f"[Memory] 置信度更新 {mem_id}: {old_conf:.2f} → {new_conf:.2f}")
            return True
        except Exception as e:
            logger.error(f"[Memory] 置信度更新失败: {e}")
            return False

    # ─── 管理员工具 ───────────────────────────────────────────────────────────

    def list_memories(self, user_id: str) -> list:
        """列出用户所有记忆（管理员工具）"""
        result = self.memory.get_all(user_id=user_id)
        return result.get("results", result) if isinstance(result, dict) else result

    def delete_memory(self, mem_id: str) -> bool:
        """删除有害记忆（管理员工具）"""
        try:
            self.memory.delete(mem_id)
            logger.info(f"[Memory] 记忆已删除: {mem_id}")
            return True
        except Exception as e:
            logger.error(f"[Memory] 删除失败: {e}")
            return False

    def audit_memory(self, mem_id: str, approved: bool) -> bool:
        """
        人工审核标记。
        approved=True  → confidence 提升至 0.8
        approved=False → confidence 降至 0.2（等待清理）
        """
        delta = 0.3 if approved else -0.3   # 0.5 + 0.3 = 0.8 ; 0.5 - 0.3 = 0.2
        return self.update_confidence(mem_id, delta)

    # ─── 矛盾检测 ─────────────────────────────────────────────────────────────

    def detect_contradictions(self, user_id: str) -> list:
        """
        矛盾检测: 检索语义相似但含否定关系的记忆对。
        返回矛盾对列表，每项含 mem1, mem2, type。
        """
        all_mems = self.list_memories(user_id)
        seen_pairs: set = set()
        contradictions = []
        NEG_WORDS = {"不", "否", "无", "禁止", "不能", "不应", "错误"}

        for m in all_mems:
            content = m.get("memory", "")
            if not content:
                continue
            has_neg = bool(NEG_WORDS.intersection(set(content)))

            raw = self.memory.search(query=content, user_id=user_id, limit=5)
            similar = raw.get("results", raw) if isinstance(raw, dict) else raw

            for s in similar:
                if s.get("id") == m.get("id"):
                    continue
                pair = tuple(sorted([m.get("id"), s.get("id")]))
                if pair in seen_pairs:
                    continue

                s_content = s.get("memory", "")
                s_has_neg = bool(NEG_WORDS.intersection(set(s_content)))

                if has_neg != s_has_neg:
                    seen_pairs.add(pair)
                    contradictions.append({
                        "mem1": {"id": m.get("id"), "memory": content},
                        "mem2": {"id": s.get("id"), "memory": s_content},
                        "type": "negation",
                    })

        return contradictions

    # ─── 时效衰减 ─────────────────────────────────────────────────────────────

    def run_time_decay(self, user_id: str) -> int:
        """
        时效衰减: 超过 DECAY_DAYS 天未引用的记忆按 DECAY_COEFF^months 衰减。
        通常由 SchedulerService 每周调用。
        返回: 本次衰减的记忆条数。
        """
        cutoff = datetime.utcnow() - timedelta(days=self.DECAY_DAYS)
        all_mems = self.list_memories(user_id)
        decayed = 0

        for m in all_mems:
            meta = m.get("metadata") or {}
            last_used_str = meta.get("last_used_at") or meta.get("created_at")
            if not last_used_str:
                continue

            try:
                last_used = datetime.fromisoformat(last_used_str)
            except ValueError:
                continue

            if last_used < cutoff:
                months = (datetime.utcnow() - last_used).days / 30.0
                old_conf = meta.get("confidence", 0.5)
                new_conf = round(old_conf * (self.DECAY_COEFF ** months), 3)
                self.memory.update(
                    m.get("id"),
                    data=m.get("memory", ""),
                    metadata={**meta, "confidence": new_conf},
                )
                decayed += 1

        logger.info(f"[Memory] 时效衰减 [{user_id}]: {decayed} 条记忆已衰减")
        return decayed

    # ─── 健康度报告 ───────────────────────────────────────────────────────────

    def get_health_report(self, user_id: str) -> dict:
        """
        5维度记忆健康报告 (Spec 3.3.3)
        1. 覆盖率  > 30%
        2. 准确率  > 70%  (以平均 confidence 近似)
        3. 多样性  > 5 个主题
        4. 时效性  > 20% (90天内新增)
        5. 矛盾度  < 5%
        """
        all_mems = self.list_memories(user_id)
        total = len(all_mems)

        if total == 0:
            return {"total_memories": 0, "message": "暂无记忆数据", "generated_at": datetime.utcnow().isoformat()}

        # 1. 覆盖率
        referenced = sum(1 for m in all_mems if (m.get("metadata") or {}).get("use_count", 0) > 0)
        coverage = round(referenced / total, 3)

        # 2. 准确率 (confidence 均值)
        avg_conf = sum((m.get("metadata") or {}).get("confidence", 0.5) for m in all_mems) / total
        accuracy = round(avg_conf, 3)

        # 3. 多样性
        topics = {
            (m.get("metadata") or {}).get("memory_type") or (m.get("metadata") or {}).get("module")
            for m in all_mems
        } - {None}
        diversity = len(topics)

        # 4. 时效性
        cutoff_90 = datetime.utcnow() - timedelta(days=90)
        recent_count = 0
        for m in all_mems:
            created_str = (m.get("metadata") or {}).get("created_at", "2000-01-01T00:00:00")
            try:
                if datetime.fromisoformat(created_str) > cutoff_90:
                    recent_count += 1
            except ValueError:
                pass
        recency = round(recent_count / total, 3)

        # 5. 矛盾度
        contradictions = self.detect_contradictions(user_id)
        contradiction_rate = round(len(contradictions) / total, 3)

        def _status(val: float, target: float, lower_is_better: bool = False) -> str:
            if lower_is_better:
                return "良好" if val <= target else "需清理"
            return "良好" if val >= target else "偏低"

        return {
            "total_memories": total,
            "coverage":          {"value": coverage,           "target": ">30%",   "status": _status(coverage, 0.3)},
            "accuracy":          {"value": accuracy,           "target": ">70%",   "status": _status(accuracy, 0.7)},
            "diversity":         {"value": diversity,          "target": ">5个主题", "status": "良好" if diversity >= 5 else "偏低"},
            "recency":           {"value": recency,            "target": ">20%",   "status": _status(recency, 0.2)},
            "contradiction_rate":{"value": contradiction_rate, "target": "<5%",    "status": _status(contradiction_rate, 0.05, lower_is_better=True)},
            "generated_at": datetime.utcnow().isoformat(),
        }

    def cleanup_low_quality(self, user_id: str, threshold: float = 0.3) -> int:
        """清理置信度低于 threshold 的记忆（管理员工具）"""
        all_mems = self.list_memories(user_id)
        removed = 0
        for m in all_mems:
            if (m.get("metadata") or {}).get("confidence", 0.5) < threshold:
                self.delete_memory(m.get("id"))
                removed += 1
        logger.info(f"[Memory] 清理完成 [{user_id}]: 删除 {removed} 条低质量记忆")
        return removed

    def is_available(self) -> bool:
        """
        检查 ChromaDB 连通性和 collection 存在性（借鉴 Hermes provider is_available() gate）。
        AgentMemoryMixin 在每次 recall 前调用此方法，不可用时降级到只读/空值，不抛异常。
        """
        try:
            result = self.memory.get_all(user_id="__health_probe__")
            return True
        except Exception as e:
            logger.warning(f"[Memory] is_available=False: {e}")
            return False

    def clear_scope(self, scope_user_id: str) -> int:
        """
        清空指定 scope 的所有记忆（事故降级工具）。
        scope_user_id 例: "agent:reply", "shared"
        注意: scope="shared" 需由调用方（CLI）做二次确认，本方法不做拦截。
        返回: 删除条数
        """
        all_mems = self.list_memories(scope_user_id)
        removed = 0
        for m in all_mems:
            mid = m.get("id")
            if mid and self.delete_memory(mid):
                removed += 1
        logger.info(f"[Memory] clear_scope [{scope_user_id}]: 删除 {removed} 条")
        return removed

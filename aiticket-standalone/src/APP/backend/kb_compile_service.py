"""
KB 编译综合页服务。

对现有 KB 原始文档做 LLM 编译，生成按话题汇总的"综合解析"条目。
存入 KB 索引（source_kind='kb_compiled'），查询时优先命中。
"""
import re
import json
import logging
import hashlib
import os
from typing import Optional

logger = logging.getLogger(__name__)

_LLM_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'llm_config.json')


def _load_default_llm_config() -> dict:
    """读取 llm_config.json，返回当前生效 provider 的配置"""
    try:
        with open(_LLM_CONFIG_PATH, encoding='utf-8') as f:
            full = json.load(f)
        provider = full.get('last_provider', '')
        if not provider or provider == 'none':
            return {}
        pcfg = full.get(provider, {})
        return {
            'provider': provider,
            'api_key': pcfg.get('api_key', ''),
            'model_name': pcfg.get('model_name', ''),
            'base_url': pcfg.get('base_url', ''),
        }
    except Exception:
        return {}


class KBCompileService:
    """从 KB 原始 chunks 编译综合解析页，存为 kb_compiled 条目"""

    COMPILE_PROMPT_TEMPLATE = """基于以下知识库文档内容，生成关于「{topic}」的综合解析。

要求：
- 直接给出产品事实，不重复原文
- 合并来自多个文档的相关信息（如有冲突，分别注明来源）
- 格式（严格按此输出，无需其他内容）：

## 定义与背景
（2-4句话说明概念和使用场景）

## 系统现状
（列出目前支持什么功能/配置项，每条一行以 - 开头）

## 操作路径
（如有配置步骤，列出 1. 2. 3. 格式的操作步骤；无则省略此节）

## 已知限制
（系统不支持的情况，每条一行以 - 开头；无则省略此节）

## 相关概念
（列出2-5个相关话题名称，用逗号分隔）

参考资料：
{context}"""

    _CONVERTED_ROOT = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'KB', 'OUTPUT', 'converted')
    )

    def __init__(self, kb_hybrid_index, kb_runtime_service, llm_service):
        self.index = kb_hybrid_index
        self.kb = kb_runtime_service
        self.llm = llm_service

    # ── BIP 实体验证 ──────────────────────────────────────────────────────────

    # 硬黑名单：文档类型词、模块目录名、指代不明的横切分类词
    _TOPIC_BLACKLIST = frozenset({
        "使用手册", "操作指南", "开发规范", "技术分享",
        "平台公共", "架构优化", "应用与需求使用手册",
        "产品白皮书", "技术规范", "培训材料", "操作手册",
    })

    def _default_priority_topics(self) -> list:
        """从 KB/OUTPUT/converted/ 目录扫描域名，数据驱动替代硬编码列表。"""
        topics = []
        if not os.path.isdir(self._CONVERTED_ROOT):
            return topics
        for name in sorted(os.listdir(self._CONVERTED_ROOT)):
            if not os.path.isdir(os.path.join(self._CONVERTED_ROOT, name)):
                continue
            if name in self._TOPIC_BLACKLIST:
                continue
            topics.append(name)
        return topics

    def _validate_topic_is_bip_entity(self, topic: str) -> tuple:
        """
        验证 topic 是否为明确的 BIP 业务实体或业务动作。
        返回 ("ok"|"reject"|"ambiguous", reason: str)
        """
        t = topic.strip()

        # 1. 硬黑名单 — 文档类型词/目录名
        if t in self._TOPIC_BLACKLIST:
            return ("reject", f"「{t}」是文档类型词或目录名，请改用具体 BIP 业务对象，如「工作流」「代理审批」")

        # 2. 黑名单关键词匹配（"XX使用手册" 这类组合词也拒绝）
        for kw in ("使用手册", "操作指南", "开发规范", "技术分享"):
            if kw in t and len(t) > len(kw):
                return ("ambiguous", f"「{t}」含文档类型词「{kw}」，实体指代不明确，需人工确认")

        # 3. 已知 BIP 对象白名单（从 req_clusters.topic_l2 / documents.l2_module 查）
        try:
            from vector_store import VectorStore
            vs = VectorStore.get_instance()
            known = set()
            for row in vs.list_clusters(limit=500):
                if row.get("topic_l2"):
                    known.add(row["topic_l2"].strip())
            if t in known:
                return ("ok", "已知 BIP 业务对象")
        except Exception:
            pass

        # 4. 过短或纯英文 — 可能是代码符号，视为 ambiguous
        if len(t) <= 2 or t.isascii():
            return ("ambiguous", f"「{t}」过短或非中文，无法判断是否为 BIP 实体")

        # 5. 其余视为可接受（入库后 Fiona 每日审计可标 pending）
        return ("ok", "通过基础校验")

    def compile_topic(
        self,
        topic: str,
        llm_config: dict = None,
        override_content: str = None,
        extra_metadata: dict = None,
        skip_bip_validation: bool = False,
        search_query: str = None,
        project_key: str = "_global",
    ) -> Optional[dict]:
        """
        编译一个话题的综合解析页，入库为 kb_compiled 条目。

        override_content: 如果提供，直接用该内容做编译（如 PRD 内容），不查 KB chunks
        extra_metadata: 额外的元数据（如 status='计划中'）
        skip_bip_validation: 内部批量调用时跳过 BIP 实体验证（已预审的批次）

        返回：{'topic': str, 'chars': int, 'content_id': str, 'related_topics': [...]}
        或   None（拒绝/失败）
        或   {'topic': str, 'bip_judgment_status': 'pending_user', 'content_id': str}（ambiguous 已入队）
        """
        if not skip_bip_validation:
            verdict, reason = self._validate_topic_is_bip_entity(topic)
            if verdict == "reject":
                logger.warning("[KB compile] BIP 实体验证拒绝: topic=%s reason=%s", topic, reason)
                raise ValueError(f"TOPIC_REJECTED: {reason}")
            elif verdict == "ambiguous":
                logger.info("[KB compile] BIP 实体验证存疑，入 pending 队列: topic=%s", topic)
                try:
                    from services.fiona_question_service import FionaQuestionService
                    FionaQuestionService().enqueue_topic(topic, reason)
                except Exception as _e:
                    logger.warning("[KB compile] enqueue_topic failed: %s", _e)
                return {
                    "topic": topic,
                    "bip_judgment_status": "pending_user",
                    "reason": reason,
                    "content_id": None,
                }

        if not self.llm:
            logger.warning("[KB compile] llm_service 未配置，跳过编译")
            return None

        # 获取原始材料
        if override_content:
            context_text = override_content[:6000]
            source_names = ["直接提供的内容"]
        else:
            _query = search_query or topic
            results = self.kb.search_bundle(_query, top_k=20) if self.kb else []
            # search_bundle always returns a dict with 'items'
            if isinstance(results, dict):
                items = results.get('items', [])
            else:
                items = results or []

            if not items:
                logger.info(f"[KB compile] 话题 '{topic}' 无相关 chunks，跳过")
                return None

            # 构造上下文（取前 12 条，每条限 400 字）
            parts = []
            source_names = []
            for i, item in enumerate(items[:12]):
                text = item.get('chunk_text') or item.get('chunk_preview', '')
                name = item.get('name', f'文档{i+1}')
                parts.append(f"【{name}】\n{text[:400]}")
                if name not in source_names:
                    source_names.append(name)
            context_text = "\n\n".join(parts)

        # LLM 编译
        prompt = self.COMPILE_PROMPT_TEMPLATE.format(
            topic=topic,
            context=context_text,
        )

        cfg = llm_config or _load_default_llm_config()
        if not cfg.get('api_key'):
            logger.warning(f"[KB compile] 无LLM API Key配置，跳过编译: topic={topic}")
            return None
        try:
            compiled_text = self.llm.call_llm(
                prompt=prompt,
                api_key=cfg.get('api_key'),
                provider=cfg.get('provider', 'gemini'),
                model_name=cfg.get('model_name', ''),
                base_url=cfg.get('base_url', ''),
            )
            compiled_text = (compiled_text or '').strip()
            if len(compiled_text) < 100:
                logger.warning(f"[KB compile] LLM 输出过短: topic={topic} len={len(compiled_text)}")
                return None
            # 拦截 LLM 错误消息（如"Error: No API Key"）
            if compiled_text.lower().startswith('error:') or '请设置' in compiled_text or 'api key' in compiled_text.lower():
                logger.warning(f"[KB compile] LLM 返回错误消息，不入库: {compiled_text[:80]}")
                return None
        except Exception as e:
            logger.error(f"[KB compile] LLM 调用失败: {e}")
            return None

        # 提取相关概念（从"相关概念"节）
        related_topics = []
        m = re.search(r'##\s*相关概念\s*\n(.+)', compiled_text)
        if m:
            related_topics = [t.strip() for t in m.group(1).split(',') if t.strip()]

        # 构建入库文档
        topic_hash = hashlib.md5(topic.encode()).hexdigest()[:8]
        content_id = f"kb_compiled:{topic_hash}"

        meta = extra_metadata or {}
        doc = {
            'content_id': content_id,
            'source_kind': 'kb_compiled',
            'name': f"综合解析：{topic}",
            'summary': f"关于「{topic}」的综合解析，来源：{', '.join(source_names[:3])}",
            'source_rel_path': f"compiled/{topic}",
            'citation_label': 'compiled',
            'l1_module': meta.get('l1_module', ''),
            'l2_module': meta.get('l2_module', ''),
            'doc_type': 'kb_compiled',
            'keywords': topic + ' ' + ' '.join(related_topics[:3]),
            'project_key': project_key or "_global",
        }
        if meta.get('status'):
            doc['summary'] += f" [状态：{meta['status']}]"
        if meta.get('req_id'):
            doc['source_rel_path'] += f"/{meta['req_id']}"

        try:
            self.index.add_item(doc, compiled_text)
            logger.info(f"[KB compile] 话题编译完成: topic={topic} chars={len(compiled_text)} content_id={content_id}")
        except Exception as e:
            logger.error(f"[KB compile] 入库失败: {e}")
            return None

        return {
            'topic': topic,
            'chars': len(compiled_text),
            'content_id': content_id,
            'related_topics': related_topics,
        }

    def compile_all(self, priority_topics: list = None, llm_config: dict = None) -> list:
        """
        批量编译。priority_topics 未指定时使用默认高频话题列表。
        返回成功编译的结果列表。
        """
        if not priority_topics:
            priority_topics = self._default_priority_topics()

        # 话题搜索 query 覆盖：某些话题作为单一词条向量命中率低，需用扩展 query 检索原材料
        _search_overrides = {
            "公式选人": "工作流 公式 选人 审批人",
            "分支合并条件": "工作流 分支 合并 条件 判断",
        }

        results = []
        for topic in priority_topics:
            try:
                sq = _search_overrides.get(topic)
                result = self.compile_topic(topic, llm_config=llm_config, search_query=sq)
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"[KB compile] 批量编译失败: topic={topic} err={e}")

        logger.info(f"[KB compile] 批量编译完成: {len(results)}/{len(priority_topics)} 个话题")
        return results

    def lint(self) -> dict:
        """
        KB 健康检查：
        - kb_compiled 条目覆盖率（高频话题是否有编译页）
        - 长期未更新的编译页
        返回 issues 列表。
        """
        default_topics = self._default_priority_topics()
        issues = []
        missing = []

        for topic in default_topics:
            results = self.index.search(topic, top_k=3, source_kind='kb_compiled')
            if not results:
                missing.append(topic)

        if missing:
            issues.append({
                'type': 'missing_compiled_page',
                'topics': missing,
                'suggestion': f"建议运行 POST /api/kb/compile 为以下话题生成综合解析: {', '.join(missing)}"
            })

        return {
            'issues': issues,
            'coverage': f"{len(default_topics) - len(missing)}/{len(default_topics)}",
            'missing_topics': missing,
        }


# 模块级单例
_compile_service_instance = None


def register_compile_service(instance: KBCompileService) -> None:
    global _compile_service_instance
    _compile_service_instance = instance


def get_compile_service() -> Optional[KBCompileService]:
    return _compile_service_instance


def get_or_create_compile_service() -> KBCompileService:
    """返回已注册实例；若无（如 daemon 进程），则自动创建一个。"""
    global _compile_service_instance
    if _compile_service_instance is not None:
        return _compile_service_instance
    from kb_runtime_service import KnowledgeRuntimeService
    from llm_service import LLMService
    _kb = KnowledgeRuntimeService()
    _compile_service_instance = KBCompileService(
        kb_hybrid_index=_kb.hybrid_index,
        kb_runtime_service=_kb,
        llm_service=LLMService(),
    )
    return _compile_service_instance

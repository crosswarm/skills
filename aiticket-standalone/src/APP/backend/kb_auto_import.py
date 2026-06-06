"""
KB 知识自动采集模块。

从用户输入（工单回复、需求备注、PRD修订意见）中识别产品知识，
自动摘要后保存到 KB 知识库（source_kind='user_contributed'）。
"""
import re
import json
import logging
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kb-auto-import")


class KBAutoImport:
    """从用户输入自动识别产品知识并保存到知识库"""

    KNOWLEDGE_SIGNALS = [
        r'(步骤|操作路径|配置方式|设置方法|如何配置|怎么设置)',
        r'(暂不支持|目前支持|系统支持|已支持|不支持)',
        r'(解决方案|排查步骤|原因是|根本原因|问题原因)',
        r'(注意|提示|警告|限制|边界条件|约束)',
        r'^\s*[1-9][0-9]?[\.\)]\s+.{10,}',  # 编号列表（操作步骤）
    ]
    MIN_LENGTH = 80

    # 防重复：同一 ref_id + text_hash 不重复入库
    _seen_hashes: set = set()
    _seen_lock = threading.Lock()

    def __init__(self, kb_hybrid_index, llm_service):
        self.index = kb_hybrid_index
        self.llm = llm_service

    def should_import(self, text: str) -> bool:
        """快速启发式判断：是否包含值得入库的知识（不调用 LLM）"""
        if not text or len(text.strip()) < self.MIN_LENGTH:
            return False
        return any(re.search(p, text, re.MULTILINE) for p in self.KNOWLEDGE_SIGNALS)

    def _make_hash(self, ref_id: str, text: str) -> str:
        return hashlib.md5(f"{ref_id}:{text[:200]}".encode()).hexdigest()[:12]

    def extract_and_save(
        self,
        text: str,
        source_context: dict,
        llm_config: dict = None,
    ) -> Optional[object]:
        """
        LLM 从 text 提取知识，入库为 user_contributed 条目。
        非阻塞：内部走后台线程。
        返回 Future（调用方通常不需要等待结果）。
        """
        if not self.should_import(text):
            return None

        ref_id = source_context.get('ref_id', 'unknown')
        h = self._make_hash(ref_id, text)
        with self._seen_lock:
            if h in self._seen_hashes:
                logger.debug(f"[KB auto-import] 跳过重复: {h}")
                return None
            self._seen_hashes.add(h)

        future = _executor.submit(self._do_extract, text, source_context, llm_config, h)
        return future

    def _do_extract(self, text: str, source_context: dict, llm_config: dict, dedupe_hash: str) -> Optional[dict]:
        """在后台线程执行 LLM 提取和入库"""
        try:
            result = self._call_llm_extract(text, llm_config)
            if not result or not result.get('is_knowledge'):
                return None

            topic = result.get('topic', '')
            summary = result.get('summary', '')
            key_facts = result.get('key_facts', [])

            chunk_text = f"# {topic}\n\n{summary}\n\n" + "\n".join(f"- {f}" for f in key_facts)
            chunk_preview = chunk_text[:240]

            ref_id = source_context.get('ref_id', '')
            source_type = source_context.get('type', 'unknown')
            content_id = f"user_contributed:{source_type}:{ref_id}:{dedupe_hash}"
            chunk_id = f"{content_id}::chunk-001"

            doc = {
                'content_id': content_id,
                'source_kind': 'user_contributed',
                'name': topic,
                'summary': summary,
                'source_rel_path': f"{source_type}/{ref_id}",
                'citation_label': 'real_world',
                'l1_module': '',
                'l2_module': '',
                'doc_type': 'user_contributed',
                'keywords': key_facts[:3],
            }

            # KnowledgeHybridIndex.add_item(item, text) handles chunking internally
            self.index.add_item(doc, chunk_text)
            logger.info(f"[KB auto-import] 知识入库: topic={topic} ref={ref_id} hash={dedupe_hash}")
            return {'saved': True, 'topic': topic, 'chunk_id': chunk_id}

        except Exception as e:
            logger.error(f"[KB auto-import] 提取失败: {e}")
            return None

    def _call_llm_extract(self, text: str, llm_config: dict = None) -> Optional[dict]:
        """调用 LLM 提取知识，返回结构化结果"""
        if not self.llm:
            return None

        prompt = (
            "从以下内容提取产品知识片段，输出 JSON（仅输出 JSON，不要任何解释）：\n"
            '{"topic":"主题词(5字以内)","summary":"100字以内摘要","key_facts":["事实1","事实2","事实3"],"is_knowledge":true}\n'
            "如果内容不含产品知识（仅问候语/通用表述/无信息量），输出：{\"is_knowledge\":false}\n\n"
            f"内容：\n{text[:1500]}"
        )

        cfg = llm_config or self._load_default_llm_config()
        if not cfg.get('api_key'):
            logger.debug("[KB auto-import] 无LLM API Key，跳过知识提取")
            return None
        try:
            response = self.llm.call_llm(
                prompt=prompt,
                api_key=cfg.get('api_key'),
                provider=cfg.get('provider', 'gemini'),
                model_name=cfg.get('model_name', ''),
                base_url=cfg.get('base_url', ''),
            )
            # 提取 JSON
            match = re.search(r'\{.*\}', response or '', re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception as e:
            logger.warning(f"[KB auto-import] LLM 调用失败: {e}")
        return None

    @staticmethod
    def _load_default_llm_config() -> dict:
        try:
            _path = os.path.join(os.path.dirname(__file__), 'llm_config.json')
            with open(_path, encoding='utf-8') as f:
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


# 模块级单例
_auto_import_instance: Optional[KBAutoImport] = None


def register_auto_import(instance: KBAutoImport) -> None:
    global _auto_import_instance
    _auto_import_instance = instance


def get_auto_import() -> Optional[KBAutoImport]:
    return _auto_import_instance

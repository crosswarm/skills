"""模块感知智能回复 API — POST /api/reply/generate-by-module + GET /api/reply/module-coverage"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import List, Optional
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from auth_deps import require_reply_quota, log_api_request

router = APIRouter(prefix="/api/reply", tags=["reply-module"])


class GenerateByModuleRequest(BaseModel):
    issue_key: str
    module: Optional[str] = None       # 强制指定模块；None 时自动推断
    force: bool = False                 # True=跳过回复缓存


@router.post("/generate-by-module")
def generate_by_module(req: GenerateByModuleRequest, raw_request: Request, _quota=Depends(require_reply_quota)):
    """按模块生成回复。module 指定时覆盖自动推断；空时走自动推断（同 /api/board/generate-reply）。"""
    log_api_request(raw_request, _quota, issue_key=req.issue_key)
    try:
        from board_service_chroma import BoardService
        # 获取主服务实例（main.py 已实例化，通过 app.state 或直接 import 全局）
        try:
            import main as _main
            _board_service = _main.board_service
        except Exception:
            raise HTTPException(status_code=503, detail="board_service 未初始化")

        result = _board_service.generate_reply_content(req.issue_key, force=req.force)
        if result.get("error"):
            raise HTTPException(status_code=422, detail=result["error"])

        # 推断模块（用于返回给调用方参考）
        ai_analysis = result.get("ai_analysis") or {}
        inferred_module = req.module or BoardService._resolve_module_category(ai_analysis)

        return {
            "status": "success",
            "reply": result.get("solution_content", result.get("reply_content", "")),
            "kb_refs": [
                {
                    "name": item.get("name", ""),
                    "module": item.get("l1_module", ""),
                    "score": round(item.get("score", 0), 3),
                }
                for item in (result.get("kb_evidence") or [])[:4]
            ],
            "module_used": inferred_module,
            "module_match_score": None,   # 留给未来打分扩展
            "fallback_used": inferred_module is None,
            "cached": result.get("cached", False),
            "word_count": result.get("word_count", 0),
            "reply_gateway": result.get("reply_gateway"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/module-coverage")
def module_coverage(module: str, raw_request: Request):
    """查询某模块在 KB / 样例 中的覆盖度，供调用方判断该模块能否使用智能回复。"""
    try:
        import sqlite3
        from pathlib import Path

        base = Path(__file__).resolve().parent.parent
        db_path = base.parent.parent / "data" / "sqlite" / "kb_chunks.db"

        # KB 文档覆盖
        kb_total = kb_module = 0
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                (kb_total,) = conn.execute("SELECT COUNT(DISTINCT content_id) FROM chunks").fetchone()
                (kb_module,) = conn.execute(
                    "SELECT COUNT(DISTINCT content_id) FROM chunks WHERE l1_module = ?", (module,)
                ).fetchone()
            finally:
                conn.close()

        # 样例覆盖（reply_trainer stats by_module）
        sample_count = adopted_count = 0
        try:
            from reply_trainer import ReplyTrainer
            import main as _main
            _stats = _main.board_service.reply_trainer._stats if hasattr(_main, "board_service") else {}
            bm = _stats.get("by_module", {}).get(module, {})
            sample_count = bm.get("total", 0)
            adopted_count = bm.get("adopted", 0)
        except Exception:
            pass

        coverage_level = "high" if kb_module >= 50 else ("medium" if kb_module >= 10 else "low")

        return {
            "module": module,
            "kb_docs_total": kb_total,
            "kb_docs_module": kb_module,
            "kb_coverage_pct": round(kb_module / kb_total * 100, 1) if kb_total else 0,
            "coverage_level": coverage_level,
            "reply_examples_total": sample_count,
            "reply_examples_adopted": adopted_count,
            "recommendation": (
                "可直接使用模块感知智能回复" if coverage_level == "high"
                else "覆盖有限，建议先补充该模块的 KB 文档后使用" if coverage_level == "medium"
                else "覆盖不足，建议先通过知识库管理补充该模块文档"
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 智能扩展（完善方案）端点 ──────────────────────────────────────────────────

class RefineRequest(BaseModel):
    issue_key: str
    user_draft: str
    focus_keywords: List[str] = []
    module: Optional[str] = None


@router.post("/refine")
def refine_reply(req: RefineRequest, raw_request: Request, _quota=Depends(require_reply_quota)):
    """基于用户修订草稿重跑语义搜索，返回精准扩展方案供 Claude 参考。"""
    log_api_request(raw_request, _quota, issue_key=req.issue_key, query_text=req.user_draft[:200])
    try:
        from board_service_chroma import BoardService
        from kb_runtime_service import KnowledgeRuntimeService
        import main as _main

        bs = _main.board_service

        ai_analysis = {}
        try:
            ai_analysis = bs.vector_store.get_cached_analysis(req.issue_key) or {}
        except Exception:
            pass

        query = " ".join([*req.focus_keywords, req.user_draft])[:500]
        module_cat = req.module or BoardService._resolve_module_category(ai_analysis)

        sim_results = bs.search_engine.search(query, top_k=5, min_score=0.6)
        sim = sim_results.get("results", []) if isinstance(sim_results, dict) else []

        kb_svc = KnowledgeRuntimeService()
        kb_raw = kb_svc.search_bundle(query, top_k=6, category=module_cat) or {}
        kb_evidence = (kb_raw.get("items") or [])[:4]

        ai_aug = {
            **ai_analysis,
            "solution_suggestion": req.user_draft,
            "user_focus_keywords": req.focus_keywords,
        }

        try:
            specificity = bs._compute_specificity_level(kb_evidence)
        except Exception:
            specificity = "normal"

        refined = bs._generate_styled_reply(
            ai_aug, sim, [], kb_evidence,
            module_category=module_cat,
            specificity_level=specificity,
        )

        return {
            "refined_solution": refined,
            "kb_sources": [i.get("name", "") for i in kb_evidence],
            "similar_issues": [
                {
                    "key": s.get("key", ""),
                    "summary": (s.get("summary") or "")[:80],
                    "score": s.get("score"),
                }
                for s in sim
            ],
            "search_keywords_used": req.focus_keywords + [req.user_draft[:60]],
            "module_used": module_cat,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

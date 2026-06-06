from __future__ import annotations
import logging

_LOG = logging.getLogger(__name__)


def build_issue_query(
    issue_key: str,
    ai_analysis: dict | None = None,
    *,
    fields: tuple[str, ...] = ("issue_title", "issue_description"),
    max_len: int = 800,
    cache_fn=None,  # optional callable(issue_key) -> dict | None  (returns raw Jira issue)
) -> str:
    """
    Build a query string for vector search from ai_analysis fields.
    Falls back to raw Jira cache via cache_fn if ai_analysis fields are empty.
    Returns "" if nothing found (caller should check and skip the search).
    """
    a = ai_analysis or {}
    parts = []
    for f in fields:
        v = (a.get(f) or "").strip()
        if v:
            parts.append(v)

    if not parts and cache_fn is not None:
        try:
            raw = cache_fn(issue_key) or {}
            summary = (raw.get("summary") or raw.get("title") or "").strip()
            desc = (raw.get("description") or "")[:600].strip()
            if summary:
                parts.append(summary)
            if desc:
                parts.append(desc)
        except Exception as e:
            _LOG.warning("[query_builder] cache_fn failed for %s: %s", issue_key, e)

    q = " ".join(parts).strip()[:max_len]
    if not q:
        _LOG.warning("[query_builder] empty query for issue_key=%s", issue_key)
    return q

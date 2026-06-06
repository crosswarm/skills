"""JSONL-based staging store for AI replies awaiting human batch approval.

Append-only with tombstones:
  - type "pending"  : new staging entry
  - type "approved" : marks entry as approved (tombstone)
  - type "rejected" : marks entry as rejected (tombstone)

Tombstone always wins over pending when aggregating state.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

import jira_service as _jira_svc

_JSONL_PATH = os.path.join(os.path.dirname(__file__), "../data/pending_batch_approve.jsonl")
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(_JSONL_PATH), exist_ok=True)


def _append_record(record: dict) -> None:
    """Append a single JSON record as one line to the JSONL file."""
    _ensure_dir()
    with open(_JSONL_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_all_records() -> list[dict]:
    """Read every line from the JSONL file. Returns [] if file missing."""
    if not os.path.exists(_JSONL_PATH):
        return []
    records: list[dict] = []
    try:
        with open(_JSONL_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    print(f"[pending_approval_store] bad JSONL line, skipping: {exc}")
    except OSError as exc:
        print(f"[pending_approval_store] cannot read file: {exc}")
    return records


def _load_state() -> dict[str, dict]:
    """
    Returns {approval_id: latest_record}.

    For each approval_id: if ANY tombstone (approved/rejected) exists it wins
    regardless of insertion order.  Active entries are those whose latest
    record type is "pending".
    """
    records = _read_all_records()
    # Group by approval_id; track whether a tombstone was seen
    groups: dict[str, list[dict]] = {}
    tombstoned: set[str] = set()

    for rec in records:
        aid = rec.get("approval_id")
        if not aid:
            continue
        groups.setdefault(aid, []).append(rec)
        if rec.get("type") in ("approved", "rejected"):
            tombstoned.add(aid)

    state: dict[str, dict] = {}
    for aid, recs in groups.items():
        if aid in tombstoned:
            # Pick the tombstone (prefer last tombstone if multiple)
            tombstones = [r for r in recs if r.get("type") in ("approved", "rejected")]
            state[aid] = tombstones[-1]
        else:
            # All records are "pending"; pick the last one
            state[aid] = recs[-1]

    return state


def _active_pending_records() -> list[dict]:
    """Return pending records not yet tombstoned, sorted newest-first."""
    state = _load_state()
    active = [rec for rec in state.values() if rec.get("type") == "pending"]
    active.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return active


def _filter_by_jira_status(records: list[dict], jira_client=None,
                           assignee_filter: set | None = None) -> list[dict]:
    """Remove records whose Jira status is not in _ACTIVE_STATUS and write auto-cleaned tombstones.
    Optionally filter to records whose Jira assignee is in assignee_filter (no tombstone written).

    Fails open: if Jira is unreachable, returns the original records unchanged.
    Uses a 60-second in-memory cache to avoid hammering Jira on repeated calls.
    Pass jira_client (a JiraService instance with valid session) for request-scoped auth;
    falls back to the module-level singleton if omitted.
    """
    if not records:
        return records

    client = jira_client or _jira_svc.jira_service

    now = time.time()
    to_fetch: list[str] = []
    # meta_map: issue_key → {"status": str, "assignee": str}
    meta_map: dict[str, dict] = {}

    for rec in records:
        key = rec.get("issue_key", "")
        if not key:
            continue
        cached = _JIRA_STATUS_CACHE.get(key)
        if cached and cached[1] > now:
            meta_map[key] = {"status": cached[0], "assignee": cached[2] if len(cached) > 2 else ""}
        else:
            to_fetch.append(key)

    if to_fetch:
        for i in range(0, len(to_fetch), 100):
            chunk = to_fetch[i:i + 100]
            jql = f"key in ({','.join(chunk)})"
            try:
                result = client.search_issues_rest_api(
                    jql, max_results=len(chunk), fields="status,assignee"
                )
                if "error" in result:
                    print(f"[pending_approval_store] jira status fetch error: {result['error']}, skip filter")
                    return records
                for issue in result.get("issues", []):
                    k = issue.get("key", "")
                    fields = issue.get("fields") or {}
                    status = (fields.get("status") or {}).get("name", "")
                    ass = fields.get("assignee") or {}
                    assignee_name = ass.get("name") or ass.get("displayName") or ""
                    if k:
                        meta_map[k] = {"status": status, "assignee": assignee_name}
                        _JIRA_STATUS_CACHE[k] = (status, now + _CACHE_TTL_SEC, assignee_name)
            except Exception as exc:
                print(f"[pending_approval_store] jira status fetch exception, skip filter: {exc}")
                return records

    active = []
    for rec in records:
        key = rec.get("issue_key", "")
        meta = meta_map.get(key, {})
        status = meta.get("status", "")
        if status and status not in _ACTIVE_STATUS:
            with _lock:
                _append_record({
                    "type": "rejected",
                    "approval_id": rec["approval_id"],
                    "ts": _now_iso(),
                    "approver": "system",
                    "reason": f"auto_cleaned: jira_status={status}",
                })
            print(f"[pending_approval_store] auto-cleaned {key} (status={status})")
            continue
        if assignee_filter:
            rec_assignee = meta.get("assignee", "")
            if rec_assignee and rec_assignee not in assignee_filter:
                continue
        active.append(rec)
    return active


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add(
    issue_key: str,
    reply_content: str,
    decision: dict,
    ai_fields: dict,
    *,
    issue_summary: str = "",
    customer_name: str = "",
    is_key_customer: bool = False,
    product_priority: str = "",
    issue_type_confirmed: str = "",
) -> str:
    """
    Store a pending approval entry.  Returns the new approval_id (UUID).

    If an existing non-tombstoned entry for issue_key already exists, it is
    replaced: a rejected tombstone is appended for the old approval_id, then
    a fresh pending record is written.
    """
    with _lock:
        # Check for an existing pending entry for this issue_key
        existing = get_by_issue_key(issue_key)
        if existing is not None:
            old_id = existing["approval_id"]
            _append_record({
                "type": "rejected",
                "approval_id": old_id,
                "ts": _now_iso(),
                "approver": "system",
                "reason": "superseded by new pending entry",
            })

        new_id = str(uuid.uuid4())
        record: dict = {
            "type": "pending",
            "approval_id": new_id,
            "issue_key": issue_key,
            "issue_summary": issue_summary,
            "customer_name": customer_name,
            "is_key_customer": is_key_customer,
            "reply_content": reply_content,
            "reply_summary": reply_content[:80],
            "composite_score": decision.get("composite_score", 0.0),
            "threshold": decision.get("threshold"),
            "product_priority": product_priority,
            "issue_type_confirmed": issue_type_confirmed,
            "ai_fields": ai_fields,
            "created_at": _now_iso(),
            "actor": "system",
        }
        _append_record(record)
        return new_id


def list_pending(limit: int = 200, offset: int = 0, jira_client=None,
                 project_key: str = "",
                 assignee_filter: set | None = None) -> list[dict]:
    """
    Return pending entries not yet approved or rejected, newest-first.

    Each entry exposes: approval_id, issue_key, issue_summary, customer_name,
    is_key_customer, reply_summary, composite_score, threshold,
    product_priority, issue_type_confirmed, created_at.

    Pass project_key to filter by project prefix (e.g. "LCZX" keeps only LCZX-* tickets).
    Pass assignee_filter (set of names) to show only tickets belonging to those assignees.
    Pass jira_client (a JiraService instance with valid request session) to enable
    real-time Jira status + assignee filtering; omit to skip (faster, no network call).
    """
    active = _active_pending_records()
    if project_key:
        prefix = f"{project_key}-"
        active = [r for r in active if (r.get("issue_key") or "").startswith(prefix)]
    if jira_client is not None:
        active = _filter_by_jira_status(active, jira_client=jira_client,
                                        assignee_filter=assignee_filter)
    page = active[offset: offset + limit]
    keys = (
        "approval_id", "issue_key", "issue_summary", "customer_name",
        "is_key_customer", "reply_summary", "composite_score", "threshold",
        "product_priority", "issue_type_confirmed", "created_at",
    )
    return [{k: rec.get(k) for k in keys} for rec in page]


def get(approval_id: str) -> dict | None:
    """Return the full pending record for approval_id, or None if not found / tombstoned."""
    state = _load_state()
    rec = state.get(approval_id)
    if rec is None or rec.get("type") != "pending":
        return None
    return rec


def approve(approval_id: str, approver: str = "operator") -> dict:
    """
    Approve a pending entry:
      1. Look up the full record.
      2. Call jira_service.reply_and_close_via_transition.
      3. Append an approved tombstone (jira_success reflects the call result).
      4. Return {"success": bool, "issue_key": str, "message": str}.

    Even on Jira failure the tombstone is appended so the entry leaves the
    pending queue and does not block future approvals.
    """
    entry = get(approval_id)
    if entry is None:
        return {
            "success": False,
            "issue_key": "",
            "message": f"approval_id {approval_id!r} not found or already tombstoned",
        }

    issue_key = entry["issue_key"]
    jira_success = False
    message = ""

    try:
        result = _jira_svc.reply_and_close_via_transition(
            issue_id=issue_key,
            comment=entry["reply_content"],
            custom_fields={
                "solution": entry["reply_content"],
                "reply_method": "回复客户",
                "issue_type_confirmed": entry.get("issue_type_confirmed", ""),
            },
            ai_fields=entry.get("ai_fields", {}),
        )
        jira_success = bool(result.get("success"))
        message = result.get("message", "")
    except Exception as exc:  # noqa: BLE001
        message = f"jira call raised: {exc}"
        print(f"[pending_approval_store] approve {approval_id} jira error: {exc}")

    with _lock:
        _append_record({
            "type": "approved",
            "approval_id": approval_id,
            "ts": _now_iso(),
            "approver": approver,
            "jira_success": jira_success,
        })

    return {"success": jira_success, "issue_key": issue_key, "message": message}


def reject(approval_id: str, approver: str = "operator", reason: str = "") -> None:
    """Append a rejected tombstone.  Does NOT call Jira."""
    with _lock:
        _append_record({
            "type": "rejected",
            "approval_id": approval_id,
            "ts": _now_iso(),
            "approver": approver,
            "reason": reason,
        })


def get_by_issue_key(issue_key: str) -> dict | None:
    """Return the current pending (non-tombstoned) record for issue_key, or None."""
    active = _active_pending_records()
    for rec in active:
        if rec.get("issue_key") == issue_key:
            return rec
    return None

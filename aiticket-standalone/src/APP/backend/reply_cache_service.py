"""
回复内容缓存服务
提供LLM生成回复的缓存机制，避免重复生成
"""

import os
import json
import threading
from typing import Optional, Dict
from datetime import datetime, timedelta

# 项目根目录（demo 沙箱可通过 DEMO_RUNTIME_DIR 重定向 data_cache）
BASE_DIR = os.environ.get("DEMO_RUNTIME_DIR") or os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

CACHE_FILE = os.path.join(BASE_DIR, "data_cache", "reply_cache.json")
CACHE_TTL_DAYS = 7  # 缓存有效期7天

# 进程内写锁：防止 precompute 2 线程 + pregen 线程并发 read-modify-write 丢失数据
_cache_write_lock = threading.Lock()


def _get_cache_key(issue_key: str, analysis_hash: str = "") -> str:
    """生成缓存键（只用工单号，与ai_analysis内容解耦，TTL控制时效性）"""
    return issue_key


def _is_entry_valid(entry: dict) -> bool:
    """检查 entry 是否在 TTL 内"""
    try:
        cached_time = datetime.fromisoformat(entry['timestamp'])
        return datetime.now() - cached_time <= timedelta(days=CACHE_TTL_DAYS)
    except Exception:
        return False


def get_cached_reply(issue_key: str, ai_analysis: Dict) -> Optional[str]:
    """
    获取缓存的回复内容

    Returns:
        缓存的回复内容，不存在或过期返回None
    """
    entry = get_cached_reply_entry(issue_key, ai_analysis)
    if entry is None:
        return None
    return entry.get('reply_content')


def get_cached_reply_entry(issue_key: str, ai_analysis: Dict) -> Optional[Dict]:
    """
    获取完整的缓存 entry（含所有持久化字段）。

    Returns:
        完整 entry dict，不存在或过期返回 None
    """
    if not os.path.exists(CACHE_FILE):
        return None

    try:
        cache_key = _get_cache_key(issue_key)

        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)

        entry = cache.get(cache_key)
        if not entry:
            return None

        if not _is_entry_valid(entry):
            return None

        return entry

    except Exception as e:
        print(f"[ReplyCache] 读取缓存失败: {e}")
        return None


def save_cached_reply(issue_key: str, ai_analysis: Dict, reply_content: str,
                      extra_fields: Optional[Dict] = None):
    """
    保存回复内容到缓存。

    Args:
        issue_key: 工单编号
        ai_analysis: AI分析结果（保留参数但不参与缓存键计算）
        reply_content: 生成的回复内容
        extra_fields: 可选的附加字段（grounded_confidence / kb_hits_scored /
                      similar_issues_scored / reply_strategy / reply_gateway /
                      suggested_reply_method / suggested_issue_type 等）
    """
    with _cache_write_lock:
        _save_cached_reply_locked(issue_key, reply_content, extra_fields)


def _save_cached_reply_locked(issue_key: str, reply_content: str,
                               extra_fields: Optional[Dict]):
    """内部写入实现，调用方已持锁。"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

        cache = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)

        cache_key = _get_cache_key(issue_key)

        entry = {
            'issue_key': issue_key,
            'reply_content': reply_content,
            'timestamp': datetime.now().isoformat(),
        }
        if extra_fields:
            entry.update(extra_fields)

        cache[cache_key] = entry

        _cleanup_expired_cache(cache)

        tmp = CACHE_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_FILE)

    except Exception as e:
        print(f"[ReplyCache] 保存缓存失败: {e}")


def merge_legacy_cache(legacy_path: str) -> int:
    """
    将旧路径的 cache 文件合并进活跃 cache（启动时一次性调用）。
    合并后将旧文件改名为 .legacy。
    返回合并的新增/更新条目数。
    """
    if not os.path.exists(legacy_path) or os.path.exists(legacy_path + ".legacy"):
        return 0

    with _cache_write_lock:
        try:
            with open(legacy_path, 'r', encoding='utf-8') as f:
                legacy = json.load(f)

            current = {}
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                    current = json.load(f)

            merged = 0
            for k, v in legacy.items():
                if not isinstance(v, dict):
                    continue
                if k not in current or current[k].get("timestamp", "") < v.get("timestamp", ""):
                    current[k] = v
                    merged += 1

            if merged > 0:
                os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
                tmp = CACHE_FILE + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(current, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CACHE_FILE)

            os.rename(legacy_path, legacy_path + ".legacy")
            return merged

        except Exception as e:
            print(f"[ReplyCache] 合并 legacy cache 失败: {e}")
            return 0


def _cleanup_expired_cache(cache: Dict):
    """清理过期缓存条目"""
    expired_keys = []
    now = datetime.now()

    for key, entry in cache.items():
        try:
            cached_time = datetime.fromisoformat(entry['timestamp'])
            if now - cached_time > timedelta(days=CACHE_TTL_DAYS):
                expired_keys.append(key)
        except Exception:
            expired_keys.append(key)

    for key in expired_keys:
        del cache[key]

    # 限制缓存大小（最多保留500条）
    if len(cache) > 500:
        sorted_items = sorted(
            cache.items(),
            key=lambda x: x[1].get('timestamp', ''),
            reverse=True
        )
        cache.clear()
        cache.update(dict(sorted_items[:500]))

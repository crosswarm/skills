#!/usr/bin/env python3
"""
KB 知识库批量编译器 — 分批编译 + 飞书进度汇报
每编译 BATCH_SIZE 个话题，通过飞书推送一次进度。
全量完成后推送最终汇报。

用法：
  python batch_compile_kb.py                  # 编译全部未编译话题
  python batch_compile_kb.py --batch 5        # 每批 5 个
  python batch_compile_kb.py --dry-run        # 只列出待编译话题，不执行
  python batch_compile_kb.py --force          # 强制重编译全部话题（忽略已编译记录）
  python batch_compile_kb.py --changed-only   # 仅编译有变更的话题（比对 manifest mtime）
"""
import sys, os, json, time, argparse, requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, BACKEND_DIR)

# 确保 localhost 请求不走 HTTP 代理（与 main.py 一致的 no_proxy 修复）
for _h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
    _existing = os.environ.get("no_proxy", "")
    if _h not in _existing:
        os.environ["no_proxy"] = f"{_existing},{_h}".strip(",")
os.environ["NO_PROXY"] = os.environ.get("no_proxy", "")

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:3000")
FEISHU_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_72ef8553bb8b552435cd91b0fb1e86ab")
KB_PAGE_URL = os.environ.get("KB_PAGE_URL", "http://100.80.80.100:3000/kb.html")


def get_all_topics():
    """从 manifest 获取所有话题名称"""
    r = requests.get(f"{API_BASE}/api/kb/manifest", timeout=30)
    r.raise_for_status()
    topics = r.json().get("topics", [])
    # 扁平化
    flat = []
    def flatten(ts):
        for t in ts:
            flat.append(t["name"])
            if t.get("children"):
                flatten(t["children"])
    flatten(topics)
    return flat


def get_compiled_names():
    """获取已编译的话题名称集合"""
    r = requests.get(f"{API_BASE}/api/kb/compiled?top_k=500", timeout=15)
    r.raise_for_status()
    items = r.json().get("items", [])
    return {(i.get("name", "").replace("综合解析：", "").replace("综合解析:", "")) for i in items}


def compile_batch(topics):
    """编译一批话题，直接调用编译服务（绕过异步 job 队列），返回结果"""
    from kb_compile_service import get_or_create_compile_service
    svc = get_or_create_compile_service()

    compiled = 0
    for topic in topics:
        try:
            result = svc.compile_topic(topic, skip_bip_validation=True)
            if result and result.get("content_id"):
                compiled += 1
                print(f"  ✓ {topic}")
            elif result and result.get("bip_judgment_status") == "pending_user":
                print(f"  ~ {topic}: pending BIP review")
            else:
                print(f"  . {topic}: no chunks found")
        except ValueError as ve:
            if "TOPIC_REJECTED" in str(ve):
                print(f"  ✗ {topic}: rejected")
            else:
                print(f"  ! {topic}: {ve}")
        except Exception as e:
            print(f"  ! {topic}: {e}")

    return {"compiled": compiled}


def push_feishu(message):
    """通过后端飞书通道推送消息"""
    try:
        # 使用 feishu_notifier 或直接调 OpenClaw 的飞书 API
        from services.feishu_notifier import FeishuNotifier
        notifier = FeishuNotifier()
        notifier.send_message(message, chat_id=FEISHU_CHAT_ID)
        print(f"[飞书] 已推送")
    except Exception as e:
        print(f"[飞书] 推送失败，尝试备用方式: {e}")
        try:
            # 备用：通过后端 API
            requests.post(
                f"{API_BASE}/api/feishu/send-text",
                json={"chat_id": FEISHU_CHAT_ID, "text": message},
                timeout=10,
            )
        except Exception:
            print(f"[飞书] 备用推送也失败，仅控制台输出")


def get_compiled_timestamps():
    """返回 {话题名: created_at_isostring} 的已编译话题时间戳映射（直连 DB）。"""
    import sqlite3
    db_candidates = [
        os.path.join(SCRIPT_DIR, '..', '..', '..', 'data', 'sqlite', 'kb_chunks.db'),
        os.path.join(SCRIPT_DIR, '..', 'data', 'sqlite', 'kb_chunks.db'),
    ]
    for db_path in db_candidates:
        db_path = os.path.normpath(db_path)
        if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT name, created_at FROM documents WHERE source_kind='kb_compiled'"
                ).fetchall()
                return {
                    row[0].replace("综合解析：", "").replace("综合解析:", ""): row[1]
                    for row in rows if row[0]
                }
            except Exception:
                pass
            finally:
                conn.close()
    return {}


def get_manifest_mtimes():
    """返回 {top_category: max_mtime_isostring}，从 manifest.json 计算各域最新源文件时间。"""
    import datetime
    manifest_candidates = [
        os.path.join(SCRIPT_DIR, '..', '..', '..', 'KB', 'INDEX', 'manifest.json'),
    ]
    for path in manifest_candidates:
        path = os.path.normpath(path)
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                manifest = json.load(f)
            domain_mtimes = {}
            for item in manifest.get('contents', {}).values():
                domain = item.get('top_category', '')
                src = item.get('source_path', '')
                if not domain or not src:
                    continue
                abs_src = os.path.normpath(os.path.join(SCRIPT_DIR, '..', '..', '..', src))
                if os.path.exists(abs_src):
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(abs_src)).isoformat()
                    if domain not in domain_mtimes or mtime > domain_mtimes[domain]:
                        domain_mtimes[domain] = mtime
            return domain_mtimes
    return {}


def main():
    parser = argparse.ArgumentParser(description="KB 批量编译")
    parser.add_argument("--batch", type=int, default=10, help="每批编译数量")
    parser.add_argument("--dry-run", action="store_true", help="只列出待编译话题，不执行")
    parser.add_argument("--force", action="store_true", help="强制重编译全部话题（忽略已编译记录）")
    parser.add_argument("--changed-only", action="store_true", dest="changed_only",
                        help="仅编译有变更的话题（源文件 mtime 晚于已编译时间戳）")
    args = parser.parse_args()

    print(f"[KB编译] 加载话题列表...")
    all_topics = get_all_topics()
    compiled = get_compiled_names()

    if args.force:
        remaining = list(all_topics)
        print(f"[KB编译] --force 模式：全量重编译 {len(remaining)} 个话题")
    elif args.changed_only:
        ts_map = get_compiled_timestamps()
        mtime_map = get_manifest_mtimes()
        remaining = []
        for t in all_topics:
            compiled_at = ts_map.get(t)
            if compiled_at is None:
                remaining.append(t)  # 未编译过
            elif mtime_map.get(t, "") > compiled_at:
                remaining.append(t)  # 源文件更新
        print(f"[KB编译] --changed-only 模式：{len(remaining)} 个话题有变更")
    else:
        remaining = [t for t in all_topics if t not in compiled]

    print(f"[KB编译] 总话题: {len(all_topics)}, 已编译: {len(compiled)}, 待编译: {len(remaining)}")

    if not remaining:
        print("[KB编译] 全部已编译完成！")
        push_feishu(f"✅ KB 知识库编译全部完成\n已编译 {len(compiled)} 个话题\n📖 查看: {KB_PAGE_URL}")
        return

    if args.dry_run:
        print("待编译话题：")
        for i, t in enumerate(remaining, 1):
            print(f"  {i}. {t}")
        return

    total_batches = (len(remaining) + args.batch - 1) // args.batch
    compiled_this_run = 0
    errors = []

    for batch_idx in range(0, len(remaining), args.batch):
        batch = remaining[batch_idx:batch_idx + args.batch]
        batch_num = batch_idx // args.batch + 1
        print(f"\n[KB编译] 批次 {batch_num}/{total_batches}: {batch}")

        try:
            t0 = time.time()
            result = compile_batch(batch)
            elapsed = time.time() - t0
            compiled_count = result.get("compiled", 0)
            compiled_this_run += compiled_count

            total_done = len(compiled) + compiled_this_run
            progress_pct = round(total_done / max(len(all_topics), 1) * 100, 1)

            print(f"[KB编译] 批次 {batch_num} 完成: +{compiled_count} ({elapsed:.0f}s)")

            # 飞书汇报
            msg = (
                f"📚 KB 编译进度 — 批次 {batch_num}/{total_batches}\n"
                f"本批: {', '.join(batch)}\n"
                f"进度: {total_done}/{len(all_topics)} ({progress_pct}%)\n"
                f"耗时: {elapsed:.0f}s\n"
                f"📖 查看: {KB_PAGE_URL}"
            )
            push_feishu(msg)

        except Exception as e:
            print(f"[KB编译] 批次 {batch_num} 失败: {e}")
            errors.append({"batch": batch_num, "topics": batch, "error": str(e)})

        # 批次间隔（避免 LLM 限流）
        if batch_idx + args.batch < len(remaining):
            time.sleep(5)

    # 最终汇报
    final_compiled = get_compiled_names()
    msg = (
        f"✅ KB 知识库编译完成\n"
        f"总话题: {len(all_topics)}\n"
        f"已编译: {len(final_compiled)}\n"
        f"本次新增: {compiled_this_run}\n"
        f"失败批次: {len(errors)}\n"
        f"📖 查看: {KB_PAGE_URL}"
    )
    if errors:
        msg += f"\n⚠️ 失败详情: {json.dumps([e['topics'] for e in errors], ensure_ascii=False)}"
    push_feishu(msg)
    print(f"\n[KB编译] 全部完成！已编译 {len(final_compiled)}/{len(all_topics)}")


if __name__ == "__main__":
    main()

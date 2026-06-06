#!/usr/bin/env python3
"""
Direct SQLite ingestion for kb_local items — skips ChromaDB.

Reads manifest.json, reads each converted .md file, chunks text, and writes
to kb_chunks.db (documents + chunks + chunks_fts tables). Safe to run while
the backend is live because it uses WAL + busy_timeout and only touches
kb_local source_kind.

Usage:
  conda run -n antigravity python APP/backend/scripts/ingest_kb_sqlite_only.py
"""
import sys, os, json, sqlite3, re, time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # APP/backend/scripts
BACKEND_DIR = SCRIPT_DIR.parent                       # APP/backend
PROJECT_ROOT = BACKEND_DIR.parent.parent              # repo root
sys.path.insert(0, str(BACKEND_DIR))

MANIFEST_PATH = PROJECT_ROOT / 'KB' / 'INDEX' / 'manifest.json'
DB_PATH = PROJECT_ROOT / 'data' / 'sqlite' / 'kb_chunks.db'
MAX_CHARS = 900
OVERLAP = 120


def chunk_text(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sections: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 <= MAX_CHARS:
            current = f"{current}\n{line}".strip()
            continue
        if current:
            sections.append(current)
        current = f"{current[-OVERLAP:]}\n{line}".strip() if current else line
    if current:
        sections.append(current)
    return sections or [text[:MAX_CHARS]]


def load_item_text(raw: dict) -> str:
    converted_path = raw.get('converted_path', '')
    if not converted_path:
        return ''
    abs_path = PROJECT_ROOT / converted_path
    if not abs_path.exists():
        return ''
    return abs_path.read_text(encoding='utf-8', errors='ignore').strip()


def main():
    print(f"[ingest] Project root: {PROJECT_ROOT}")
    print(f"[ingest] DB: {DB_PATH}")
    print(f"[ingest] Manifest: {MANIFEST_PATH}")

    if not MANIFEST_PATH.exists():
        print("[ingest] ERROR: manifest.json not found")
        sys.exit(1)

    manifest = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    contents = manifest.get('contents', {})
    print(f"[ingest] Manifest entries: {len(contents)}")

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Clear existing kb_local data
    print("[ingest] Clearing existing kb_local data...")
    conn.execute("DELETE FROM chunks WHERE source_kind='kb_local'")
    conn.execute("DELETE FROM documents WHERE source_kind='kb_local'")
    conn.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_kind='kb_local')")
    conn.commit()

    inserted_docs = 0
    inserted_chunks = 0
    skipped = 0
    t0 = time.time()

    for content_id, raw in contents.items():
        text = load_item_text(raw)
        if not text:
            skipped += 1
            continue

        source_kind = 'kb_local'
        name = raw.get('name', '')
        summary = raw.get('summary', '')
        # Use source_path (KB/公式/...) so domain SQL LIKE '%/公式/%' matches correctly
        source_rel_path = raw.get('source_path', raw.get('source_rel_path', ''))
        citation_label = f"[KB] {source_rel_path}"
        l1_module = raw.get('top_category', '')
        l2_module = raw.get('second_category', '')
        doc_type = ''
        project_key = '_global'
        keywords = ' '.join(raw.get('keywords', []))

        try:
            conn.execute(
                """INSERT OR REPLACE INTO documents (
                    content_id, source_kind, name, summary, source_rel_path,
                    citation_label, l1_module, l2_module, doc_type, project_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content_id, source_kind, name, summary, source_rel_path,
                 citation_label, l1_module, l2_module, doc_type, project_key)
            )
            inserted_docs += 1
        except Exception as e:
            print(f"[ingest] doc insert error {content_id}: {e}")
            continue

        chunks = chunk_text(text)
        for idx, chunk in enumerate(chunks, start=1):
            chunk_id = f"{content_id}::chunk-{idx:03d}"
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO chunks (
                        chunk_id, content_id, chunk_index, chunk_text, chunk_preview,
                        source_kind, name, summary, source_rel_path, citation_label,
                        l1_module, l2_module, doc_type, project_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (chunk_id, content_id, idx, chunk, chunk[:240],
                     source_kind, name, summary, source_rel_path, citation_label,
                     l1_module, l2_module, doc_type, project_key)
                )
                conn.execute(
                    """INSERT OR REPLACE INTO chunks_fts (
                        chunk_id, name, summary, keywords, source_rel_path, chunk_text
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (chunk_id, name, summary, keywords, source_rel_path, chunk)
                )
                inserted_chunks += 1
            except Exception as e:
                print(f"[ingest] chunk insert error {chunk_id}: {e}")

        if inserted_docs % 100 == 0:
            conn.commit()
            elapsed = time.time() - t0
            print(f"[ingest] Progress: {inserted_docs}/{len(contents)} docs, {inserted_chunks} chunks ({elapsed:.0f}s)")

    conn.commit()
    conn.close()

    elapsed = time.time() - t0
    print(f"\n[ingest] Done in {elapsed:.1f}s")
    print(f"[ingest] Documents: {inserted_docs}, Chunks: {inserted_chunks}, Skipped: {skipped}")


if __name__ == '__main__':
    main()

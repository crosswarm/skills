#!/usr/bin/env python3
"""KB 数据库 Schema 升级：新增可信度/热度/验证等字段（幂等执行）"""
import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'sqlite', 'kb_chunks.db')

COLUMNS = [
    ("credibility", "REAL DEFAULT 0.7"),
    ("last_validated_at", "TEXT"),
    ("citation_count", "INTEGER DEFAULT 0"),
    ("contradiction_flags", "TEXT DEFAULT '[]'"),
    ("heat_score", "REAL DEFAULT 0.0"),
    ("merged_from", "TEXT DEFAULT '[]'"),
    ("validation_sources", "TEXT DEFAULT '[]'"),
]

def upgrade():
    db = os.path.abspath(DB_PATH)
    print(f"DB: {db}")
    conn = sqlite3.connect(db)
    for col_name, col_type in COLUMNS:
        try:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col_name} {col_type}")
            print(f"  ✓ 新增 documents.{col_name} {col_type}")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print(f"  - 已存在 documents.{col_name}")
            else:
                print(f"  ✗ {col_name}: {e}")
    conn.commit()
    # 验证
    cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
    for col_name, _ in COLUMNS:
        assert col_name in cols, f"字段 {col_name} 未找到！"
    print(f"\n✅ 全部 {len(COLUMNS)} 个字段已就绪（共 {len(cols)} 列）")
    conn.close()

if __name__ == "__main__":
    upgrade()

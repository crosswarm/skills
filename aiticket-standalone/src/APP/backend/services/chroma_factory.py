"""Chroma 客户端工厂。

通过环境变量 CHROMA_MODE 决定连接方式：
  - persistent（默认）: 各进程直接打开本地文件目录（单 worker 模式）
  - server: 连接独立 Chroma daemon（多 worker 安全）

服务器模式端口：
  CHROMA_KB_PORT   (默认 8001) → data/chroma_kb (kb_hybrid_index)
  CHROMA_BOARD_PORT (默认 8002) → APP/backend/chroma_db (vector_store + product_facts)
"""
import os
try:
    import chromadb
    from chromadb.config import Settings
    _CHROMADB_AVAILABLE = True
except ImportError:
    chromadb = None
    Settings = None
    _CHROMADB_AVAILABLE = False


def get_chroma_client(persist_path: str | None = None):
    """返回 Chroma 客户端。

    Args:
        persist_path: 本地目录路径（persistent 模式使用；server 模式忽略）。
    """
    if not _CHROMADB_AVAILABLE:
        raise ImportError("chromadb is not installed. Vector store features are unavailable in core mode.")
    mode = os.environ.get("CHROMA_MODE", "persistent")
    if mode == "server":
        # 根据路径 hint 选择端口
        if persist_path and "chroma_kb" in str(persist_path):
            port = int(os.environ.get("CHROMA_KB_PORT", "8001"))
        else:
            port = int(os.environ.get("CHROMA_BOARD_PORT", "8002"))
        host = os.environ.get("CHROMA_HOST", "127.0.0.1")
        return chromadb.HttpClient(
            host=host,
            port=port,
            settings=Settings(anonymized_telemetry=False),
        )

    if persist_path is None:
        raise ValueError("persistent 模式必须提供 persist_path")
    return chromadb.PersistentClient(
        path=str(persist_path),
        settings=Settings(anonymized_telemetry=False, allow_reset=True),
    )

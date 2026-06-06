"""
基于Chroma的语义搜索引擎
替代原有的TF-IDF方案，提供更准确的语义相似度搜索
"""

from typing import List, Dict, Optional, Any
import os
from vector_store import VectorStore

# 全局向量存储实例（懒加载）
_vector_store = None

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, "../.."))

def get_vector_store(api_key: str = None, allow_download: bool = False) -> VectorStore:
    """获取全局向量存储实例

    Args:
        api_key: LLM API密钥
        allow_download: 是否允许下载嵌入模型（默认False，服务器部署时跳过下载）
    """
    global _vector_store
    if _vector_store is None:
        persist_dir = os.path.join(BASE_DIR, "chroma_db")
        _vector_store = VectorStore(persist_directory=persist_dir, api_key=api_key, allow_download=allow_download)
    return _vector_store


class SemanticSearchEngine:
    """
    语义搜索引擎 - 基于Chroma向量数据库
    
    功能：
    1. 从conclusion和src目录加载数据到向量库
    2. 提供语义相似度搜索（替代TF-IDF）
    3. 支持增量更新
    4. 与原有SearchEngine接口兼容
    """
    
    def __init__(self, api_key: str = None, allow_download: bool = False):
        self.vector_store = get_vector_store(api_key, allow_download)
        # 后台线程加载历史数据，避免阻塞 uvicorn 启动（12K+ 条目需要数分钟）
        import threading
        threading.Thread(target=self._init_from_legacy_data, daemon=True).start()
    
    def _init_from_legacy_data(self):
        """从原有数据源初始化向量库"""
        # 检查是否已有数据
        stats = self.vector_store.get_stats()
        if stats['issues_count'] > 0:
            print(f"[SemanticSearch] 向量库已有 {stats['issues_count']} 条记录，跳过初始化")
            return

        # 检查是否有可用的embedding函数
        if self.vector_store.embedding_func is None:
            print("[SemanticSearch] 警告: 没有可用的embedding函数，跳过数据加载")
            return

        print("[SemanticSearch] 正在从数据源初始化向量库...")

        # 从conclusion/index.md加载
        self._load_conclusion_data()

        # 从src目录加载
        self._load_src_data()

        stats = self.vector_store.get_stats()
        print(f"[SemanticSearch] 初始化完成，共 {stats['issues_count']} 条记录")
    
    def _load_conclusion_data(self):
        """加载conclusion目录的已分析工单"""
        conclusion_dir = os.path.join(PROJECT_ROOT, "conclusion")
        index_path = os.path.join(conclusion_dir, "index.md")
        
        if not os.path.exists(index_path):
            return
        
        import re
        
        with open(index_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # 解析index.md获取工单列表
        issues_to_add = []
        start_parsing = False
        
        for line in lines:
            if "| --- |" in line:
                start_parsing = True
                continue
            if not start_parsing:
                continue
            
            parts = [p.strip() for p in line.strip().split("|")]
            if len(parts) >= 5:
                issue_key = parts[1]
                summary = parts[2]
                
                issues_to_add.append({
                    'key': issue_key,
                    'summary': summary,
                    'description': summary,  # conclusion 表格无独立描述，用 summary 填充以改善嵌入覆盖
                    'source': 'conclusion'
                })
        
        if issues_to_add:
            self.vector_store.batch_add_issues(issues_to_add)
    
    def _load_src_data(self):
        """加载src目录的原始工单数据"""
        src_dir = os.path.join(PROJECT_ROOT, "src")
        if not os.path.exists(src_dir):
            return
        
        import pandas as pd
        
        issues_to_add = []
        
        for filename in os.listdir(src_dir):
            if filename.endswith(".csv"):
                try:
                    df = pd.read_csv(os.path.join(src_dir, filename))
                    for _, row in df.iterrows():
                        if '问题关键字' in row and pd.notna(row['问题关键字']):
                            issues_to_add.append({
                                'key': str(row['问题关键字']),
                                'summary': str(row.get('概要', '')),
                                'description': str(row.get('详细描述', '')),
                                'source': 'src'
                            })
                except:
                    pass
        
        if issues_to_add:
            self.vector_store.batch_add_issues(issues_to_add)
    
    def search(self, query: str, top_k: int = 5,
               min_score: float = 0.3) -> Dict[str, Any]:
        """
        语义搜索相似工单
        
        兼容原有SearchEngine.search()接口
        
        Returns:
            {
                "type": "conclusion" | "mixed" | "none",
                "results": [
                    {
                        "key": str,
                        "display_summary": str,
                        "score": float,
                        "content": str,
                        "source": str
                    },
                    ...
                ]
            }
        """
        if not query or not query.strip():
            return {"type": "none", "results": []}
        
        # 使用向量库搜索
        results = self.vector_store.search_similar_issues(
            query=query,
            top_k=top_k,
            min_score=min_score
        )
        
        if not results:
            return {"type": "none", "results": []}
        
        # 格式化输出（兼容原有格式）
        formatted_results = []
        for r in results:
            formatted_results.append({
                "key": r['issue_key'],
                "display_summary": r['summary'],
                "score": r['score'],
                "search_text": f"{r['issue_key']} {r['summary']}",
                "content": r.get('document', ''),
                "full_text": r.get('document', ''),
                "source": r['metadata'].get('source', 'unknown'),
                "topic_file": r['metadata'].get('topic_file', ''),
                "metadata": r['metadata']
            })
        
        # 判断类型（兼容原有逻辑）
        best_score = formatted_results[0]['score']
        best_source = formatted_results[0]['source']
        
        if best_score > 0.7 and best_source == 'conclusion':
            result_type = "conclusion"
        else:
            result_type = "mixed"
        
        return {
            "type": result_type,
            "results": formatted_results
        }
    
    def search_with_cache(self, query: str, issue_key: str = None) -> Dict:
        """
        搜索并尝试复用历史分析结果
        
        用于看板场景的优化：先查缓存，再查相似度复用，最后才调用LLM
        """
        # 1. 如果指定了工单编号，先查分析缓存
        if issue_key:
            cached = self.vector_store.get_cached_analysis(issue_key)
            if cached and not cached.get('stale'):
                return {
                    "type": "cached",
                    "results": [],
                    "analysis": cached
                }
        
        # 2. 语义搜索相似工单
        search_results = self.search(query, top_k=5, min_score=0.6)
        
        # 3. 尝试复用相似工单的分析结果
        if search_results['results']:
            best_match = search_results['results'][0]
            if best_match['score'] > 0.9:
                # 相似度>0.9，尝试复用其分析结果
                reused = self.vector_store.get_cached_analysis(best_match['key'])
                if reused:
                    reused['is_reused'] = True
                    reused['reused_from'] = best_match['key']
                    reused['reused_similarity'] = best_match['score']
                    return {
                        "type": "reused",
                        "results": search_results['results'],
                        "analysis": reused
                    }
        
        return {
            "type": search_results['type'],
            "results": search_results['results'],
            "analysis": None
        }
    
    def add_issue(self, issue_key: str, summary: str, description: str = "", 
                  **metadata) -> bool:
        """
        添加新工单到搜索索引
        
        用于：当Jira新增工单时，实时加入向量库
        """
        return self.vector_store.add_issue(
            issue_key=issue_key,
            summary=summary,
            description=description,
            metadata=metadata
        )
    
    def cache_analysis(self, issue_key: str, analysis: Dict, summary: str = ""):
        """缓存AI分析结果"""
        self.vector_store.cache_analysis(issue_key, analysis, summary)
    
    def get_cached_analysis(self, issue_key: str) -> Optional[Dict]:
        """获取缓存的AI分析结果"""
        return self.vector_store.get_cached_analysis(issue_key)
    
    def reload_data(self):
        """
        重新加载数据（兼容原有接口）
        
        实际行为：增量添加新数据，不删除已有向量
        """
        print("[SemanticSearch] 增量更新向量库...")
        self._load_conclusion_data()
        self._load_src_data()
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.vector_store.get_stats()


# 保持与原有代码兼容的别名
SearchEngine = SemanticSearchEngine

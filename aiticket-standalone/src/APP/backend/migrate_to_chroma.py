"""
迁移脚本：从原有系统迁移到Chroma向量数据库

执行步骤：
1. 加载原有conclusion和src数据
2. 生成向量嵌入并存储到Chroma
3. 迁移现有的board_ai_cache.json到Chroma
4. 验证数据完整性
"""

import os
import json
import sys
from datetime import datetime
from typing import List, Dict

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(__file__))

from vector_store import VectorStore


def migrate_from_cache_json(vector_store: VectorStore, cache_file: str = "board_ai_cache.json"):
    """从旧的JSON缓存迁移到Chroma"""
    if not os.path.exists(cache_file):
        print(f"[Migrate] 未找到缓存文件 {cache_file}")
        return 0
    
    print(f"[Migrate] 正在从 {cache_file} 迁移数据...")
    
    with open(cache_file, 'r', encoding='utf-8') as f:
        cache_data = json.load(f)
    
    count = 0
    for issue_key, analysis in cache_data.items():
        try:
            # 转换为新的格式
            vector_store.cache_analysis(
                issue_key=issue_key,
                analysis=analysis,
                summary=analysis.get('functionality_impact', ''),
                ttl_days=30
            )
            count += 1
        except Exception as e:
            print(f"[Migrate Error] {issue_key}: {e}")
    
    print(f"[Migrate] 成功迁移 {count} 条缓存记录")
    return count


def migrate_conclusion_index(vector_store: VectorStore, conclusion_dir: str = "../../conclusion"):
    """从conclusion/index.md迁移工单数据"""
    index_path = os.path.join(conclusion_dir, "index.md")
    
    if not os.path.exists(index_path):
        print(f"[Migrate] 未找到 {index_path}")
        return 0
    
    print(f"[Migrate] 正在从结论目录迁移工单...")
    
    with open(index_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
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
        vector_store.batch_add_issues(issues_to_add)
    
    print(f"[Migrate] 成功迁移 {len(issues_to_add)} 条结论工单")
    return len(issues_to_add)


def migrate_src_data(vector_store: VectorStore, src_dir: str = "../../src"):
    """从src目录迁移原始工单数据"""
    if not os.path.exists(src_dir):
        print(f"[Migrate] 未找到 {src_dir}")
        return 0
    
    print(f"[Migrate] 正在从SRC目录迁移原始工单...")
    
    import pandas as pd
    
    count = 0
    for filename in os.listdir(src_dir):
        if filename.endswith(".csv"):
            try:
                df = pd.read_csv(os.path.join(src_dir, filename))
                issues_to_add = []
                
                for _, row in df.iterrows():
                    if '问题关键字' in row and pd.notna(row['问题关键字']):
                        issues_to_add.append({
                            'key': str(row['问题关键字']),
                            'summary': str(row.get('概要', '')),
                            'description': str(row.get('详细描述', '')),
                            'source': 'src'
                        })
                
                if issues_to_add:
                    vector_store.batch_add_issues(issues_to_add)
                    count += len(issues_to_add)
                    
            except Exception as e:
                print(f"[Migrate Error] {filename}: {e}")
    
    print(f"[Migrate] 成功迁移 {count} 条SRC工单")
    return count


def verify_migration(vector_store: VectorStore):
    """验证迁移结果"""
    stats = vector_store.get_stats()
    
    print("\n" + "="*50)
    print("迁移验证报告")
    print("="*50)
    print(f"工单向量数量: {stats['issues_count']}")
    print(f"分析缓存数量: {stats['analysis_count']}")
    print(f"相似度边数量: {stats['similarity_edges']}")
    
    # 测试搜索
    print("\n测试搜索功能...")
    results = vector_store.search_similar_issues(
        query="流程审批报错",
        top_k=3,
        min_score=0.5
    )
    print(f"  搜索 '流程审批报错' 返回 {len(results)} 条结果")
    for r in results:
        print(f"    - {r['issue_key']}: {r['summary'][:50]}... (相似度: {r['score']:.2f})")
    
    print("\n" + "="*50)


def main():
    """主迁移流程"""
    print("="*50)
    print("Chroma 向量数据库迁移工具")
    print("="*50)
    
    # 初始化向量存储
    persist_dir = os.path.join(os.path.dirname(__file__), "chroma_db")
    print(f"\n数据目录: {persist_dir}")
    
    api_key = os.environ.get("GEMINI_API_KEY", None)
    vector_store = VectorStore(persist_directory=persist_dir, api_key=api_key)
    
    # 显示当前状态
    stats = vector_store.get_stats()
    print(f"\n当前状态:")
    print(f"  工单向量: {stats['issues_count']}")
    print(f"  分析缓存: {stats['analysis_count']}")
    
    # 询问是否继续
    if stats['issues_count'] > 0:
        print("\n向量库已有数据，跳过迁移。如需重新迁移，请删除 chroma_db 目录。")
        verify_migration(vector_store)
        return
    
    # 执行迁移
    print("\n开始迁移...")
    
    # 1. 迁移工单数据
    count1 = migrate_conclusion_index(vector_store)
    count2 = migrate_src_data(vector_store)
    
    # 2. 迁移AI缓存
    count3 = migrate_from_cache_json(vector_store)
    
    # 验证
    verify_migration(vector_store)
    
    print(f"\n✅ 迁移完成！总计迁移 {count1 + count2} 条工单，{count3} 条分析缓存")


if __name__ == "__main__":
    main()

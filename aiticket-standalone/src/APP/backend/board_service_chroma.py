"""
看板服务 - Chroma优化版

核心优化：
1. 使用语义搜索替代关键词匹配，提升相似工单召回准确率
2. 向量数据库存储AI分析结果，支持语义相似的建议复用
3. 批量分析优化，利用向量相似度预筛选可复用结果
4. 异步队列 + Chroma持久化，提升整体吞吐量
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
import concurrent.futures
import json
import os
import threading
import queue
from pathlib import Path
import time
from dataclasses import dataclass, asdict

# 项目根目录（demo 沙箱可通过 DEMO_RUNTIME_DIR 重定向 data_cache / chroma_db）
BASE_DIR = os.environ.get("DEMO_RUNTIME_DIR") or os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

# 新的依赖
from vector_store import VectorStore
from search_chroma import SemanticSearchEngine
from llm_service import LLMService
from reply_cache_service import get_cached_reply, get_cached_reply_entry, save_cached_reply
from reply_trainer import ReplyTrainer, STYLE_RULES_FILE
from kb_runtime_service import KnowledgeRuntimeService

# Jira服务（保持原有）
from jira_service import jira_service, JiraIssue, JiraService


def load_file_cached_analysis(issue_key: str, project_root: str = PROJECT_ROOT) -> Optional[Dict]:
    """
    从文件缓存加载分析结果（模块级工具函数）

    提取为通用函数，避免在AIAnalysisWorker和BoardService中重复定义。

    Args:
        issue_key: 工单编号
        project_root: 项目根目录路径

    Returns:
        分析结果字典，如果不存在则返回None
    """
    cache_file = os.path.join(BASE_DIR, "data_cache", "analysis_cache.json")

    if not os.path.exists(cache_file):
        return None

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
        if not raw:
            return None
        cache = json.loads(raw)
        return cache.get(issue_key)
    except Exception as e:
        print(f"[CacheLoader] 加载文件缓存失败: {e}")
        return None


@dataclass
class BoardIssue:
    """看板展示的工单数据结构"""
    key: str
    summary: str
    status: str
    assignee: str
    reporter: str
    due_date: str
    created_date: str
    priority: str
    customer: str = ""
    description: str = ""
    contact_name: str = ""  # 联系人 (customfield_10404)
    contact_info: str = ""  # 联系方式 (customfield_10405)
    # AI分析结果
    ai_analysis: Optional[Dict] = None
    ai_status: str = "pending"  # pending/analyzing/completed/failed
    

class AIAnalysisWorker:
    """
    后台AI分析工作器 - 利用Chroma向量特性优化

    优化点：
    1. 批量获取待分析工单
    2. 先查向量库相似度，>0.9的直接复用
    3. 剩余工单批量调用LLM
    4. 结果写入向量缓存
    """

    def __init__(self, vector_store: VectorStore, llm_service: LLMService,
                 batch_size: int = 5, max_workers: int = 2, max_queue_size: int = 1000):
        self.vector_store = vector_store
        self.llm_service = llm_service
        self.batch_size = batch_size
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size  # 队列大小限制，防止内存溢出

        # LLM配置（从BoardService同步）
        self.llm_config = {
            "provider": "zhipu",
            "api_key": "",
            "model_name": "glm-5",
            "base_url": ""
        }

        # 任务队列
        self.task_queue = queue.PriorityQueue()
        self.running = False
        self.worker_thread = None

        # 回调函数列表
        self.callbacks = []

        # 限流控制（QPM）
        self.last_api_call = 0
        self.min_interval = 6  # 10 QPM = 6秒间隔

        # 任务计数器，用于确保PriorityQueue元素可比较
        self._task_counter = 0

        # 待处理 key 集合，防止同一工单重复入队
        self._pending_keys: set = set()
    
    def start(self):
        """启动后台工作线程"""
        self.running = True
        self.worker_thread = threading.Thread(target=self._process_loop, daemon=True)
        self.worker_thread.start()
        print("[AIWorker] 后台分析工作器已启动")
    
    def stop(self):
        """停止工作线程"""
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=10)
    
    def submit(self, issue: JiraIssue, priority: int = 5, skip_reuse: bool = False):
        """
        提交分析任务

        优先级：
        - 1-3: 今天到期（紧急）
        - 4-6: 明天到期（高）
        - 7-9: 本周到期（中）
        - 10: 其他（低）

        Args:
            skip_reuse: 是否跳过复用逻辑（强制重新分析时使用）
        """
        # 去重保护：已在队列中的工单不重复入队
        if issue.key in self._pending_keys:
            return False  # 已在队列中

        # 检查是否已有有效缓存（仅当不跳过复用时）
        if not skip_reuse:
            cached = self.vector_store.get_cached_analysis(issue.key)
            if cached and not cached.get('stale'):
                return False  # 已有有效缓存，跳过（未入队）

        # 检查队列大小，防止内存溢出
        if self.task_queue.qsize() >= self.max_queue_size:
            print(f"[AIWorker] 队列已满({self.max_queue_size})，丢弃低优先级任务: {issue.key}")
            # 如果优先级较高(<=3)，尝试清理一些低优先级任务
            if priority <= 3:
                print(f"[AIWorker] 任务 {issue.key} 优先级高，尝试强制入队")
            else:
                return False  # 丢弃低优先级任务（未入队）

        # 使用计数器确保队列元素可比较 (priority, counter, issue, skip_reuse)
        self._task_counter += 1
        self._pending_keys.add(issue.key)
        self.task_queue.put((priority, self._task_counter, issue, skip_reuse))
        print(f"[AIWorker] 任务已提交: {issue.key} (优先级: {priority}, 跳过复用: {skip_reuse})")
        return True  # 已成功入队
    
    def on_complete(self, callback):
        """注册分析完成回调"""
        self.callbacks.append(callback)
    
    def _process_loop(self):
        """主处理循环"""
        while self.running:
            try:
                # 批量获取任务
                batch = self._collect_batch()
                if not batch:
                    time.sleep(1)
                    continue
                
                # 处理批量
                self._process_batch(batch)
                
            except Exception as e:
                print(f"[AIWorker Error] {e}")
                time.sleep(5)
    
    def _collect_batch(self) -> List[Tuple[JiraIssue, bool]]:
        """收集一批待处理任务，返回 (issue, skip_reuse) 元组列表"""
        batch = []
        timeout = time.time() + 2  # 最多等待2秒凑齐一批

        while len(batch) < self.batch_size and time.time() < timeout:
            try:
                priority, counter, issue, skip_reuse = self.task_queue.get(timeout=0.5)
                batch.append((priority, issue, skip_reuse))
            except queue.Empty:
                break
            except Exception as e:
                print(f"[AIWorker] 获取任务出错: {e}")
                break

        return [(issue, skip_reuse) for _, issue, skip_reuse in batch]
    
    def _process_batch(self, items: List[tuple]):
        """批量处理工单

        Args:
            items: [(issue, skip_reuse), ...] 元组列表
        """
        issues = [issue for issue, _ in items]
        print(f"[AIWorker] 处理批次: {[i.key for i in issues]}")
        # 从 pending_keys 中移除已出队的工单，允许后续重新入队
        for _issue in issues:
            self._pending_keys.discard(_issue.key)

        # 阶段1: 尝试复用已有分析（基于语义相似度）
        to_analyze = []
        for issue, skip_reuse in items:
            # 如果跳过复用，直接进入分析阶段
            if skip_reuse:
                print(f"[AIWorker] {issue.key} 强制重新分析，跳过复用检查")
                to_analyze.append(issue)
                continue

            try:
                analysis = self._try_reuse_analysis(issue)
                if analysis:
                    # 直接复用成功
                    self._save_and_notify(issue.key, analysis, issue_title=issue.summary, issue_description=(issue.description or '')[:1000])
                else:
                    to_analyze.append(issue)
            except Exception as e:
                print(f"[AIWorker] 复用分析失败 {issue.key}: {e}")
                to_analyze.append(issue)

        if not to_analyze:
            return

        # 阶段2: 批量LLM分析
        try:
            self._batch_llm_analyze(to_analyze)
        except Exception as e:
            print(f"[AIWorker] 批量LLM分析失败: {e}")
            # 确保所有工单都有降级结果
            for issue in to_analyze:
                try:
                    fallback = self._rule_based_analysis(issue)
                    self._save_and_notify(issue.key, fallback, issue_title=issue.summary, issue_description=(issue.description or '')[:1000])
                except Exception as e2:
                    print(f"[AIWorker] 降级分析也失败 {issue.key}: {e2}")
    
    def _try_reuse_analysis(self, issue: JiraIssue) -> Optional[Dict]:
        """
        尝试复用历史分析结果
        
        策略：
        1. 查精确缓存
        2. 查语义相似建议（向量搜索）
        3. 查相似工单关联
        """
        query = f"{issue.summary} {issue.description}"
        
        # 1. 查精确缓存
        cached = self.vector_store.get_cached_analysis(issue.key)
        if cached and not cached.get('stale'):
            return cached
        
        # 2. 语义相似建议复用
        reused = self.vector_store.find_reusable_analysis(
            query=query,
            min_confidence=0.8,
            min_suggestion_similarity=0.85
        )
        if reused:
            print(f"[Reuse] {issue.key} 复用 {reused.get('reused_from')} 的分析 (相似度: {reused.get('reused_similarity', 0):.2f})")
            return reused
        
        # 3. 查相似工单关联
        similar_issues = self.vector_store.search_similar_issues(
            query=query,
            top_k=1,
            min_score=0.92  # 更严格的阈值
        )
        if similar_issues:
            best = similar_issues[0]
            neighbor_analysis = self.vector_store.get_cached_analysis(best['issue_key'])
            if neighbor_analysis:
                neighbor_analysis['is_reused'] = True
                neighbor_analysis['reused_from'] = best['issue_key']
                neighbor_analysis['reused_similarity'] = best['score']
                return neighbor_analysis
        
        return None
    
    def _resolve_routed_llm_config(self, feature_key: str, user_id: str = None) -> dict:
        """按功能路由解析 LLM 配置（凭据优先用户级），resolve 失败时回退 self.llm_config。

        若路由返回 _blocked（用户级强制但未配凭据），原样返回该 dict（含 _blocked），
        由调用方决定阻断（A 类有 user）或跳过（B 类后台 user_id=None）。
        """
        try:
            from main import resolve_feature_llm_runtime
            cfg = resolve_feature_llm_runtime(feature_key, user_id=user_id or None)
            if cfg and cfg.get("_blocked"):
                return cfg
            if cfg and cfg.get("api_key"):
                return cfg
        except Exception as e:
            print(f"[AIWorker] 功能路由解析失败 {feature_key}: {e}")
        return self.llm_config

    def _call_llm_with_feature(self, feature_key: str, prompt: str, user_id: str = None) -> str:
        """按功能路由调用 LLM，返回响应文本；LLM 返回错误时抛出 RuntimeError。

        _blocked：B 类（user_id=None，后台）跳过该步并打日志，返回空串不崩；
                  A 类（有 user_id）抛 RuntimeError，由上层转结构化提示/4xx。
        """
        cfg = self._resolve_routed_llm_config(feature_key, user_id=user_id)
        if cfg.get("_blocked"):
            if user_id:
                raise RuntimeError(
                    f"LLM_BLOCKED:{feature_key}:feature_requires_user_llm"
                )
            print(f"[AIWorker] feature={feature_key} 系统兜底关闭且无用户凭据，跳过 LLM 步骤")
            return ""
        response = self.llm_service.call_llm(
            prompt,
            api_key=cfg.get("api_key", ""),
            provider=cfg.get("provider", "zhipu"),
            model_name=cfg.get("model_name", "glm-5"),
            base_url=cfg.get("base_url", ""),
        )
        if response.startswith("Error:"):
            raise RuntimeError(response)
        return response

    def _batch_llm_analyze(self, issues: List[JiraIssue], user_id: str = None):
        """多字段并发生成：5 字段各自走功能路由，失败 >= 3 个时降级到原单 prompt 实现。

        user_id 透传到各 feature 调用（凭据优先用户级）。后台 AI worker 无 user → None，
        受各 feature "允许系统兜底"开关约束（关则该字段跳过/降级，不崩）。
        """
        import concurrent.futures

        def _gen_field(feature_key: str, prompt: str):
            """单字段调用，返回 (feature_key, result_or_None)"""
            try:
                result = self._call_llm_with_feature(feature_key, prompt, user_id=user_id)
                return feature_key, result
            except Exception as e:
                print(f"[AIWorker] 字段 {feature_key} 生成失败: {e}")
                return feature_key, None

        for issue in issues:
            summary = issue.summary or ""
            description = (issue.description or "")[:500]

            field_tasks = [
                ("classification_analysis",
                 f"对工单进行分类分析，输出推荐团队和角色:\n{summary}\n{description}"),
                ("confidence_scoring",
                 f"评估分类置信度，输出0到1之间的数字:\n{summary}"),
                ("solution_recommendation",
                 f"推荐解决方案（80字内）:\n{summary}\n{description}"),
                ("reply_style_router",
                 f"判断适合的回复方式（formal/friendly/technical），只输出一个词:\n{summary}"),
                ("domain_module_router",
                 f"判断所属领域模块（财务/人力/流程/基础架构等），只输出模块名:\n{summary}"),
            ]

            # 并发发起 5 个字段请求
            field_results: dict = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(_gen_field, fk, prompt): fk
                    for fk, prompt in field_tasks
                }
                for future in concurrent.futures.as_completed(futures):
                    fk, val = future.result()
                    field_results[fk] = val

            failed_count = sum(1 for v in field_results.values() if v is None)

            # 失败字段 >= 3 时整体降级到原单 prompt 路径
            if failed_count >= 3:
                print(f"[AIWorker] {issue.key} 多字段生成失败数 {failed_count}/5，降级到单 prompt 分析")
                self._batch_llm_analyze_fallback([issue])
                continue

            # 用原有批量提示词补全 recommended_team/role/confidence 等核心字段
            batch_prompt = self._build_batch_prompt([issue])
            try:
                # 限流
                elapsed = time.time() - self.last_api_call
                if elapsed < self.min_interval:
                    time.sleep(self.min_interval - elapsed)
                self.last_api_call = time.time()

                batch_response = self._call_llm_with_feature("classification_analysis", batch_prompt, user_id=user_id)
                model_name = self._resolve_routed_llm_config("classification_analysis", user_id=user_id).get("model_name", "glm-5")
                base_results = self._parse_batch_response(batch_response, [issue.key], model_name)
                analysis = base_results.get(issue.key) or self._rule_based_analysis(issue)
            except Exception as e:
                print(f"[AIWorker] {issue.key} 批量提示词分析失败: {e}，使用规则引擎")
                analysis = self._rule_based_analysis(issue)

            # 将并发字段结果合并进 analysis
            if field_results.get("solution_recommendation"):
                analysis["solution_recommendation"] = field_results["solution_recommendation"]
            if field_results.get("reply_style_router"):
                analysis["reply_style"] = field_results["reply_style_router"].strip()
            if field_results.get("domain_module_router"):
                analysis["domain_module"] = field_results["domain_module_router"].strip()
            analysis["_feature_routing"] = {
                "classification": "classification_analysis",
                "confidence":     "confidence_scoring",
                "solution":       "solution_recommendation",
                "reply_style":    "reply_style_router",
                "domain_module":  "domain_module_router",
            }

            # 相似工单
            try:
                similar = self.vector_store.search_similar_issues(
                    query=f"{issue.summary} {issue.description}",
                    top_k=3,
                    min_score=0.6,
                )
                analysis["similar_issues"] = [s["issue_key"] for s in similar]
            except Exception as e:
                print(f"[AIWorker] 搜索相似工单失败 {issue.key}: {e}")
                analysis["similar_issues"] = []

            self._save_and_notify(
                issue.key, analysis,
                issue_title=issue.summary,
                issue_description=(issue.description or "")[:1000],
            )

    def _batch_llm_analyze_fallback(self, issues: List[JiraIssue]):
        """原单 prompt 批量分析（降级路径，保留原有逻辑；优先走功能路由，其次回退 self.llm_config）"""
        cfg = self._resolve_routed_llm_config("classification_analysis")
        api_key = cfg.get("api_key", "")
        if not api_key:
            print("[LLM Warning] 未配置API Key，使用规则引擎降级")
            for issue in issues:
                fallback = self._rule_based_analysis(issue)
                self._save_and_notify(issue.key, fallback, issue_title=issue.summary, issue_description=(issue.description or '')[:1000])
            return

        # 限流
        elapsed = time.time() - self.last_api_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        # 构建批量提示词
        prompt = self._build_batch_prompt(issues)

        try:
            self.last_api_call = time.time()
            response = self.llm_service.call_llm(
                prompt,
                api_key=api_key,
                provider=cfg.get("provider", "zhipu"),
                model_name=cfg.get("model_name", "glm-5"),
                base_url=cfg.get("base_url", "")
            )

            # 检查响应是否为错误消息
            if response.startswith("Error:"):
                print(f"[LLM Error] {response}")
                for issue in issues:
                    fallback = self._rule_based_analysis(issue)
                    self._save_and_notify(issue.key, fallback, issue_title=issue.summary, issue_description=(issue.description or '')[:1000])
                return

            # 解析结果，传递实际使用的模型名称
            model_name = cfg.get("model_name", "glm-5")
            results = self._parse_batch_response(response, [i.key for i in issues], model_name)

            # 保存并通知
            for issue in issues:
                key = issue.key
                if key in results:
                    analysis = results[key]
                    # 添加相似工单信息（容错处理）
                    try:
                        similar = self.vector_store.search_similar_issues(
                            query=f"{issue.summary} {issue.description}",
                            top_k=3,
                            min_score=0.6
                        )
                        analysis['similar_issues'] = [s['issue_key'] for s in similar]
                    except Exception as e:
                        print(f"[AIWorker] 搜索相似工单失败 {key}: {e}")
                        analysis['similar_issues'] = []

                    self._save_and_notify(key, analysis, issue_title=issue.summary, issue_description=(issue.description or '')[:1000])
                else:
                    # 解析失败，使用降级方案
                    fallback = self._rule_based_analysis(issue)
                    self._save_and_notify(key, fallback, issue_title=issue.summary, issue_description=(issue.description or '')[:1000])

        except Exception as e:
            print(f"[LLM Error] 批量分析失败: {e}")
            # 全部使用降级方案
            for issue in issues:
                fallback = self._rule_based_analysis(issue)
                self._save_and_notify(issue.key, fallback, issue_title=issue.summary, issue_description=(issue.description or '')[:1000])
    
    def _build_batch_prompt(self, issues: List[JiraIssue]) -> str:
        """构建批量分析提示词"""
        # 收集相似历史工单作为上下文
        all_similar = []
        issues_section = []
        
        for idx, issue in enumerate(issues, 1):
            # 查相似工单（容错处理）
            try:
                similar = self.vector_store.search_similar_issues(
                    query=f"{issue.summary} {issue.description}",
                    top_k=3,
                    min_score=0.6
                )
                all_similar.extend(similar)
            except Exception as e:
                print(f"[AIWorker] 构建提示词时搜索相似工单失败 {issue.key}: {e}")
            
            similar_refs = [f"{s['issue_key']}(相似度{s['score']:.0%})" for s in similar]
            
            issues_section.append(f"""
【工单{idx}】
编号: {issue.key}
标题: {issue.summary}
描述: {issue.description[:300] if issue.description else '无'}
历史相似工单: {', '.join(similar_refs) if similar_refs else '无'}
""")
        
        # 去重并格式化相似工单上下文
        unique_similar = {s['issue_key']: s for s in all_similar}
        similar_context = "\n".join([
            f"- {k}: {v['summary'][:80]}" 
            for k, v in list(unique_similar.items())[:8]
        ])
        
        return f"""你是工单智能分析助手。请批量分析以下{len(issues)}个Jira工单，为每个工单给出处理建议。

【相似历史工单参考】
{similar_context if similar_context else '（无高相似度历史工单）'}

【待分析工单列表】
{'---'.join(issues_section)}

【输出要求】
请严格返回JSON格式（不要Markdown代码块标记）：
{{
  "results": [
    {{
      "issue_key": "工单编号",
      "recommended_team": "推荐团队（云平台-流程中心/财务组/人力组/基础架构组）",
      "recommended_role": "推荐角色（后端开发/前端开发/产品经理/测试/运维）",
      "functionality_impact": "功能影响范围（30字内）",
      "solution_suggestion": "解决建议（80字内）",
      "confidence": 0.92,
      "reasoning": "判断理由简述"
    }}
  ]
}}

注意：
1. confidence基于历史相似工单匹配程度计算（0-1）
2. 若信息不足，confidence可低于0.7，并备注"需人工确认"
3. 推荐团队/角色必须从给定枚举中选择
"""
    
    def _parse_batch_response(self, response: str, expected_keys: List[str], model_name: str = "glm-5") -> Dict[str, Dict]:
        """解析批量响应"""
        import re

        try:
            # 提取JSON
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if not json_match:
                raise ValueError("未找到JSON")

            data = json.loads(json_match.group(0))
            results = {}

            for item in data.get('results', []):
                key = item.get('issue_key')
                if key in expected_keys:
                    results[key] = {
                        'recommended_team': item.get('recommended_team', '待确认'),
                        'recommended_role': item.get('recommended_role', '待确认'),
                        'functionality_impact': item.get('functionality_impact', ''),
                        'solution_suggestion': item.get('solution_suggestion', ''),
                        'confidence': float(item.get('confidence', 0.7)),
                        'reasoning': item.get('reasoning', ''),
                        'model_used': model_name,
                        'is_reused': False
                    }

            return results
            
        except Exception as e:
            print(f"[Parse Error] 解析LLM响应失败: {e}")
            return {}
    
    def _rule_based_analysis(self, issue: JiraIssue) -> Dict:
        """基于规则的降级分析"""
        text = f"{issue.summary} {issue.description}".lower()
        
        # 团队判断（从 deployment.yaml module_taxonomy 加载，不再硬编码）
        from config.loader import cfg
        _taxonomy = cfg("module_taxonomy") or []
        team_keywords = {
            m["team"]: m.get("keywords", [])
            for m in _taxonomy if m.get("team") and m.get("keywords")
        }
        _default_team = _taxonomy[0]["team"] if _taxonomy and _taxonomy[0].get("team") else ""

        team_scores = {team: sum(1 for k in keywords if k in text)
                      for team, keywords in team_keywords.items()}
        recommended_team = max(team_scores, key=team_scores.get) if team_scores and max(team_scores.values()) > 0 else _default_team
        
        # 角色判断
        role_keywords = {
            '前端开发': ['页面', '样式', 'ui', '交互', '前端', '组件', 'css', 'js'],
            '后端开发': ['接口', 'api', '数据库', 'sql', '流程', '审批', '服务'],
            '产品经理': ['需求', '产品', '设计', '方案', '优化', '建议'],
            '测试': ['bug', '缺陷', '测试', '用例', '验证'],
            '运维': ['部署', '环境', '服务器', '配置', '重启', '日志']
        }
        
        role_scores = {role: sum(1 for k in keywords if k in text) 
                      for role, keywords in role_keywords.items()}
        recommended_role = max(role_scores, key=role_scores.get) if max(role_scores.values()) > 0 else '待确认'
        
        return {
            'recommended_team': recommended_team,
            'recommended_role': recommended_role,
            'functionality_impact': '基于关键词规则推断',
            'solution_suggestion': '此为AI分析失败后的降级结果，建议人工复核。可参考历史相似工单处理方案。',
            'confidence': 0.5,
            'model_used': 'rule_engine',
            'is_reused': False
        }
    
    def _save_and_notify(self, issue_key: str, analysis: Dict, issue_title: str = "", issue_description: str = ""):
        """保存结果并触发回调"""
        # 写入向量缓存（如果可用）
        vector_cached = False
        try:
            self.vector_store.cache_analysis(issue_key, analysis, summary=issue_title)
            vector_cached = True
        except Exception as e:
            print(f"[AIWorker] 向量缓存失败: {e}")

        # 无论向量缓存是否成功，都保存到文件缓存（确保持久化）
        self._file_cache_analysis(issue_key, analysis, issue_title=issue_title, issue_description=issue_description)

        # 触发回调
        for callback in self.callbacks:
            try:
                callback(issue_key, analysis)
            except Exception as e:
                print(f"[Callback Error] {e}")

    def _file_cache_analysis(self, issue_key: str, analysis: Dict, issue_title: str = "", issue_description: str = ""):
        """文件缓存分析结果（向量存储不可用时）"""
        # 使用PROJECT_ROOT确保路径正确
        cache_dir = os.path.join(BASE_DIR, "data_cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, "analysis_cache.json")
        temp_file = f"{cache_file}.tmp"

        try:
            # 读取现有缓存
            cache = {}
            if os.path.exists(cache_file):
                with open(cache_file, 'r', encoding='utf-8') as f:
                    raw = f.read().strip()
                if raw:
                    try:
                        cache = json.loads(raw)
                    except json.JSONDecodeError:
                        print(f"[AIWorker] 文件缓存损坏，重建缓存文件: {cache_file}")

            # 添加新分析结果
            entry = {**analysis, 'cached_at': datetime.now().isoformat(), 'cache_type': 'file'}
            # Backfill title/description if provided and not already in analysis
            if issue_title and not entry.get('issue_title'):
                entry['issue_title'] = issue_title
            if issue_description and not entry.get('issue_description'):
                entry['issue_description'] = issue_description[:1000]
            # Soft validation: warn when title is missing
            if not entry.get('issue_title'):
                print(f"[Cache] WARN: issue_title missing for {issue_key}, confidence={entry.get('confidence')}")
            cache[issue_key] = entry

            # 原子写入，避免中途中断留下空文件
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, cache_file)

            print(f"[AIWorker] {issue_key} 分析结果已保存到文件缓存")
        except Exception as e:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
            print(f"[AIWorker] 文件缓存失败: {e}")


def _patch_analysis_cache(issue_key: str, **patch_fields) -> None:
    """Atomically write one or more fields to an existing analysis_cache entry."""
    cache_file = os.path.join(BASE_DIR, "data_cache", "analysis_cache.json")
    temp_file = f"{cache_file}.tmp"
    try:
        cache = {}
        if os.path.exists(cache_file):
            raw = open(cache_file, encoding="utf-8").read().strip()
            if raw:
                cache = json.loads(raw)
        if issue_key not in cache:
            return
        cache[issue_key].update(patch_fields)
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, cache_file)
    except Exception as e:
        print(f"[Cache] _patch_analysis_cache failed for {issue_key}: {e}")


class BoardService:
    """
    看板服务 - Chroma优化版

    核心改进：
    1. 使用向量数据库存储工单和AI分析结果
    2. 看板加载时即时返回缓存数据，后台异步更新
    3. 支持语义相似的建议复用，减少API调用
    """

    def __init__(self, llm_service: LLMService, api_key: str = None, allow_download: bool = True):
        self.llm_service = llm_service

        # 使用绝对路径确保一致性（demo 沙箱走 DEMO_RUNTIME_DIR）
        persist_dir = os.path.join(BASE_DIR, "chroma_db")

        self.vector_store = VectorStore(persist_directory=persist_dir, api_key=api_key, allow_download=allow_download)
        self.search_engine = SemanticSearchEngine(api_key=api_key, allow_download=allow_download)

        # LLM配置 - 从配置文件加载
        self.llm_config = self._load_llm_config()
        # 看板配置
        self.board_config = self._load_board_config()

        # 配置文件读写锁（保证并发安全）
        self._config_lock = threading.Lock()

        # 启动后台分析工作器
        self.worker = AIAnalysisWorker(
            vector_store=self.vector_store,
            llm_service=llm_service,
            batch_size=5,
            max_workers=2
        )
        self.worker.start()

        # 同步配置到worker
        self.worker.llm_config = self.llm_config
        
        # 注册分析完成回调（用于推送更新）
        self.worker.on_complete(self._on_analysis_complete)

        # 回复训练器
        self.reply_trainer = ReplyTrainer()

        # 智能回复网关 v2
        from services.reply_gateway import ReplyGateway as _ReplyGateway
        self.reply_gateway = _ReplyGateway(
            vector_store=self.vector_store,
            llm_service=None,
            reply_trainer=self.reply_trainer,
        )

        # 分析状态内存缓存（用于前端轮询）
        self.analysis_status = {}  # issue_key -> status
        self.jira_cache_service = None
        self._last_board_fetch_meta = self._build_fetch_meta()
        self._board_fetch_prefer_proxy = os.environ.get("BOARD_FETCH_PREFER_PROXY", "false").lower() == "true"
        self._jira_direct_failure_cooldown_seconds = int(
            os.environ.get("BOARD_JIRA_DIRECT_COOLDOWN_SECONDS", "120")
        )
        self._jira_direct_cooldown_until = 0.0
        self._jira_direct_last_error = None
        self._jira_direct_last_failure_at = None
        self._jira_direct_state_lock = threading.Lock()

        # 回复内容预生成线程池（低优先级，分析完成后自动预热回复缓存，让用户秒开回复弹窗）
        self._reply_pregen_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="reply-pregen"
        )

    def _load_llm_config(self) -> Dict:
        """从配置文件加载LLM配置"""
        config_file = os.path.join(os.path.dirname(__file__), "llm_config.json")
        default_config = {
            "provider": "zhipu",
            "api_key": "",
            "model_name": "glm-5",
            "base_url": ""
        }

        if not os.path.exists(config_file):
            print("[BoardService] LLM配置文件不存在，使用默认配置")
            return default_config

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                saved_config = json.load(f)

            # 获取最后使用的provider
            last_provider = saved_config.get("last_provider", "zhipu")
            provider_config = saved_config.get(last_provider, {})

            config = {
                "provider": last_provider,
                "api_key": provider_config.get("api_key", ""),
                "model_name": provider_config.get("model_name", default_config["model_name"]),
                "base_url": provider_config.get("base_url", "")
            }

            if config["api_key"]:
                print(f"[BoardService] 已加载LLM配置: provider={last_provider}")
            else:
                print("[BoardService] LLM配置中无API Key")

            return config

        except Exception as e:
            print(f"[BoardService] 加载LLM配置失败: {e}")
            return default_config

    def update_llm_config(self, provider: str, api_key: str, model_name: str, base_url: str):
        """更新LLM配置"""
        self.llm_config = {
            "provider": provider,
            "api_key": api_key,
            "model_name": model_name or self._get_default_model(provider),
            "base_url": base_url
        }
        # 同步更新worker的配置
        self.worker.llm_config = self.llm_config
        print(f"[BoardService] LLM配置已更新: provider={provider}, model={self.llm_config['model_name']}")

    def set_jira_cache_service(self, jira_cache_service):
        """注入 Jira 代理缓存服务，用于优先获取新鲜数据。"""
        self.jira_cache_service = jira_cache_service

    def get_last_board_fetch_meta(self) -> Dict[str, Any]:
        """返回最近一次看板取数的数据来源元信息。"""
        return dict(self._last_board_fetch_meta)

    def _ensure_fetch_strategy_state(self):
        """兼容测试桩和老实例，补齐取数策略状态字段。"""
        if not hasattr(self, "_board_fetch_prefer_proxy"):
            self._board_fetch_prefer_proxy = os.environ.get("BOARD_FETCH_PREFER_PROXY", "false").lower() == "true"
        if not hasattr(self, "_jira_direct_failure_cooldown_seconds"):
            self._jira_direct_failure_cooldown_seconds = int(
                os.environ.get("BOARD_JIRA_DIRECT_COOLDOWN_SECONDS", "120")
            )
        if not hasattr(self, "_jira_direct_cooldown_until"):
            self._jira_direct_cooldown_until = 0.0
        if not hasattr(self, "_jira_direct_last_error"):
            self._jira_direct_last_error = None
        if not hasattr(self, "_jira_direct_last_failure_at"):
            self._jira_direct_last_failure_at = None
        if not hasattr(self, "_jira_direct_state_lock"):
            self._jira_direct_state_lock = threading.Lock()

    def _has_healthy_proxy_node(self) -> bool:
        """判断当前是否存在可用的 mini 代理节点。"""
        if not self.jira_cache_service:
            return False

        nodes = getattr(self.jira_cache_service, "_nodes", None)
        if not isinstance(nodes, list) or not nodes:
            return True

        try:
            return any(node.is_available() for node in nodes)
        except Exception:
            return True

    def _is_jira_direct_network_error(self, error_message: Optional[str]) -> bool:
        """识别值得进入冷却期的 Jira 直连网络错误。"""
        if not error_message:
            return False

        lower_error = error_message.lower()
        keywords = (
            "timeout",
            "timed out",
            "connecttimeout",
            "connectionerror",
            "connection aborted",
            "max retries exceeded",
            "failed to establish a new connection",
            "name or service not known",
            "temporary failure in name resolution",
        )
        return any(keyword in lower_error for keyword in keywords)

    def _record_jira_direct_success(self):
        """Jira 直连成功后清空冷却状态。"""
        self._ensure_fetch_strategy_state()
        with self._jira_direct_state_lock:
            self._jira_direct_cooldown_until = 0.0
            self._jira_direct_last_error = None
            self._jira_direct_last_failure_at = None

    def _record_jira_direct_failure(self, error_message: Optional[str]):
        """Jira 直连失败后记录错误，并在网络错误时打开冷却期。"""
        self._ensure_fetch_strategy_state()
        with self._jira_direct_state_lock:
            self._jira_direct_last_error = error_message
            self._jira_direct_last_failure_at = time.time()
            if self._is_jira_direct_network_error(error_message):
                self._jira_direct_cooldown_until = (
                    time.time() + self._jira_direct_failure_cooldown_seconds
                )

    def get_fetch_strategy_state(self) -> Dict[str, Any]:
        """暴露当前有效取数顺序与 Jira 直连冷却状态。"""
        self._ensure_fetch_strategy_state()
        now = time.time()
        with self._jira_direct_state_lock:
            cooldown_remaining = max(0, int(self._jira_direct_cooldown_until - now))
            cooldown_active = cooldown_remaining > 0
            last_error = self._jira_direct_last_error

        return {
            "prefer_proxy": self._board_fetch_prefer_proxy,
            "proxy_healthy": self._has_healthy_proxy_node(),
            "effective_fetch_order": self._get_effective_fetch_order(),
            "jira_direct_cooldown_active": cooldown_active,
            "jira_direct_cooldown_remaining_seconds": cooldown_remaining,
            "jira_direct_last_error": last_error,
            "jira_direct_failure_cooldown_seconds": self._jira_direct_failure_cooldown_seconds,
        }

    def _get_effective_fetch_order(self) -> List[str]:
        """根据环境偏好和 Jira 直连冷却状态返回当前有效取数顺序。"""
        self._ensure_fetch_strategy_state()
        has_proxy = self._has_healthy_proxy_node()

        if has_proxy and self._board_fetch_prefer_proxy:
            return ["jira_proxy", "jira_direct", "local_cache"]

        with self._jira_direct_state_lock:
            cooldown_active = time.time() < self._jira_direct_cooldown_until

        if has_proxy and cooldown_active:
            return ["jira_proxy", "local_cache", "jira_direct"]

        return ["jira_direct", "jira_proxy", "local_cache"]

    def _build_fetch_meta(
        self,
        data_source: str = "jira_direct",
        cache_timestamp: Optional[str] = None,
        jira_error: Optional[str] = None
    ) -> Dict[str, Any]:
        return {
            "data_source": data_source,
            "cache_timestamp": cache_timestamp,
            "jira_error": jira_error,
        }

    def _extract_cache_service_payload(self, result: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """解包 jira_cache_service.search_issues() 的统一响应。"""
        if not isinstance(result, dict):
            return None, f"unexpected response type: {type(result).__name__}"

        if result.get("status") != "success":
            error = result.get("message") or result.get("details") or result.get("code") or "unknown error"
            return None, str(error)

        payload = result.get("data")
        if not isinstance(payload, dict) or "issues" not in payload:
            return None, "missing Jira search payload"

        return payload, None

    def _fetch_board_issues(
        self,
        jql: str,
        jira_client: Optional[JiraService] = None,
        force: bool = False,
    ) -> Tuple[List[JiraIssue], Dict[str, Any]]:
        """按当前有效顺序获取看板工单，并在直连异常时触发代理优先降级。

        优化：如果本地缓存存在且不超过5分钟，优先返回缓存，后台异步刷新Jira。
        force=True 跳过缓存，直接从Jira获取最新数据。
        """
        self._ensure_fetch_strategy_state()
        errors: List[str] = []
        client = jira_client or jira_service

        # === 快路径：有缓存就直接返回，后台异步刷新（force=True时跳过）===
        if not force:
            cache_info = client.get_cache_info()
            if cache_info.get("exists"):
                import time as _t
                cache_age = _t.time() - cache_info.get("timestamp_epoch", 0)
                issues = client.load_board_cache()
                if issues:
                    print(f"[BoardService] 快路径: 使用{cache_age:.0f}s前的缓存 ({len(issues)}条), 后台刷新Jira")
                    self._background_refresh_cache(jql, client)
                    return issues, self._build_fetch_meta(
                        data_source="local_cache_fast",
                        cache_timestamp=cache_info.get("timestamp"),
                    )

        # 无缓存时走Jira直连（首次使用场景）
        import time as _t2
        _fetch_start = _t2.monotonic()
        for source in self._get_effective_fetch_order():
            if _t2.monotonic() - _fetch_start > 12:
                print("[BoardService] 取数总耗时超 12s，跳出降级链")
                break
            if source == "jira_direct":
                search_result = client.search_issues_rest_api(jql)
                if "error" not in search_result:
                    issues = client.parse_search_response(search_result)
                    print(f"[BoardService] 从Jira直连获取 {len(issues)} 条工单")
                    self._record_jira_direct_success()
                    if issues:
                        client.save_board_cache(issues)
                    return issues, self._build_fetch_meta(data_source="jira_direct")

                direct_error = str(search_result.get("error"))
                self._record_jira_direct_failure(direct_error)
                errors.append(f"jira_direct: {direct_error}")
                print(f"[BoardService] Jira直连失败: {direct_error}")
                continue

            if source == "jira_proxy":
                if not self.jira_cache_service:
                    continue

                proxy_result = self.jira_cache_service.search_issues(jql, 0, 500)
                proxy_payload, proxy_error = self._extract_cache_service_payload(proxy_result)
                if proxy_payload is not None:
                    issues = client.parse_search_response(proxy_payload)
                    print(f"[BoardService] 通过 Mini代理 获取 {len(issues)} 条工单")
                    if issues:
                        client.save_board_cache(issues)
                    return issues, self._build_fetch_meta(
                        data_source="jira_proxy",
                        jira_error="; ".join(errors) if errors else None,
                    )

                errors.append(f"jira_cache_service: {proxy_error}")
                print(f"[BoardService] Mini代理获取失败: {proxy_error}")
                continue

            if source == "local_cache":
                cache_info = client.get_cache_info()
                if not cache_info.get("exists"):
                    continue

                print(f"[BoardService] 全部在线源失败，回退到本地缓存 ({cache_info.get('count')} 条)")
                issues = client.load_board_cache()
                return issues, self._build_fetch_meta(
                    data_source="local_cache",
                    cache_timestamp=cache_info.get("timestamp"),
                    jira_error="; ".join(errors) if errors else None,
                )

        # 铁律 9：全局会话仅白名单用户可用（mini/QCL 上 = qiangxiao）。
        # 非白名单用户直接 unavailable，不借用全局凭据，防止跨用户数据串。
        # 白名单由环境变量 JIRA_GLOBAL_FALLBACK_USERS（逗号分隔）控制，默认空集合。
        _GLOBAL_FALLBACK_USERS = {
            u.strip() for u in os.environ.get("JIRA_GLOBAL_FALLBACK_USERS", "").split(",") if u.strip()
        }
        if jira_client is not None and jira_client is not jira_service:
            fallback_username = (
                jira_client._auth_override.get("username")
                or jira_client.cache_namespace
            )
            if fallback_username not in _GLOBAL_FALLBACK_USERS:
                print(f"[BoardService] 用户 {fallback_username!r} 不在全局兜底白名单，返回 unavailable")
                errors.append(f"global_fallback: 用户 {fallback_username} 不在白名单")
            else:
                fallback_jql = jql.replace("currentUser()", fallback_username) if fallback_username else jql
                print(f"[BoardService] 全局兜底: 替换 currentUser() → {fallback_username}")
                fallback = jira_service.search_issues_rest_api(fallback_jql)
                if "error" not in fallback:
                    issues = jira_service.parse_search_response(fallback)
                    print(f"[BoardService] 全局凭据兜底获取 {len(issues)} 条工单")
                    if issues:
                        jira_service.save_board_cache(issues)
                    return issues, self._build_fetch_meta(
                        data_source="jira_direct",
                        jira_error="; ".join(errors) if errors else None,
                    )
                errors.append(f"jira_service_fallback: {fallback.get('error', 'unknown')}")


        return [], self._build_fetch_meta(
            data_source="unavailable",
            jira_error="; ".join(errors) if errors else None,
        )

    def _background_refresh_cache(self, jql: str, client):
        """后台线程刷新Jira缓存，不阻塞响应。"""
        if getattr(self, '_bg_refresh_running', False):
            return  # 已有后台刷新在运行
        self._bg_refresh_running = True

        def _refresh():
            try:
                search_result = client.search_issues_rest_api(jql)
                if "error" not in search_result:
                    issues = client.parse_search_response(search_result)
                    if issues:
                        client.save_board_cache(issues)
                        print(f"[BoardService] 后台刷新完成: {len(issues)}条")
            except Exception as e:
                print(f"[BoardService] 后台刷新失败: {e}")
            finally:
                self._bg_refresh_running = False

        t = threading.Thread(target=_refresh, daemon=True)
        t.start()

    def _get_default_model(self, provider: str) -> str:
        """获取默认模型名称"""
        defaults = {
            "gemini": "gemini-2.0-flash",
            "deepseek": "deepseek-chat",
            "zhipu": "glm-5",
            "aliyun": "glm-4.7",
            "minimax": "abab6.5s-chat",
            "kimi": "moonshot-v1-8k",
            "openai": "gpt-4o"
        }
        return defaults.get(provider, "glm-5")

    def _resolve_feature_chain(self, feature: str) -> list:
        """读 llm_feature_routing.json，返回该 feature 的 provider 降级链。"""
        try:
            import json as _json
            routing_path = os.path.join(os.path.dirname(__file__), "llm_feature_routing.json")
            with open(routing_path, encoding="utf-8") as f:
                routing = _json.load(f)
            val = routing.get(feature) or routing.get("_default", self.llm_config.get("provider", "local"))
            return val if isinstance(val, list) else [val]
        except Exception as e:
            print(f"[LLMRouting] 读取 feature routing 失败: {e}")
            return [self.llm_config.get("provider", "local")]

    def _load_provider_config(self, provider: str) -> dict:
        """从 llm_config.json 读取指定 provider 的配置，返回 {api_key, model_name, base_url}。"""
        try:
            import json as _json
            cfg_path = os.path.join(os.path.dirname(__file__), "llm_config.json")
            with open(cfg_path, encoding="utf-8") as f:
                all_cfg = _json.load(f)
            p = all_cfg.get(provider, {})
            return {
                "api_key": p.get("api_key", ""),
                "model_name": p.get("model_name", self._get_default_model(provider)),
                "base_url": p.get("base_url", ""),
            }
        except Exception as e:
            print(f"[LLMRouting] 读取 provider config 失败 ({provider}): {e}")
            return {}

    def _build_jql(
        self,
        project_key: str = None,
        assignee: str = "currentUser()",
        created_start: str = "",
        created_end: str = "",
        labels: str = "",
        dev_issue_type: str = "",
        customer_issue_type: str = "",
        resolution_method: str = "",
        domain_modules: list = []
    ) -> str:
        """
        构建JQL查询语句

        Args:
            project_key: 项目Key，默认 MYPROJECT
            assignee: 经办人，默认 currentUser()，"ALL" 表示查询全部
            created_start: 创建时间开始 (YYYY-MM-DD)
            created_end: 创建时间结束 (YYYY-MM-DD)
            labels: 标签
            dev_issue_type: 研发确认问题类型 (customfield_10729)
            customer_issue_type: 客户问题类型 (customfield_10402)
            resolution_method: 解决方式 (customfield_10906)

        Returns:
            完整的JQL查询语句
        """
        if not project_key:
            from config.loader import cfg
            project_key = cfg("instance", "primary_project_key") or None
        conditions = []
        if project_key and project_key != "ALL":
            conditions.append(f"project = {project_key}")

        # 默认只查未解决的工单
        conditions.append("resolution = Unresolved")

        # 经办人条件（ALL表示查询全部）
        if assignee and assignee != "ALL":
            conditions.append(f"assignee in ({assignee})")

        # 创建时间范围
        if created_start:
            conditions.append(f'created >= "{created_start}"')
        if created_end:
            conditions.append(f'created <= "{created_end}"')

        # 标签
        if labels:
            conditions.append(f'labels = "{labels}"')

        # 研发确认问题类型 (customfield_10729)
        if dev_issue_type:
            conditions.append(f'cf[10729] = "{dev_issue_type}"')

        # 客户问题类型 (customfield_10402)
        if customer_issue_type:
            conditions.append(f'cf[10402] = "{customer_issue_type}"')

        # 解决方式 (customfield_10906)
        if resolution_method:
            conditions.append(f'cf[10906] = "{resolution_method}"')

        # 用户领域模块过滤（cf[10123] = 领域模块 cascading select）
        if domain_modules:
            quoted = ", ".join(f'"{m}"' for m in domain_modules)
            conditions.append(f"cf[10123] in ({quoted})")

        return " AND ".join(conditions) + " ORDER BY due ASC, updated DESC"

    def get_board_data(
        self,
        project_key: str = None,
        assignee: str = "currentUser()",
        created_start: str = "",
        created_end: str = "",
        labels: str = "",
        dev_issue_type: str = "",
        customer_issue_type: str = "",
        resolution_method: str = "",
        jira_client: Optional[JiraService] = None,
        force: bool = False,
        domain_modules: list = [],
    ) -> Dict[str, List[Dict]]:
        """
        获取看板数据 - 优化后版本（支持离线缓存模式和可配置查询条件）

        优化点：
        1. 从Jira获取工单后，立即尝试从向量缓存读取AI分析
        2. 有缓存的工单直接显示分析结果（completed状态）
        3. 无缓存的工单显示"未分析"状态，不自动触发分析
        4. 只有用户点击"重新分析"时才提交LLM分析
        5. Jira连接失败时使用本地缓存（服务器部署模式）
        6. 支持多维度筛选条件（创建时间、标签、问题类型等）

        Args:
            project_key: 项目Key，默认 MYPROJECT
            assignee: 经办人，默认 currentUser()，"ALL" 表示查询全部
            created_start: 创建时间开始 (YYYY-MM-DD)
            created_end: 创建时间结束 (YYYY-MM-DD)
            labels: 标签
            dev_issue_type: 研发确认问题类型 (customfield_10729)
            customer_issue_type: 客户问题类型 (customfield_10402)
            resolution_method: 解决方式 (customfield_10906)
        """
        import time as _time
        _t0 = _time.time()

        # 重新加载看板配置（支持热更新）
        self.board_config = self._load_board_config()

        # 1. 构建JQL查询
        issues = []
        jql = self._build_jql(
            project_key=project_key,
            assignee=assignee,
            created_start=created_start,
            created_end=created_end,
            labels=labels,
            dev_issue_type=dev_issue_type,
            customer_issue_type=customer_issue_type,
            resolution_method=resolution_method,
            domain_modules=domain_modules
        )
        print(f"[BoardService] JQL: {jql}")
        issues, fetch_meta = self._fetch_board_issues(jql, jira_client=jira_client, force=force)
        self._last_board_fetch_meta = fetch_meta
        _t1 = _time.time()
        print(f"[BoardService] Step1 Jira获取: {_t1-_t0:.2f}s ({len(issues)}条)")

        if not issues:
            print("[BoardService] 警告: 无数据可用")

        # 2. 组织看板列
        columns = self._organize_columns(issues)
        
        # 3. 合并AI分析（缓存优先，批量查询优化）
        not_analyzed_issues = []

        # 3a. 预加载文件缓存（一次性读取，避免N次磁盘IO）
        file_cache = {}
        cache_file = os.path.join(BASE_DIR, "data_cache", "analysis_cache.json")
        try:
            if os.path.exists(cache_file):
                with open(cache_file, 'r', encoding='utf-8') as f:
                    raw = f.read().strip()
                if raw:
                    file_cache = json.loads(raw)
        except Exception as e:
            print(f"[BoardService] 文件缓存加载失败: {e}")

        # 3b. 收集所有issue key，批量查询ChromaDB
        all_keys = []
        for col_name in columns:
            for issue_dict in columns[col_name]:
                all_keys.append(issue_dict['key'])

        # 批量从ChromaDB获取（单次查询替代N次查询）
        chroma_cache = {}
        if all_keys:
            try:
                batch_ids = [f"analysis_{k}" for k in all_keys]
                result = self.vector_store.analysis_collection.get(
                    ids=batch_ids,
                    include=['metadatas']
                )
                if result and result['ids']:
                    from datetime import datetime, timedelta
                    now = datetime.now()
                    for i, aid in enumerate(result['ids']):
                        key = aid.replace('analysis_', '', 1)
                        meta = result['metadatas'][i]
                        # 检查过期
                        expires_at = datetime.fromisoformat(meta.get('expires_at', '2000-01-01'))
                        if now > expires_at:
                            continue
                        chroma_cache[key] = self.vector_store._meta_to_analysis(meta)
            except Exception as e:
                print(f"[BoardService] ChromaDB批量查询失败: {e}")

        # 3c. 合并缓存结果
        for col_name in columns:
            for issue_dict in columns[col_name]:
                issue_key = issue_dict['key']

                cached = chroma_cache.get(issue_key) or file_cache.get(issue_key)
                is_rule_engine = cached and cached.get('model_used') == 'rule_engine'

                if cached and not is_rule_engine:
                    issue_dict['ai_analysis'] = cached
                    issue_dict['ai_status'] = 'completed'
                else:
                    # 未分析 或 仅有规则引擎降级结果 → 重新提交LLM分析队列
                    issue_dict['ai_analysis'] = cached if is_rule_engine else None
                    issue_dict['ai_status'] = 'analyzing'
                    not_analyzed_issues.append(issue_dict)

        _t2 = _time.time()
        print(f"[BoardService] Step2+3 缓存合并: {_t2-_t1:.2f}s (chroma={len(chroma_cache)}, file={len(file_cache)}, miss={len(not_analyzed_issues)})")
        print(f"[BoardService] 总耗时: {_t2-_t0:.2f}s")

        # 4. 自动提交未分析工单到AI队列
        if not_analyzed_issues:
            self._auto_submit_analysis(not_analyzed_issues)

        return columns

    def _auto_submit_analysis(self, issues: List[Dict], max_batch_size: int = 50):
        """
        自动提交未分析工单到AI队列

        根据board.md文档要求：
        - 加载看板时，所有工单的AI分析应该已经准备好
        - 异步提前查询工单并提交LLM进行任务队列分析

        Args:
            issues: 待分析的工单列表
            max_batch_size: 单次最大提交数量，防止队列瞬间爆满
        """
        # 根据队列剩余容量动态调整提交数量
        remaining_capacity = self.worker.max_queue_size - self.worker.task_queue.qsize()
        max_submit = min(len(issues), max_batch_size, remaining_capacity)

        if max_submit <= 0:
            print(f"[BoardService] AI队列已满({self.worker.task_queue.qsize()}/{self.worker.max_queue_size})，跳过自动提交")
            # 标记剩余工单为未分析状态
            for issue_dict in issues:
                issue_dict['ai_status'] = 'not_analyzed'
            return

        submitted = 0
        skipped = 0
        failed = 0

        for issue_dict in issues[:max_submit]:
            issue_key = issue_dict['key']

            # 检查是否已经在分析中（避免重复提交）
            current_status = self.analysis_status.get(issue_key, {}).get('status')
            if current_status == 'analyzing':
                skipped += 1
                continue

            try:
                # 创建JiraIssue对象（使用正确的字段名）
                issue = JiraIssue(
                    key=issue_key,
                    summary=issue_dict.get('summary', ''),
                    description=issue_dict.get('description', ''),
                    issue_type=issue_dict.get('issue_type', 'Support'),
                    priority=issue_dict.get('priority', 'Normal'),
                    status=issue_dict.get('status', ''),
                    assignee=issue_dict.get('assignee', ''),
                    reporter=issue_dict.get('reporter', ''),
                    created=issue_dict.get('created', ''),
                    updated=issue_dict.get('updated', ''),
                    due_date=issue_dict.get('due_date', ''),
                    project_name=issue_dict.get('project_name', '云平台-流程中心')
                )

                # 提交到AI队列（低优先级，不影响用户手动触发的分析）
                # rule_engine降级结果强制重分析，其余走正常复用逻辑
                is_rule_engine_cached = issue_dict.get('ai_analysis', {}) and \
                    issue_dict.get('ai_analysis', {}).get('model_used') == 'rule_engine'
                was_queued = self.worker.submit(issue, priority=5, skip_reuse=bool(is_rule_engine_cached))
                if was_queued:
                    # 实际入队：标记为分析中，等待回调更新
                    self.analysis_status[issue_key] = {'status': 'analyzing'}
                    submitted += 1
                    print(f"[BoardService] {issue_key} 已自动提交AI分析队列")
                else:
                    # 缓存命中，worker未入队：不设analyzing，让前端轮询直接读缓存
                    skipped += 1

            except Exception as e:
                print(f"[BoardService] 自动提交分析失败 {issue_key}: {e}")
                # 提交失败，回退到not_analyzed状态
                issue_dict['ai_status'] = 'not_analyzed'
                # 清理内存状态，避免状态不一致
                if issue_key in self.analysis_status:
                    del self.analysis_status[issue_key]
                failed += 1

        # 超出批处理上限的工单标记为not_analyzed
        for issue_dict in issues[max_submit:]:
            issue_dict['ai_status'] = 'not_analyzed'

        if submitted > 0 or skipped > 0 or failed > 0:
            print(f"[BoardService] 自动分析提交完成: {submitted}个提交, {skipped}个跳过, {failed}个失败")

    def _load_board_config(self) -> Dict:
        """从配置文件加载看板配置"""
        config_file = os.path.join(os.path.dirname(__file__), "data/board_config.json")
        default_config = {
            "columns": [
                {"key": "overdue", "title": "⚠️ 已逾期", "type": "date", "rule": "overdue", "color": "red", "bg": "bg-red-50", "visible": True},
                {"key": "today", "title": "📅 今天到期", "type": "date", "rule": "today", "color": "orange", "bg": "bg-orange-50", "visible": True},
                {"key": "tomorrow", "title": "⏰ 明天到期", "type": "date", "rule": "tomorrow", "color": "yellow", "bg": "bg-yellow-50", "visible": True},
                {"key": "this_week", "title": "📆 本周到期", "type": "date", "rule": "this_week", "color": "blue", "bg": "bg-blue-50", "visible": True},
                {"key": "next_week", "title": "📋 下周到期", "type": "date", "rule": "next_week", "color": "indigo", "bg": "bg-indigo-50", "visible": True},
                {"key": "future", "title": "📌 更晚", "type": "date", "rule": "future", "color": "green", "bg": "bg-green-50", "visible": True},
                {"key": "no_date", "title": "📝 无到期日", "type": "date", "rule": "no_date", "color": "gray", "bg": "bg-gray-50", "visible": True}
            ]
        }
        
        if not os.path.exists(config_file):
            return default_config
            
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[BoardService] 加载看板配置失败: {e}")
            return default_config

    def save_board_config(self, config: Dict):
        """保存看板配置"""
        config_file = os.path.join(os.path.dirname(__file__), "data/board_config.json")
        try:
            os.makedirs(os.path.dirname(config_file), exist_ok=True)
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self.board_config = config
            return True
        except Exception as e:
            print(f"[BoardService] 保存看板配置失败: {e}")
            return False

    def _parse_due_date(self, due_date: Optional[str]):
        """解析 Jira 到期日为 date，失败时返回 None。"""
        if not due_date:
            return None

        try:
            date_str = due_date.strip()
            if '.' in date_str:
                date_str = date_str.split('.')[0]
            if ' ' in date_str:
                return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").date()
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            return None

    def _resolve_date_column_key(
        self,
        issue: JiraIssue,
        visible_date_columns: Dict[str, str],
        today,
        tomorrow,
        this_week_end,
        next_week_end,
    ) -> Optional[str]:
        """
        兼容旧接口：返回首个匹配列 key (用于单列定位场景)。
        实际分类请用 _resolve_date_column_keys (复数) 获取所有应归属的列。
        """
        keys = self._resolve_date_column_keys(
            issue, visible_date_columns, today, tomorrow, this_week_end, next_week_end,
        )
        return keys[0] if keys else None

    def _resolve_date_column_keys(
        self,
        issue: "JiraIssue",
        visible_date_columns: Dict[str, str],
        today,
        tomorrow,
        this_week_end,
        next_week_end,
    ) -> List[str]:
        """
        返回所有应归属的日期列 keys。
        设计:
          - this_week 是汇总列: 本周到期（今天/明天/本周其他）都要归属
          - today / tomorrow 是细分列: 只有当天/明天到期才归属
          - 互斥关系: overdue / next_week / future / no_date 之间互斥
        MYPROJECT-61748 (今天到期) → ["today", "this_week"] 两列都显示。
        """
        due = self._parse_due_date(issue.due_date)
        if due is None:
            key = visible_date_columns.get("no_date")
            return [key] if key else []

        results: List[str] = []

        if due < today:
            k = visible_date_columns.get("overdue")
            return [k] if k else []

        # 本周内到期 (含今天/明天/本周其他)
        if due <= this_week_end:
            # 细分列优先（today/tomorrow）
            if due == today and "today" in visible_date_columns:
                results.append(visible_date_columns["today"])
            elif due == tomorrow and "tomorrow" in visible_date_columns:
                results.append(visible_date_columns["tomorrow"])
            # this_week 作为汇总列: 也加入（所有本周到期的工单都归属于此）
            if "this_week" in visible_date_columns:
                week_key = visible_date_columns["this_week"]
                if week_key not in results:
                    results.append(week_key)
            elif not results:
                # 没有 this_week 列也没命中 today/tomorrow (不应发生)，回落
                pass
            return results

        if due <= next_week_end:
            k = visible_date_columns.get("next_week")
            return [k] if k else []

        k = visible_date_columns.get("future")
        return [k] if k else []

    def _organize_columns(self, issues: List[JiraIssue]) -> Dict[str, List[Dict]]:
        """按配置组织看板列（支持手动管理类型）
        注意：所有列（包括 visible=false）都参与工单归类，避免工单因列隐藏而丢失。
        visible=false 由前端负责隐藏，后端始终返回完整数据。
        """
        columns = {}
        # 初始化所有列（含隐藏列），前端按 visible 控制显示
        for col in self.board_config.get("columns", []):
            columns[col["key"]] = []

        # 构建手动管理看板的工单映射 {issue_key: column_key}（含隐藏列）
        manual_issue_map = {}
        for col in self.board_config.get("columns", []):
            if col.get("type") == "manual":
                for issue_key in col.get("manual_issues", []):
                    manual_issue_map[issue_key] = col["key"]

        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)
        this_week_end = today + timedelta(days=(6 - today.weekday()))
        next_week_end = this_week_end + timedelta(days=7)
        # 所有日期规则列都参与映射（不区分 visible），确保工单有处可归
        all_date_columns: Dict[str, str] = {}

        for col in self.board_config.get("columns", []):
            if col.get("type", "system") in ("system", "date") and col.get("rule"):
                all_date_columns.setdefault(col["rule"], col["key"])

        # 用于查找工单的辅助函数
        def find_issue_by_key(issue_list, key):
            for issue in issue_list:
                if issue.key == key:
                    return issue
            return None

        # 先处理手动管理看板的工单（含隐藏列）
        for col in self.board_config.get("columns", []):
            if col.get("type") == "manual":
                col_key = col["key"]
                for issue_key in col.get("manual_issues", []):
                    issue = find_issue_by_key(issues, issue_key)
                    if issue:
                        issue_dict = asdict(issue)
                        issue_dict['_column'] = col_key
                        issue_dict['_manual'] = True  # 标记为手动管理
                        columns[col_key].append(issue_dict)

        # 处理日期规则看板的工单（含隐藏列，避免工单丢失）
        for issue in issues:
            issue_key = issue.key

            # 如果工单已在手动管理看板中，跳过
            if issue_key in manual_issue_map:
                continue

            issue_dict = asdict(issue)
            matched_col_key = None
            # 获取所有应归属的日期列（today+this_week 可同时归）
            resolved_date_column_keys = self._resolve_date_column_keys(
                issue,
                all_date_columns,
                today,
                tomorrow,
                this_week_end,
                next_week_end,
            )
            resolved_date_column_key = resolved_date_column_keys[0] if resolved_date_column_keys else None

            # 遍历所有列定义（含隐藏列），找到第一个匹配的列
            for col in self.board_config.get("columns", []):

                col_type = col.get("type", "system")
                rule = col.get("rule", "")

                # 手动管理类型的看板跳过
                if col_type == "manual":
                    continue

                is_match = False

                if col_type in ("system", "date"):
                    # 日期列支持多列归属（如 today 工单也归 this_week 汇总）
                    is_match = col["key"] in resolved_date_column_keys

                elif col_type == "status":
                    # 状态类型匹配
                    if issue.status == rule:
                        is_match = True

                elif col_type == "label":
                    # 标签/字段匹配 (暂未完全实现，需扩展JiraIssue字段)
                    pass

                if is_match:
                    # 日期列允许多列归属（today + this_week 都显示）
                    # 非日期列(status/label)只取首个匹配
                    if col_type in ("system", "date"):
                        if col["key"] in columns:
                            dup = dict(issue_dict)
                            dup['_column'] = col["key"]
                            columns[col["key"]].append(dup)
                        if matched_col_key is None:
                            matched_col_key = col["key"]
                    else:
                        matched_col_key = col["key"]
                        break

            # 非日期列匹配（status/label）单独处理，避免和日期列重复加入
            if matched_col_key and matched_col_key not in resolved_date_column_keys:
                if matched_col_key in columns:
                    issue_dict['_column'] = matched_col_key
                    columns[matched_col_key].append(issue_dict)

        return columns
    
    def _submit_for_analysis(self, issue_dict: Dict):
        """提交工单到后台分析队列"""
        issue = JiraIssue(
            key=issue_dict['key'],
            summary=issue_dict.get('summary', ''),
            description=issue_dict.get('description', ''),
            status=issue_dict.get('status', ''),
            assignee=issue_dict.get('assignee', ''),
            reporter=issue_dict.get('reporter', ''),
            due_date=issue_dict.get('due_date', ''),
            created=issue_dict.get('created', ''),
            updated=issue_dict.get('updated', ''),
            priority=issue_dict.get('priority', ''),
            issue_type=issue_dict.get('issue_type', ''),
            project_name=issue_dict.get('project_name', '')
        )
        
        # 根据到期日设置优先级
        priority = 5
        if issue_dict.get('ai_status') == 'analyzing':
            # 根据看板列判断优先级
            col = issue_dict.get('_column', '')  # 需要在_organize_columns中设置
            if col == 'today':
                priority = 1
            elif col == 'tomorrow':
                priority = 2
            elif col == 'this_week':
                priority = 3
        
        self.worker.submit(issue, priority)
    
    def _on_analysis_complete(self, issue_key: str, analysis: Dict):
        """分析完成回调"""
        self.analysis_status[issue_key] = {
            'status': 'completed',
            'analysis': analysis,
            'completed_at': datetime.now().isoformat()
        }
        print(f"[BoardService] {issue_key} AI分析完成")
        # 分析完成后异步预热回复缓存，让用户秒开回复弹窗
        self._reply_pregen_pool.submit(self._pregen_reply_async, issue_key)

    def _pregen_reply_async(self, issue_key: str):
        """后台预生成回复内容，预热 reply_cache（不阻塞主线程）"""
        try:
            cached = get_cached_reply(issue_key, {})
            if cached and len(cached) >= 10:
                print(f"[ReplyPregen] {issue_key} 已有缓存，跳过预生成")
                return
            result = self.generate_reply_content(issue_key, force=False)
            rc = result.get("reply_content") or result.get("solution_content") or ""
            if rc:
                print(f"[ReplyPregen] {issue_key} 预生成完成 ({result.get('word_count', 0)} 字)")
            else:
                reason = result.get("status") or result.get("gate_status") or result.get("error", "no_content")
                print(f"[ReplyPregen] {issue_key} 预生成跳过: {reason}")
        except Exception as e:
            print(f"[ReplyPregen] {issue_key} 预生成异常: {e}")
    
    def get_analysis_updates(self, issue_keys: List[str]) -> Dict[str, Dict]:
        """
        前端轮询接口：获取指定工单的最新分析状态

        Returns:
            {
                "MYPROJECT-12345": {"status": "completed", "analysis": {...}},
                "MYPROJECT-12346": {"status": "analyzing"},
                "MYPROJECT-12347": {"status": "not_analyzed"}
            }
        """
        updates = {}
        for key in issue_keys:
            # 先查内存状态（用户手动触发的分析任务）
            if key in self.analysis_status:
                updates[key] = self.analysis_status[key]
                continue

            # 再查向量缓存
            cached = self.vector_store.get_cached_analysis(key)
            if cached:
                updates[key] = {'status': 'completed', 'analysis': cached}
                continue

            # 再查文件缓存（向量存储不可用时）
            file_cached = load_file_cached_analysis(key)
            if file_cached:
                updates[key] = {'status': 'completed', 'analysis': file_cached}
                continue

            # 无缓存且不在分析队列中，返回未分析状态
            updates[key] = {'status': 'not_analyzed'}

        return updates
    
    def force_analyze(self, issue_key: str, jira_client: Optional[JiraService] = None) -> Dict:
        """
        强制重新分析指定工单（用户手动触发）

        流程：
        1. 使旧缓存失效
        2. 从Jira重新获取工单信息
        3. 提交高优先级分析任务
        """
        # 1. 使旧缓存失效
        self.vector_store.invalidate_cache(issue_key)

        # 2. 从Jira获取工单信息
        try:
            client = jira_client or jira_service
            jql = f"key = {issue_key}"
            issues_data = client.search_issues_rest_api(jql)
            issues = client.parse_search_response(issues_data)

            if not issues:
                return {'status': 'error', 'message': f'未找到工单 {issue_key}'}

            issue = issues[0]

            # 3. 提交高优先级分析（跳过复用，强制重新分析）
            self.worker.submit(issue, priority=0, skip_reuse=True)  # 最高优先级，跳过复用

            # 更新内存状态
            self.analysis_status[issue_key] = {'status': 'analyzing'}

            print(f"[BoardService] {issue_key} 已提交重新分析（高优先级）")
            return {'status': 'submitted', 'message': '已提交重新分析'}

        except Exception as e:
            print(f"[Force Analyze Error] {e}")
            return {'status': 'error', 'message': str(e)}

    def batch_reanalyze(self, issue_keys: List[str]) -> Dict:
        """
        批量重新分析多个工单

        Args:
            issue_keys: 工单编号列表

        Returns:
            {
                "submitted": 成功提交数量,
                "failed": 失败数量,
                "queue_size": 当前队列大小
            }
        """
        submitted = 0
        failed = 0

        for issue_key in issue_keys:
            try:
                # 使旧缓存失效
                self.vector_store.invalidate_cache(issue_key)

                # 从本地缓存获取工单信息（避免Jira查询超时）
                issue = self._get_issue_from_cache(issue_key)

                if issue:
                    # 提交高优先级分析（跳过复用，强制重新分析）
                    self.worker.submit(issue, priority=0, skip_reuse=True)
                    self.analysis_status[issue_key] = {'status': 'analyzing'}
                    submitted += 1
                    print(f"[BoardService] {issue_key} 已提交批量重新分析")
                else:
                    failed += 1
                    print(f"[BoardService] {issue_key} 未找到，跳过")

            except Exception as e:
                failed += 1
                print(f"[Batch Reanalyze Error] {issue_key}: {e}")

        return {
            'submitted': submitted,
            'failed': failed,
            'queue_size': self.worker.task_queue.qsize()
        }

    def _get_issue_from_cache(self, issue_key: str, jira_client: Optional[JiraService] = None) -> Optional[JiraIssue]:
        """从本地缓存获取工单信息（无需Jira连接）"""
        # 先从看板数据中查找
        for col in self.board_data.values() if hasattr(self, 'board_data') else []:
            for issue_dict in col:
                if issue_dict.get('key') == issue_key:
                    return JiraIssue(
                        key=issue_dict['key'],
                        summary=issue_dict.get('summary', ''),
                        description=issue_dict.get('description', ''),
                        status=issue_dict.get('status', ''),
                        assignee=issue_dict.get('assignee', ''),
                        reporter=issue_dict.get('reporter', ''),
                        due_date=issue_dict.get('due_date', ''),
                        created=issue_dict.get('created', ''),
                        updated=issue_dict.get('updated', ''),
                        priority=issue_dict.get('priority', ''),
                        issue_type=issue_dict.get('issue_type', ''),
                        project_name=issue_dict.get('project_name', '')
                    )

        # 从Jira缓存加载
        client = jira_client or jira_service
        cache_issues = client.load_board_cache()
        for issue in cache_issues:
            if issue.key == issue_key:
                return issue

        return None

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'vector_stats': self.vector_store.get_stats(),
            'queue_size': self.worker.task_queue.qsize()
        }

    def move_issue_to_board(self, issue_key: str, target_board: str, source_board: str = None, sync_jira: bool = True) -> Dict:
        """
        移动工单到指定看板（支持手动管理类型持久化）

        功能：
        1. 系统看板（按日期分组）→ 仅前端分组变化，不持久化
        2. 手动管理看板 → 持久化工单位置到 manual_issues 列表
        3. 自定义看板 → 可选同步更新Jira状态

        Args:
            issue_key: 工单编号
            target_board: 目标看板key
            source_board: 来源看板key（可选，用于撤销）
            sync_jira: 是否同步到Jira（自定义看板时有效）

        Returns:
            {
                "success": bool,
                "issue_key": str,
                "target_board": str,
                "synced_to_jira": bool,
                "new_status": str,
                "persisted": bool,  # 是否持久化
                "error": str (optional)
            }
        """
        result = {
            'success': False,
            'issue_key': issue_key,
            'target_board': target_board,
            'source_board': source_board,
            'synced_to_jira': False,
            'new_status': None,
            'persisted': False,
            'error': None
        }

        try:
            # 1. 获取目标看板类型
            target_board_type = self._get_board_type(target_board)
            result['board_type'] = target_board_type

            # 2. 如果来源是手动管理看板，从 manual_issues 中移除
            if source_board:
                source_board_type = self._get_board_type(source_board)
                if source_board_type == 'manual':
                    if not self._remove_issue_from_manual_board(issue_key, source_board):
                        result['error'] = "从源看板移除失败"
                        return result

            # 3. 如果目标是手动管理看板，添加到 manual_issues
            if target_board_type == 'manual':
                if not self._add_issue_to_manual_board(issue_key, target_board):
                    result['error'] = "添加到目标看板失败"
                    return result
                result['persisted'] = True

            # 4. 如果是自定义看板且需要同步Jira
            if target_board_type == 'custom' and sync_jira:
                # 获取看板对应的状态映射
                new_status = self._get_board_status_mapping(target_board)
                if new_status:
                    # 调用Jira API更新状态
                    jira_result = self._update_issue_status_in_jira(issue_key, new_status)
                    if jira_result['success']:
                        result['synced_to_jira'] = True
                        result['new_status'] = new_status
                    else:
                        result['error'] = f"Jira更新失败: {jira_result.get('error', '未知错误')}"
                        return result

            # 5. 记录移动历史（用于撤销）
            self._record_move_history(issue_key, source_board, target_board)

            result['success'] = True
            print(f"[BoardService] {issue_key} 已移动到 {target_board} (persisted={result['persisted']})")

        except Exception as e:
            result['error'] = str(e)
            print(f"[Move Issue Error] {issue_key}: {e}")

        return result

    def _add_issue_to_manual_board(self, issue_key: str, board_key: str) -> bool:
        """
        将工单添加到手动管理看板的 manual_issues 列表

        使用线程锁保证并发安全，避免多用户同时添加导致重复。

        Returns:
            bool: 是否成功添加
        """
        with self._config_lock:
            for col in self.board_config.get("columns", []):
                if col.get("key") == board_key and col.get("type") == "manual":
                    if "manual_issues" not in col:
                        col["manual_issues"] = []
                    if issue_key not in col["manual_issues"]:
                        # 先保存，失败则回滚
                        old_issues = col["manual_issues"].copy()
                        col["manual_issues"].append(issue_key)
                        if self.save_board_config(self.board_config):
                            print(f"[BoardService] 添加 {issue_key} 到手动看板 {board_key}")
                            return True
                        else:
                            # 回滚内存修改
                            col["manual_issues"] = old_issues
                            print(f"[BoardService] 保存失败，回滚 {issue_key} 添加操作")
                            return False
                    break
            return False

    def _remove_issue_from_manual_board(self, issue_key: str, board_key: str) -> bool:
        """
        从手动管理看板的 manual_issues 列表中移除工单

        使用线程锁保证并发安全。

        Returns:
            bool: 是否成功移除
        """
        with self._config_lock:
            for col in self.board_config.get("columns", []):
                if col.get("key") == board_key and col.get("type") == "manual":
                    if "manual_issues" in col and issue_key in col["manual_issues"]:
                        # 先保存，失败则回滚
                        old_issues = col["manual_issues"].copy()
                        col["manual_issues"].remove(issue_key)
                        if self.save_board_config(self.board_config):
                            print(f"[BoardService] 从手动看板 {board_key} 移除 {issue_key}")
                            return True
                        else:
                            # 回滚内存修改
                            col["manual_issues"] = old_issues
                            print(f"[BoardService] 保存失败，回滚 {issue_key} 移除操作")
                            return False
                    break
            return False

    def batch_move_issues(self, moves: List[Dict], sync_jira: bool = True) -> Dict:
        """
        批量移动工单

        Args:
            moves: 移动列表 [{"issue_key": "MYPROJECT-123", "target_board": "done"}, ...]
            sync_jira: 是否同步到Jira

        Returns:
            {
                "success": bool,
                "completed": int,
                "failed": int,
                "results": List[Dict]
            }
        """
        results = []
        completed = 0
        failed = 0

        for move in moves:
            issue_key = move.get('issue_key')
            target_board = move.get('target_board')
            source_board = move.get('source_board')

            result = self.move_issue_to_board(issue_key, target_board, source_board, sync_jira)
            results.append(result)

            if result['success']:
                completed += 1
            else:
                failed += 1

        return {
            'success': failed == 0,
            'completed': completed,
            'failed': failed,
            'results': results
        }

    def undo_move(self, move_id: str) -> Dict:
        """
        撤销移动操作

        根据移动历史记录执行反向移动，将工单恢复到原来的看板。

        Args:
            move_id: 移动记录ID（格式：{issue_key}_{timestamp}）

        Returns:
            {
                "success": bool,
                "restored": bool,
                "message": str,
                "move_record": Dict (optional)
            }
        """
        try:
            # 获取移动历史
            history = self.get_move_history(limit=100)
            move_record = next((h for h in history if h.get('id') == move_id), None)

            if not move_record:
                return {
                    'success': False,
                    'restored': False,
                    'message': '未找到移动记录'
                }

            source_board = move_record.get('source_board')
            target_board = move_record.get('target_board')
            issue_key = move_record.get('issue_key')

            if not source_board or not target_board:
                return {
                    'success': False,
                    'restored': False,
                    'message': '移动记录信息不完整'
                }

            # 执行反向移动（从target回到source）
            result = self.move_issue_to_board(
                issue_key=issue_key,
                target_board=source_board,
                source_board=target_board,
                sync_jira=False  # 撤销时不同步Jira
            )

            if result['success']:
                return {
                    'success': True,
                    'restored': True,
                    'message': f'已撤销：{issue_key} 从 {target_board} 恢复到 {source_board}',
                    'move_record': move_record
                }
            else:
                return {
                    'success': False,
                    'restored': False,
                    'message': f"撤销失败: {result.get('error', '未知错误')}",
                    'move_record': move_record
                }

        except Exception as e:
            return {
                'success': False,
                'restored': False,
                'message': f'撤销异常: {str(e)}'
            }

    def _get_board_type(self, board_key: str) -> str:
        """
        获取看板类型

        Returns:
            - 'system': 系统日期规则看板
            - 'manual': 手动管理看板
            - 'custom': 自定义看板（带Jira状态映射）
        """
        for col in self.board_config.get('columns', []):
            if col.get('key') == board_key:
                col_type = col.get('type', 'system')
                # 兼容旧配置：type=date 视为 system
                if col_type == 'date':
                    return 'system'
                return col_type
        # 未知看板默认视为系统看板
        return 'system'

    def _get_board_status_mapping(self, board_key: str) -> Optional[str]:
        """获取看板对应的Jira状态"""
        # 从看板配置中获取状态映射
        for col in self.board_config.get('columns', []):
            if col.get('key') == board_key:
                return col.get('jira_status')
        return None

    def _update_issue_status_in_jira(self, issue_key: str, new_status: str) -> Dict:
        """
        调用Jira API更新工单状态

        注意：此方法暂未完整实现，当前返回失败状态以明确告知用户。
        TODO: 实现实际的Jira transition API调用

        Args:
            issue_key: 工单编号
            new_status: 目标状态

        Returns:
            {'success': False, 'error': str} - 当前总是返回失败
        """
        # 当前功能未实现，明确返回失败
        return {
            'success': False,
            'error': 'Jira状态同步功能暂未实现，如需同步请手动在Jira中修改状态'
        }

    def _record_move_history(self, issue_key: str, source_board: str, target_board: str):
        """记录移动历史到本地存储"""
        history_file = os.path.join(os.path.dirname(__file__), "data/move_history.json")
        try:
            history = []
            if os.path.exists(history_file):
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)

            move_record = {
                'id': f"{issue_key}_{int(time.time())}",
                'issue_key': issue_key,
                'source_board': source_board,
                'target_board': target_board,
                'timestamp': datetime.now().isoformat()
            }
            history.append(move_record)

            # 只保留最近100条记录
            history = history[-100:]

            os.makedirs(os.path.dirname(history_file), exist_ok=True)
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)

        except Exception as e:
            print(f"[Move History Error] {e}")

    def get_move_history(self, issue_key: str = None, limit: int = 10) -> List[Dict]:
        """获取移动历史"""
        history_file = os.path.join(os.path.dirname(__file__), "data/move_history.json")
        try:
            if not os.path.exists(history_file):
                return []

            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)

            if issue_key:
                history = [h for h in history if h.get('issue_key') == issue_key]

            return history[-limit:]

        except Exception as e:
            print(f"[Get Move History Error] {e}")
            return []

    def generate_reply_content(self, issue_key: str, force: bool = False,
                               user_id: str = "", project_key: str = "",
                               force_pass_gate1: bool = False,
                               force_pass_gate2: bool = False,
                               context_only: bool = False) -> Dict:
        """
        基于AI分析结果生成智能回复内容

        生成规则:
        1. 获取工单的AI分析结果
        2. 使用Chroma搜索相似工单作为参考
        3. 整合solution_suggestion、相似工单解决方案、功能影响等
        4. 生成详细的回复内容（与解决方案字段一致）

        Args:
            issue_key: 工单编号
            force: 是否强制重新生成（忽略缓存）

        Returns:
            {
                "reply_content": "详细解决方案...",
                "solution_content": "详细解决方案...",
                "ai_analysis": {...},
                "word_count": 386
            }
        """
        # ── 富缓存早期命中：跳过 Gate1/Gate2/AI分析（生成时已通过所有门控）─────────
        if not force:
            _early_entry = get_cached_reply_entry(issue_key, {})
            if _early_entry is not None and "grounded_confidence" in _early_entry:
                _early_reply = _early_entry.get("reply_content", "")
                if _early_reply and len(_early_reply) >= 10 and not any(
                        _early_reply.startswith(p) for p in ("Error:", "模型调用失败:", "LLM 分析失败:")):
                    print(f"[GenerateReply] 富缓存早期命中，跳过AI分析加载: {issue_key}")
                    _kb_scored = _early_entry.get("kb_hits_scored") or []
                    return {
                        "reply_content": _early_reply,
                        "solution_content": _early_reply,
                        "ai_analysis": _early_entry.get("ai_analysis"),
                        "word_count": len(_early_reply.replace('\n', '').replace(' ', '')),
                        "cached": True,
                        "suggested_reply_method": _early_entry.get("suggested_reply_method"),
                        "suggested_issue_type": _early_entry.get("suggested_issue_type"),
                        "generation_method": _early_entry.get("generation_method", "cached"),
                        "kb_sources": [r.get("title", "") for r in _kb_scored],
                        "kb_evidence_count": len(_kb_scored),
                        "kb_hits_scored": _kb_scored,
                        "similar_issues_scored": _early_entry.get("similar_issues_scored") or [],
                        "examples_used_count": 0,
                        "style_rules_applied": True,
                        "grounded_confidence": _early_entry.get("grounded_confidence"),
                        "reply_strategy": _early_entry.get("reply_strategy") or "",
                        "reply_gateway": _early_entry.get("reply_gateway"),
                        "issue_key": issue_key,
                        "reuse_score": _early_entry.get("reuse_score"),
                    }
        # ─────────────────────────────────────────────────────────────────────

        # ── context_only（纯 MCP 委托）：跳过会 call_llm / 写 Jira 的 gate ──────
        # Gate1 completeness / Gate2 classification 内部都会 llm.call_llm；Gate2 高置信
        # 还会 move_issue_to_board(sync_jira=True) 写 Jira；Gate3 direct 复用会 supervise→LLM。
        # context_only 只做纯检索的证据收集（KB/相似工单/specificity 规则版），不调 LLM、无副作用。
        if context_only:
            force_pass_gate1 = True
            force_pass_gate2 = True
        # ─────────────────────────────────────────────────────────────────────

        # ── Gate 1: 信息完整性检查 ─────────────────────────────────────────────
        if not force_pass_gate1:
            gate1_result = self._run_gate1_completeness(issue_key)
            if gate1_result is not None:
                return gate1_result
        # ─────────────────────────────────────────────────────────────────────

        # ── Gate 2: 分类正确性检查 ─────────────────────────────────────────────
        if not force_pass_gate2:
            gate2_result = self._run_gate2_classification(issue_key)
            if gate2_result is not None:
                return gate2_result
        # ─────────────────────────────────────────────────────────────────────

        # Gate 4 具体度等级 —— 在 KB 证据填充后更新，这里先初始化默认值
        _specificity_level = "medium"

        # 获取AI分析结果
        ai_analysis = None

        # 1. 从向量缓存获取（跳过规则引擎降级结果，避免缓存垃圾内容）
        try:
            cached = self.vector_store.get_cached_analysis(issue_key)
            if cached and cached.get('model_used') != 'rule_engine':
                ai_analysis = cached
        except Exception as e:
            print(f"[GenerateReply] 向量缓存获取失败: {e}")

        # 2. 从文件缓存获取（同样跳过规则引擎降级结果）
        if not ai_analysis:
            file_cached = load_file_cached_analysis(issue_key, PROJECT_ROOT)
            if file_cached and file_cached.get('model_used') != 'rule_engine':
                ai_analysis = file_cached

        if not ai_analysis:
            # 无 AI 分析时，尝试从看板缓存获取工单信息，直接走训练器
            issue_info = self._get_issue_from_cache(issue_key)
            if issue_info:
                ai_analysis = {
                    'issue_title': issue_info.get('summary', ''),
                    'issue_description': issue_info.get('description', ''),
                    'problem_analysis': '',
                    'solution_suggestion': '',
                    'functionality_impact': '',
                    'recommended_team': '',
                    'recommended_role': '',
                    'confidence': 0,
                }
                print(f"[GenerateReply] 无AI分析，使用工单原始信息走训练器: {issue_key}")
            else:
                return {
                    "reply_content": "",
                    "solution_content": "",
                    "ai_analysis": None,
                    "word_count": 0,
                    "error": "未找到AI分析结果，请先进行分析"
                }

        # 3. 旧格式 cache entry 检测（非强制模式下）——触发一次性升级到富 entry
        _bad_reply_prefixes = ("Error:", "模型调用失败:", "LLM 分析失败:")
        if not force:
            cached_reply = get_cached_reply(issue_key, ai_analysis)
            if cached_reply and any(cached_reply.startswith(p) for p in _bad_reply_prefixes):
                print(f"[GenerateReply] 缓存内容含错误文本，跳过: {issue_key} ({cached_reply[:40]})")
                cached_reply = None
            if cached_reply and len(cached_reply) < 10:
                cached_reply = None
            if cached_reply:
                # 旧 entry（无 grounded_confidence）：fall-through 到完整生成，写回富 entry
                print(f"[GenerateReply] 旧 cache entry，触发一次性升级: {issue_key}")

        # 4. 使用Chroma搜索相似工单
        similar_issues = []
        try:
            # 构建查询：使用当前工单的标题和描述
            _ana = ai_analysis or {}
            query = f"{_ana.get('issue_title', '')} {_ana.get('issue_description', '')}".strip()
            # 4a-early: ai_analysis 无 title 时提前从 Jira 缓存补充（4b 的完整逻辑在下方，这里只取 query 用）
            if not query:
                try:
                    _pre_issue = self._get_issue_from_cache(issue_key) or {}
                    query = f"{_pre_issue.get('summary', '')} {_pre_issue.get('description', '')}".strip()[:300]
                except Exception:
                    pass
            if not query:
                try:
                    _vs_issue = self.search_engine.vector_store.get_issue_by_key(issue_key) or {}
                    query = _vs_issue.get('document', _vs_issue.get('summary', ''))[:300]
                except Exception:
                    pass
            if query.strip():
                search_results = self.search_engine.search(query, top_k=5, min_score=0.7)
                similar_issues = search_results.get('results', [])
                # 过滤掉当前工单
                similar_issues = [r for r in similar_issues if r.get('key') != issue_key]
                # MS-3: 关键词后置降权 — query 核心词不命中则 score ×0.5
                import re as _re_ms3
                _kw_stop = {"使用", "操作", "配置", "如何", "怎么", "问题", "设置", "方案", "说明", "功能", "系统"}
                _kw_tokens = [t for t in _re_ms3.findall(r'[一-鿿]{2,}', query) if t not in _kw_stop]
                if _kw_tokens:
                    for _r in similar_issues:
                        _haystack = (((_r.get('summary') or _r.get('display_summary') or '') + ' ' +
                                      (_r.get('content') or _r.get('full_text') or ''))).lower()
                        if not any(t in _haystack for t in _kw_tokens):
                            _r['score'] = round(_r.get('score', 0) * 0.5, 4)
                print(f"[GenerateReply] 找到 {len(similar_issues)} 个相似工单作为参考")
        except Exception as e:
            print(f"[GenerateReply] 搜索相似工单失败: {e}")

        # 4b. 补充缺失的标题/描述（旧AI缓存可能缺少这些字段）
        if not (ai_analysis.get('issue_title') or '').strip():
            issue_info = self._get_issue_from_cache(issue_key)
            if issue_info:
                ai_analysis['issue_title'] = issue_info.get('summary', '')
                ai_analysis['issue_description'] = issue_info.get('description', '')
                print(f"[GenerateReply] AI缓存缺标题，从看板补充: {ai_analysis['issue_title'][:50]}")
            else:
                pass  # 看板缓存 miss，暂无标题

        # 4d. 注入部署模式（公有云/私有云/专属云）+ 产品版本
        # 优先从看板缓存取，缓存 miss 时通过 Jira API 实时拉取 customfield_13529
        if not ai_analysis.get('deploy_mode'):
            try:
                _issue_meta = self._get_issue_from_cache(issue_key)
                if _issue_meta:
                    _dm = _issue_meta.get('deploy_mode', '') if isinstance(_issue_meta, dict) else getattr(_issue_meta, 'deploy_mode', '')
                    _pv = _issue_meta.get('product_version', '') if isinstance(_issue_meta, dict) else getattr(_issue_meta, 'product_version', '')
                    if _dm:
                        ai_analysis['deploy_mode'] = _dm
                    if _pv:
                        ai_analysis['product_version'] = _pv
            except Exception:
                pass


        if ai_analysis.get('deploy_mode'):
            print(f"[GenerateReply] 部署模式: {ai_analysis['deploy_mode']} 产品版本: {(ai_analysis.get('product_version',''))[:40]}")

        # 4c. 附件图片分析（GLM-5V-Turbo）—— 结果注入 ai_analysis，供下游 LLM 看到"图里说了什么"
        try:
            image_analysis = self._analyze_attachment_images(issue_key, max_images=2)
            if image_analysis:
                ai_analysis['image_analysis'] = image_analysis
        except Exception as e:
            print(f"[GenerateReply] 图片分析跳过: {e}")

        # 5. KB证据搜索（双路：标题搜 + 关键词搜，合并去重）
        module_category = self._resolve_module_category(ai_analysis,
                                                         issue_key=issue_key,
                                                         cache_fn=self._get_issue_from_cache)
        if module_category:
            print(f"[GenerateReply] 模块感知: category='{module_category}' (keyword match from title)")
        kb_evidence = []
        try:
            import re as _re_kb
            kb_service = KnowledgeRuntimeService()

            # A路：清洗后的标题搜索（用统一构造器，空字段时从 Jira 缓存回填）
            from services.query_builder import build_issue_query as _build_query
            kb_query = _build_query(issue_key, ai_analysis,
                                    fields=("issue_title",),
                                    max_len=300,
                                    cache_fn=self._get_issue_from_cache)
            # 剥离【xxx】前缀噪声 + URL + 帐户分享链接
            kb_query = _re_kb.sub(r'^【[^】]*】\s*', '', kb_query).strip()
            kb_query = _re_kb.sub(r'【[^】]*链接[^】]*】.*$', '', kb_query).strip()
            kb_query = _re_kb.sub(r'https?://\S+', '', kb_query).strip()

            seen_names = set()
            if kb_query.strip():
                kb_results_a = kb_service.search_bundle(kb_query, top_k=6, category=module_category)
                for item in (kb_results_a or {}).get('items', [])[:4]:
                    name = item.get('name', '')
                    if name not in seen_names:
                        kb_evidence.append(item)
                        seen_names.add(name)

            # B路：基于领域词表（TERM_EXPANSIONS）做精准关键词搜索
            from kb_runtime_service import TERM_EXPANSIONS as _TERM_EXP
            full_text = _build_query(issue_key, ai_analysis,
                                     fields=("issue_title", "issue_description", "problem_analysis"),
                                     max_len=800,
                                     cache_fn=self._get_issue_from_cache)
            domain_keywords = []
            for term, expansions in _TERM_EXP.items():
                if term in full_text:
                    # 完整匹配：如"加签"出现在标题中
                    domain_keywords.append(term)
                    domain_keywords.extend(expansions[:2])
                elif len(term) >= 3 and term[:2] in full_text:
                    # 子串模糊匹配：如标题含"字段"→ 命中"字段权限"
                    domain_keywords.append(term)
            # 如果领域词匹配不足，补充 2-4 字中文短语
            if len(domain_keywords) < 3:
                raw_candidates = _re_kb.findall(r'[\u4e00-\u9fff]{2,4}', full_text)
                stop_phrases = {'支持问题', '服务中心', '请问', '如何', '怎么', '什么', '附件所示', '如附件', '入职', '员工', '当某一', '审批通过', '通过后'}
                for w in raw_candidates:
                    if w not in stop_phrases and w not in domain_keywords:
                        domain_keywords.append(w)
                    if len(domain_keywords) >= 8:
                        break
            keyword_query = " ".join(dict.fromkeys(domain_keywords))  # 去重保序

            if keyword_query.strip():
                kb_results_b = kb_service.search_bundle(keyword_query, top_k=4, category=module_category)
                for item in (kb_results_b or {}).get('items', [])[:4]:
                    name = item.get('name', '')
                    if name not in seen_names:
                        kb_evidence.append(item)
                        seen_names.add(name)

            # 按 score 降序排列，取 top 4（让 B 路高分命中有机会挤掉 A 路低分噪声）
            kb_evidence.sort(key=lambda x: x.get('score', 0), reverse=True)
            kb_evidence = kb_evidence[:4]
            if kb_evidence:
                print(f"[GenerateReply] KB搜索 query='{kb_query[:60]}' keywords='{keyword_query[:40]}' category='{module_category}' → 命中 {len(kb_evidence)} 条: {[i.get('name') for i in kb_evidence]}")
        except Exception as e:
            print(f"[GenerateReply] KB搜索失败(降级到无KB模式): {e}")

        # ── Gate 4: 操作具体度检查 ────────────────────────────────────────────
        _specificity_level = self._compute_specificity_level(kb_evidence)
        try:
            import yaml as _yaml
            _g4_cfg = _yaml.safe_load(open(PROJECT_ROOT / "config" / "reply_gates.yaml"))
            _g4_enabled = _g4_cfg.get("gates", {}).get("specificity", {}).get("enabled", False)
        except Exception:
            _g4_enabled = False

        if _g4_enabled and _specificity_level == "none":
            return {
                "reply_content": "",
                "solution_content": "",
                "ai_analysis": ai_analysis,
                "word_count": 0,
                "gate": "specificity",
                "specificity_level": "none",
                "gate_decisions": {"specificity": {"passed": False, "reason": "no_evidence"}},
                "suggested_reply_method": None,
                "suggested_issue_type": None,
                "generation_method": "gate_blocked",
                "kb_sources": [],
                "kb_evidence_count": 0,
                "examples_used_count": 0,
                "style_rules_applied": False,
            }
        # ─────────────────────────────────────────────────────────────────────

        # 6. 搜索用户历史回复范例（训练器）
        reply_examples = []
        try:
            query = _build_query(issue_key, ai_analysis, max_len=300,
                                 cache_fn=self._get_issue_from_cache)
            if query:
                # project_key: 优先使用 API 层传入的归属（含 user.current_project），
                # 未传时退回 issue_key 前缀推断
                _proj_key = project_key or (issue_key.split("-")[0].upper() if "-" in issue_key else "")
                reply_examples = self.reply_trainer.search_examples(query, top_k=3, module=module_category or "", project_key=_proj_key)
                if reply_examples:
                    print(f"[GenerateReply] 找到 {len(reply_examples)} 个回复范例")
        except Exception as e:
            print(f"[GenerateReply] 搜索回复范例失败: {e}")

        # ── Gate 3: 历史复用评估 ──────────────────────────────────────────────
        _reuse_candidate = None
        _issue_info_g3 = {}  # 初始化，确保 direct 分支可访问
        try:
            from services.reply_reuse_evaluator import evaluate_reuse
            _issue_meta_g3 = self._get_issue_from_cache(issue_key)
            if _issue_meta_g3:
                _issue_info_g3 = _issue_meta_g3 if isinstance(_issue_meta_g3, dict) else {
                    k: getattr(_issue_meta_g3, k, None)
                    for k in ("product_version", "deploy_mode")
                    if getattr(_issue_meta_g3, k, None)
                }
            _product_version_g3 = _issue_info_g3.get("product_version", "")
            _reuse_candidate = evaluate_reuse(
                reply_examples=reply_examples,
                current_product_version=_product_version_g3,
                current_module=module_category or "",
            )
        except Exception as _e_g3:
            print(f"[Gate3] reuse evaluation error, skipping: {_e_g3}")

        _g3_downgrade_reason: str | None = None  # 非 None 表示 direct 路径被降级，原因写入 G3 gate
        # context_only 不走 direct 复用短路（其中 supervise 会 call_llm），落到下方证据收集 + context_only 返回
        if _reuse_candidate is not None and _reuse_candidate.tier == "direct" and not context_only:
            # 直接复用：个性化替换 → 污染校验 + G5 supervisor → 通过则短路返回，否则降级 llm_blend
            try:
                from services.reply_personalize import personalize_reply
                _best_ex = _reuse_candidate.example
                _personalized = personalize_reply(
                    reply_text=_best_ex.get("reply", ""),
                    new_issue_key=issue_key,
                    old_issue_key=_best_ex.get("issue_key", ""),
                    new_version=_issue_info_g3.get("product_version", ""),
                    new_customer_name="",
                )
            except Exception as _ep:
                print(f"[Gate3] personalize error: {_ep}")
                _personalized = _reuse_candidate.example.get("reply", "")

            # 污染校验：复用文本与原工单描述高度重叠 → 内容污染
            import difflib as _difflib
            _src_desc = (ai_analysis or {}).get("issue_description", "") or ""
            _pollution_ratio = (
                _difflib.SequenceMatcher(None, _personalized[:500], _src_desc[:500]).ratio()
                if _src_desc else 0.0
            )
            _polluted = _pollution_ratio > 0.4

            # G5 supervisor 在 direct 路径上强制运行（supervisor 是最后一道质量墙）
            _sup_g3 = None
            _sup_g3_failed = False
            try:
                from agents.reply_supervisor_agent import supervise as _supervise_g3
                _sup_g3 = _supervise_g3(
                    issue_key=issue_key,
                    issue_title=(ai_analysis or {}).get("issue_title", ""),
                    issue_description=_src_desc,
                    generated_reply=_personalized,
                    kb_evidence=[],
                    gate_decisions={"reuse": {"tier": "direct", "composite": _reuse_candidate.composite_score}},
                    main_provider="minimax",
                )
                _sup_g3_failed = _sup_g3 is None or (
                    _sup_g3.supervisor_score is not None and _sup_g3.supervisor_score < 0.6
                )
            except Exception as _e_sup3:
                print(f"[Gate3] supervisor on direct path failed: {_e_sup3}")
                _sup_g3_failed = True

            _downgrade_reason = None
            if _polluted:
                _downgrade_reason = f"pollution_ratio={_pollution_ratio:.2f}"
            elif _sup_g3_failed:
                _downgrade_reason = "supervisor_fail"

            if _downgrade_reason:
                print(f"[Gate3] {issue_key}: downgrade direct→llm_blend ({_downgrade_reason})")
                _reuse_candidate.tier = "llm_blend"
                _g3_downgrade_reason = _downgrade_reason
                # 不短路，继续走完整 5 网关流程
            else:
                # 通过验证：短路返回，附带 G5 结果
                _g3_gateway = None
                if _sup_g3 is not None:
                    try:
                        _g3_gateway = {
                            "version": "v2",
                            "gates": {
                                "G5_supervisor": {
                                    "verdict": "pass" if (_sup_g3.supervisor_score or 0) >= 0.6 else "warn",
                                    "score": _sup_g3.supervisor_score,
                                    "risk_flags": _sup_g3.risk_flags or [],
                                    "gate_enabled": _sup_g3.gate_enabled,
                                }
                            },
                        }
                    except Exception:
                        pass
                suggested_fields = self._suggest_reply_fields(ai_analysis, reply_examples)
                print(f"[Gate3] {issue_key}: direct reuse (composite={_reuse_candidate.composite_score:.3f})")
                _g3_extra = {
                    "grounded_confidence": None,
                    "kb_hits_scored": [],
                    "similar_issues_scored": [],
                    "reply_strategy": f"Gate3 direct reuse (composite={_reuse_candidate.composite_score:.3f})",
                    "reply_gateway": _g3_gateway,
                    "suggested_reply_method": suggested_fields.get('reply_method'),
                    "suggested_issue_type": suggested_fields.get('issue_type'),
                    "generation_method": "reuse_direct",
                    "reuse_score": _reuse_candidate.composite_score,
                    "downgrade_reason": None,
                }
                if not force:
                    save_cached_reply(issue_key, ai_analysis, _personalized, extra_fields=_g3_extra)
                return {
                    "reply_content": _personalized,
                    "solution_content": _personalized,
                    "ai_analysis": ai_analysis,
                    "word_count": len(_personalized.replace('\n', '').replace(' ', '')),
                    "cached": False,
                    "suggested_reply_method": suggested_fields.get('reply_method'),
                    "suggested_issue_type": suggested_fields.get('issue_type'),
                    "generation_method": "reuse_direct",
                    "specificity_level": _specificity_level,
                    "reuse_score": _reuse_candidate.composite_score,
                    "kb_sources": [],
                    "kb_evidence_count": 0,
                    "examples_used_count": 1,
                    "style_rules_applied": True,
                    "issue_key": issue_key,
                    "reply_gateway": _g3_gateway,
                    "grounded_confidence": None,
                    "similar_issues_scored": [],
                    "reply_strategy": f"Gate3 direct reuse (composite={_reuse_candidate.composite_score:.3f})",
                    "downgrade_reason": None,
                }

        elif _reuse_candidate is not None and _reuse_candidate.tier == "skip":
            # 证据弱：清空范例，只靠 KB evidence
            print(f"[Gate3] {issue_key}: low composite ({_reuse_candidate.composite_score:.3f}), skipping examples")
            reply_examples = []
        # tier == "llm_blend": 继续现有流程，不做修改
        # ─────────────────────────────────────────────────────────────────────

        # ── context_only：纯 MCP 委托模式 ───────────────────────────────────
        # 跑完所有非 LLM 的上下文收集 + gate 判定后，在 LLM 生成之前返回
        # 证据 + prompt 模板，交调用方 Agent（OpenClaw/WorkBuddy/Claude Code）
        # 用各自的 LLM 生成回复正文。服务侧不调用 LLM。
        if context_only:
            _ctx_kb = [
                {
                    "title": item.get("name", item.get("title", "")),
                    "score": round(min(1.0, max(0.0, float(item.get("score") or 0) / 100.0)), 4),
                    "category": item.get("category", ""),
                    "url": item.get("url", ""),
                    "text": (item.get("chunk_text") or item.get("summary") or "")[:500],
                }
                for item in (kb_evidence or [])[:5]
            ]
            _ctx_sim = [
                {
                    "key": s.get("key", ""),
                    "score": round(float(s.get("score") or 0), 4),
                    "summary": ((s.get("summary") or s.get("content") or "")[:200]),
                }
                for s in (similar_issues or [])[:5]
                if (s.get("score") or 0) >= 0.6
            ]
            _ai = ai_analysis if isinstance(ai_analysis, dict) else {}
            _issue_title = _ai.get("issue_title", "")
            _issue_desc = _ai.get("issue_description", "")
            _style_rules = ""
            try:
                _style_rules = self.reply_trainer.get_style_rules() or ""
            except Exception:
                pass
            _kb_block = "\n".join(
                f"- [{k['title']}]（相关度 {int(k['score'] * 100)}%）: {k['text']}" for k in _ctx_kb
            ) or "（无知识库命中）"
            _sim_block = "\n".join(
                f"- {s['key']}（相似度 {int(s['score'] * 100)}%）: {s['summary']}" for s in _ctx_sim
            ) or "（无相似工单）"
            _user_prompt = (
                f"工单：{issue_key}\n标题：{_issue_title}\n描述：{_issue_desc}\n\n"
                f"【知识库证据】\n{_kb_block}\n\n"
                f"【相似历史工单】\n{_sim_block}\n\n"
                f"请基于以上证据为该工单撰写专业、可执行的解决方案回复。仅依据证据作答，"
                f"不要杜撰；若证据不足，请说明还需补充哪些信息。"
            )
            _sys_prompt = (
                "你是用友 BIP 工单支持专家。依据提供的知识库证据与相似历史工单，"
                "生成准确、具体、可操作的解决方案回复。"
                + (f"\n\n【风格规则】\n{_style_rules}" if _style_rules else "")
            )
            return {
                "context_only": True,
                "issue_key": issue_key,
                "generation_method": "context_only",
                "reply_content": "",
                "solution_content": "",
                "ai_analysis": ai_analysis,
                "module_category": module_category,
                "specificity_level": _specificity_level,
                "kb_hits_scored": _ctx_kb,
                "kb_evidence_count": len(_ctx_kb),
                "kb_sources": [k["title"] for k in _ctx_kb],
                "similar_issues_scored": _ctx_sim,
                "examples_used_count": len(reply_examples) if reply_examples else 0,
                "reuse_score": (_reuse_candidate.composite_score if _reuse_candidate else None),
                "gate_decisions": {
                    "completeness": "passed",
                    "specificity": ("passed" if _specificity_level not in ("none", None) else "blocked"),
                    "reuse": ("candidate" if _reuse_candidate else "none"),
                },
                "prompt_template": {"system": _sys_prompt, "user": _user_prompt},
            }

        # 7. 生成解决方案内容（始终用 LLM，即使无 KB 证据/范例也可基于 ai_analysis + product_facts 生成）
        solution_content = self._generate_styled_reply(ai_analysis, similar_issues, reply_examples, kb_evidence, module_category=module_category, user_id=user_id, specificity_level=_specificity_level)

        # ── Gate 5: 独立监督审计 + 多维加权 Auto-Reply 决策 ──────────────────
        _supervisor_result = None
        _auto_reply_decision = None
        _is_vip_g5 = False
        _customer_name_g5 = ""
        _product_type_g5 = "standard"
        try:
            from agents.reply_supervisor_agent import supervise as _supervise
            # 获取工单信息（已在 Gate 3 中缓存到 _issue_info_g3，直接复用）
            _issue_title_g5 = ai_analysis.get("issue_title", "")
            _issue_desc_g5 = ai_analysis.get("issue_description", "")
            _supervisor_result = _supervise(
                issue_key=issue_key,
                issue_title=_issue_title_g5,
                issue_description=_issue_desc_g5,
                generated_reply=solution_content or "",
                kb_evidence=kb_evidence or [],
                gate_decisions={"completeness": {}, "specificity": {"level": _specificity_level}},
                main_provider="minimax",
            )
        except Exception as _e_g5:
            print(f"[Gate5] supervisor error, skipping: {_e_g5}")

        if _supervisor_result is not None and _supervisor_result.gate_enabled:
            try:
                from services.auto_reply_decider import decide as _decide
                from services.customer_priority_tagger import is_key_customer as _is_key

                # 产品类型推断
                _issue_info_g5 = getattr(self, '_issue_info_g3', None) or self._get_issue_from_cache(issue_key) or {}
                _product_version_g5 = (_issue_info_g5.get("product_version") or "").lower()
                if "yonsuite" in _product_version_g5 or "公有云" in _product_version_g5:
                    _product_type_g5 = "yonsuite"
                elif "客开" in (ai_analysis.get("issue_description") or ""):
                    _product_type_g5 = "custom"
                else:
                    _product_type_g5 = "standard"

                _customer_name_g5 = _issue_info_g5.get("customer_name", "")
                _is_vip_g5 = _is_key(_customer_name_g5)

                _reuse_matched_g5 = (
                    _reuse_candidate is not None
                    and _reuse_candidate.composite_score >= 0.85
                )
                _auto_reply_decision = _decide(
                    supervisor_score=_supervisor_result.supervisor_score,
                    product_type=_product_type_g5,
                    is_key_customer=_is_vip_g5,
                    reuse_matched=_reuse_matched_g5,
                    risk_flags=_supervisor_result.risk_flags or [],
                )
                print(
                    f"[Gate5] {issue_key}: supervisor={_supervisor_result.supervisor_score if _supervisor_result.supervisor_score is not None else 'llm_failed'} "
                    f"flags={_supervisor_result.risk_flags} "
                    f"auto_reply={_auto_reply_decision.auto_reply} ({_auto_reply_decision.action})"
                )
            except Exception as _e_ar:
                print(f"[Gate5] auto_reply_decider error: {_e_ar}")
        # ─────────────────────────────────────────────────────────────────────

        # 7. AI 智能推荐字段值（优先从范例历史数据提取，其次关键词匹配）
        suggested_fields = self._suggest_reply_fields(ai_analysis, reply_examples)

        # 缓存在 grounded_conf + gateway 计算完成后写入（见下方 _patch_analysis_cache 之后）

        _transfer_pending = getattr(self, '_gate2_transfer_pending', None)
        self._gate2_transfer_pending = None  # reset

        # ── Reply Gateway v2 ────────────────────────────────────────────────
        try:
            _gw_ticket_meta = {
                "project": issue_key.split("-")[0] if issue_key else "",
                "issue_type": (ai_analysis or {}).get("issue_type", ""),
                "description": (ai_analysis or {}).get("issue_description", ""),
                "summary": (ai_analysis or {}).get("issue_title", ""),
                "product_version": (ai_analysis or {}).get("product_version", ""),
                "customer_name": _customer_name_g5,
            }
            _gw_result = self.reply_gateway.run(
                issue_key,
                ai_analysis or {},
                _gw_ticket_meta,
                only=["G1", "G2", "G3", "G4"],
                kb_evidence=kb_evidence,
                reply_examples=reply_examples,
                generated_reply="",
            )
            self.reply_gateway.inject_g5_from_supervisor(_gw_result, issue_key, _supervisor_result)
            # 若 direct 路径降级，将原因注入 G3_reuse 以便前端展示
            if _g3_downgrade_reason and isinstance(_gw_result.get("gates"), dict):
                _gw_result["gates"].setdefault("G3_reuse", {})["downgrade_reason"] = _g3_downgrade_reason
        except Exception as _e_gw:
            print(f"[Gateway] assembly failed (non-blocking): {_e_gw}")
            _gw_result = {"version": "v2", "gates": {}, "display_cards": [], "final_action": "", "extra_operations": []}
        # ────────────────────────────────────────────────────────────────────

        # ── Gate 决策埋点 + Staging ──────────────────────────────────────────
        try:
            from services import gate_decision_log as _gdl
            from services import pending_approval_store as _pas

            _action_key_g = _auto_reply_decision.action if _auto_reply_decision else "manual_review"
            if _action_key_g == "pending_batch_approve":
                _final_action_g = "pending_batch_approve"
            elif _action_key_g in ("auto_reply", "auto_reply_low_risk"):
                _final_action_g = "auto_replied_low_risk" if _is_vip_g5 else "auto_replied_normal"
            elif _action_key_g == "human_required" or (_supervisor_result and _supervisor_result.risk_flags):
                _final_action_g = "needs_decision"
            elif _action_key_g == "needs_decision":
                _final_action_g = "needs_decision"
            else:
                _final_action_g = "manual"

            _op_steps_g: list = []
            _pending_approval_id_g: str | None = None
            if _final_action_g == "pending_batch_approve":
                _op_steps_g = [
                    f"AI 生成回复 composite_score={_auto_reply_decision.composite_score:.0%}",
                    "非重点客户 + 高置信 → 加入批量审批队列",
                    "待操作员批准后触发 reply_and_close_via_transition",
                ]
                _pending_approval_id_g = _pas.add(
                    issue_key=issue_key,
                    reply_content=solution_content or "",
                    decision={
                        "composite_score": _auto_reply_decision.composite_score,
                        "threshold": _auto_reply_decision.threshold,
                        "action": _auto_reply_decision.action,
                        "product_type": _auto_reply_decision.product_type,
                    },
                    ai_fields={},
                    issue_summary=(ai_analysis or {}).get("issue_title", ""),
                    customer_name=_customer_name_g5,
                    is_key_customer=_is_vip_g5,
                    product_priority=_product_type_g5,
                )

            _gdl.log_gate_decision(
                issue_key=issue_key,
                project=(ai_analysis or {}).get("project", ""),
                issue_type=(ai_analysis or {}).get("issue_type", ""),
                customer_name=_customer_name_g5,
                is_key_customer=_is_vip_g5,
                product_priority=_product_type_g5,
                gate_decisions={
                    "completeness": "passed",
                    "classification": "skipped",
                    "reuse": "passed" if _reuse_candidate else "skipped",
                    "specificity": "passed" if _specificity_level else "skipped",
                    "supervisor": ("passed" if (_supervisor_result and _supervisor_result.supervisor_score is not None and getattr(_supervisor_result, 'status', 'ok') == 'ok') else ("blocked" if _supervisor_result else "skipped")),
                },
                supervisor_score=_supervisor_result.supervisor_score if _supervisor_result else None,
                risk_flags=_supervisor_result.risk_flags if _supervisor_result else [],
                reuse_score=_reuse_candidate.composite_score if _reuse_candidate else None,
                specificity_level=_specificity_level,
                auto_reply_decision={
                    "auto_reply": _auto_reply_decision.auto_reply,
                    "composite_score": _auto_reply_decision.composite_score,
                    "threshold": _auto_reply_decision.threshold,
                    "action": _auto_reply_decision.action,
                } if _auto_reply_decision else None,
                final_action=_final_action_g,
                reply_summary=(solution_content or "")[:80],
                operation_steps=_op_steps_g,
                blocked_by=_auto_reply_decision.blocked_by if _auto_reply_decision else [],
                reply_gateway=_gw_result,
            )
        except Exception as _e_log:
            print(f"[GateLog] 埋点失败（非阻断）: {_e_log}")
        # ────────────────────────────────────────────────────────────────────

        # ── Inject auto_decision into reply_gateway v2 ───────────────────────
        _ad = locals().get('_auto_reply_decision')
        if _gw_result is not None and _ad is not None:
            _gw_result["auto_decision"] = {
                "composite_confidence": _ad.composite_score,
                "threshold_hit": _ad.action,
                "action": locals().get('_final_action_g', ''),
                "decided_by": "auto_reply_decider",
                "blocked_by": list(_ad.blocked_by or []),
            }
        # ─────────────────────────────────────────────────────────────────────

        # Compute grounded multi-source confidence
        from services.confidence_calculator import calculate_grounded_confidence as _calc_gc
        _gc_sim = [
            {"key": s.get("key", ""), "score": float(s.get("score") or 0),
             "summary": (s.get("summary") or s.get("content") or "")[:80]}
            for s in (similar_issues or [])[:5]
        ]
        _gc_kb = [
            {"title": item.get("name", item.get("title", "")),
             "score": float(item.get("score") or 0),
             "category": item.get("category", "")}
            for item in (kb_evidence or [])[:5]
        ]
        _grounded_conf = _calc_gc(
            similar_issues_scored=_gc_sim or None,
            kb_evidence=_gc_kb or None,
            supervisor_score=(_supervisor_result.supervisor_score if _supervisor_result else None),
            ai_raw_confidence=ai_analysis.get("confidence"),
        )
        _sim_cache = [
            {"key": s.get("key", ""), "score": round(float(s.get("score") or 0), 4),
             "summary": ((s.get("summary") or s.get("content") or "")[:80])}
            for s in (similar_issues or [])[:5]
        ]
        _strategy_cache = ((_supervisor_result.rationale if _supervisor_result else None)
                           or ai_analysis.get("reasoning") or "")
        _patch_analysis_cache(issue_key,
            grounded_confidence_score=_grounded_conf.get("score"),
            grounded_evidence_status=_grounded_conf.get("evidence_status"),
            similar_issues_cached=_sim_cache,
            reply_strategy_cached=_strategy_cache)

        # 写入富 cache entry（含所有展示字段，后续命中时零重算）
        if not force:
            _kb_scored_cache = [
                {"title": item.get("name", item.get("title", "")),
                 "score": round(min(1.0, max(0.0, float(item.get("score") or 0) / 100.0)), 4),
                 "category": item.get("category", ""),
                 "url": item.get("url", ""),
                 "text": (item.get("chunk_text") or item.get("summary") or "")[:200]}
                for item in (kb_evidence or [])[:5]
            ]
            _sim_scored_cache = [
                {"key": s.get("key", ""), "score": round(float(s.get("score") or 0), 4),
                 "summary": (s.get("summary") or s.get("content") or "")[:80]}
                for s in (similar_issues or [])[:5]
                if (s.get("score") or 0) >= 0.25
            ]
            save_cached_reply(issue_key, ai_analysis, solution_content, extra_fields={
                "grounded_confidence": _grounded_conf,
                "kb_hits_scored": _kb_scored_cache,
                "similar_issues_scored": _sim_scored_cache,
                "reply_strategy": _strategy_cache,
                "reply_gateway": locals().get("_gw_result"),
                "suggested_reply_method": suggested_fields.get('reply_method'),
                "suggested_issue_type": suggested_fields.get('issue_type'),
                "generation_method": "llm",
            })
        else:
            print(f"[GenerateReply] 强制重新生成，不保存缓存: {issue_key}")

        return {
            "reply_content": solution_content,
            "solution_content": solution_content,
            "ai_analysis": ai_analysis,
            "word_count": len(solution_content.replace('\n', '').replace(' ', '')),
            "cached": False,
            "suggested_reply_method": suggested_fields.get('reply_method'),
            "suggested_issue_type": suggested_fields.get('issue_type'),
            "generation_method": "llm",
            "specificity_level": _specificity_level,
            "kb_sources": [item.get('name', '') for item in kb_evidence[:4]] if kb_evidence else [],
            "kb_evidence_count": len(kb_evidence) if kb_evidence else 0,
            "kb_hits_scored": [
                {"title": item.get("name", item.get("title", "")),
                 "score": round(min(1.0, max(0.0, float(item.get("score") or 0) / 100.0)), 4),
                 "category": item.get("category", ""),
                 "url": item.get("url", ""),
                 "text": (item.get("chunk_text") or item.get("summary") or "")[:200]}
                for item in (kb_evidence or [])[:5]
            ],
            "grounded_confidence": _grounded_conf,
            "examples_used_count": len(reply_examples) if reply_examples else 0,
            "style_rules_applied": bool(self.reply_trainer.get_style_rules()),
            "reuse_score": _reuse_candidate.composite_score if _reuse_candidate else None,
            "supervisor_audit": {
                "score": _supervisor_result.supervisor_score if _supervisor_result else None,
                "risk_flags": _supervisor_result.risk_flags if _supervisor_result else [],
                "step_safety": _supervisor_result.step_safety if _supervisor_result else None,
                "rationale": _supervisor_result.rationale if _supervisor_result else "",
                "provider_used": _supervisor_result.provider_used if _supervisor_result else "",
            } if _supervisor_result else None,
            "auto_reply_decision": {
                "auto_reply": _auto_reply_decision.auto_reply if _auto_reply_decision else False,
                "composite_score": _auto_reply_decision.composite_score if _auto_reply_decision else None,
                "threshold": _auto_reply_decision.threshold if _auto_reply_decision else None,
                "action": _auto_reply_decision.action if _auto_reply_decision else "manual_review",
            } if _auto_reply_decision else None,
            "transfer_to": _transfer_pending,
            "pending_approval_id": locals().get("_pending_approval_id_g"),
            "similar_issues_scored": [
                {
                    "key": s.get("key", ""),
                    "score": round(float(s.get("score") or 0), 4),
                    "summary": ((s.get("summary") or s.get("content") or "")[:80]),
                }
                for s in (similar_issues or [])[:5]
                if (s.get("score") or 0) >= 0.6
            ],
            "is_key_customer": _is_vip_g5,
            "reply_strategy": (
                (_supervisor_result.rationale if _supervisor_result else None)
                or (ai_analysis.get("reasoning") or "")
            ),
            "reply_gateway": _gw_result,
        }

    def _analyze_attachment_images(self, issue_key: str, max_images: int = 2) -> str:
        """图片附件视觉分析（暂不支持，返回空串）。"""
        return ""

    def _analyze_attachment_images_impl(self, issue_key: str, max_images: int = 2) -> str:
        """保留原始实现供未来通过直连 Jira API 恢复，当前未调用。"""
        import base64 as _b64
        import json as _json
        import os as _os
        try:
            mini_proxy_port = int(_os.environ.get("MINI_PROXY_PORT", "5001"))
            try:
                resp = requests.get(
                    f"http://127.0.0.1:{mini_proxy_port}/proxy/jira/issue/{issue_key}",
                    timeout=10,
                )
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                if data.get("status") != "success":
                    return ""
                raw_attachments = data.get("data", {}).get("fields", {}).get("attachment", [])
            except Exception:
                return ""

            # 2. 过滤图片
            image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
            images_meta = []
            for att in raw_attachments:
                filename = (att.get("filename") or "").lower()
                mime = att.get("mimeType") or ""
                if mime.startswith("image/") or filename.endswith(image_exts):
                    images_meta.append({
                        "id": att.get("id"),
                        "filename": att.get("filename"),
                        "mime": mime or "image/png",
                    })
                if len(images_meta) >= max_images:
                    break

            if not images_meta:
                return ""

            # 3. 下载并 base64 编码
            b64_images = []
            for img in images_meta:
                try:
                    dl = requests.get(
                        f"http://127.0.0.1:{mini_proxy_port}/proxy/jira/attachment/{img['id']}",
                        timeout=8,
                    )
                    if dl.status_code != 200:
                        continue
                    ct = dl.headers.get("content-type", "")
                    # 排除 HTML 认证页误伤
                    if "text/html" in ct:
                        continue
                    b64 = _b64.b64encode(dl.content).decode("ascii")
                    mime = img["mime"] if img["mime"].startswith("image/") else "image/png"
                    b64_images.append({
                        "filename": img["filename"],
                        "data_uri": f"data:{mime};base64,{b64}",
                    })
                except Exception:
                    continue

            if not b64_images:
                return ""

            # 4. 加载 LLM 配置（多 provider 支持）
            cfg_path = _os.path.join(_os.path.dirname(__file__), "llm_config.json")
            with open(cfg_path, encoding="utf-8") as f:
                raw_cfg = _json.load(f)

            # 5. 构建 user content blocks
            content_blocks = [{
                "type": "text",
                "text": (
                    "你是一个工单截图分析助手。请按以下格式简洁描述图片中与用户问题相关的关键信息：\n"
                    "1. 截图类型（报错弹窗 / 配置页面 / 数据表格 / 流程图 / 其他）\n"
                    "2. 关键文字或报错信息（逐字准确抄录报错码、字段名、数字）\n"
                    "3. 可见的异常点（如红框 / 空字段 / 错误状态）\n"
                    "4. 用户可能想表达的问题\n"
                    "不要编造截图中没有的内容。如果图片模糊或不相关，直接说明。\n\n"
                    f"工单号: {issue_key}，附件文件名: {', '.join(i['filename'] for i in b64_images)}"
                ),
            }]
            for img in b64_images:
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": img["data_uri"]},
                })

            # 6. 多 provider 跨级降级（2026-04-09 实测结果）：
            #   Tier 1: zhipu/GLM-4.6V   ← 主力（用户指定）
            #   Tier 2: zhipu/GLM-4.5V   ← 同 key 备选，实测能稳定返回描述
            #   Tier 3: minimax/MiniMax-M2.7 ← 跨 provider 最后兜底（接受多模态格式）
            # 不可用：GLM-5V-Turbo（套餐未开放）、glm-4v/glm-4v-plus（余额不足）、
            #        kimi-for-coding（endpoint 仅 coding 模型）、moonshot vision（需独立 API key）
            from openai import OpenAI as _OpenAI

            fallback_chain = [
                ("zhipu",   "GLM-4.6V"),
                ("zhipu",   "GLM-4.5V"),
                ("minimax", "MiniMax-M2.7"),
            ]

            description = ""
            for provider_name, model_name in fallback_chain:
                provider_cfg = raw_cfg.get(provider_name, {})
                if not provider_cfg.get("api_key"):
                    continue
                try:
                    client = _OpenAI(
                        api_key=provider_cfg["api_key"],
                        base_url=provider_cfg.get("base_url", ""),
                        timeout=15,  # 图片分析非核心路径，严格限时避免阻塞线程池
                    )
                    resp = client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "user", "content": content_blocks}],
                        max_tokens=600,
                        temperature=0.2,
                    )
                    raw = (resp.choices[0].message.content or "").strip()
                    # 去掉 minimax 可能混入的 <think>...</think> 块
                    import re as _re
                    raw = _re.sub(r'<think>[\s\S]*?</think>', '', raw, flags=_re.DOTALL).strip()
                    if raw:
                        description = raw
                        print(f"[VisionAnalysis] {issue_key}: {len(b64_images)}张图 ({provider_name}/{model_name}, {len(description)}字)")
                        break
                except Exception as model_err:
                    print(f"[VisionAnalysis] {provider_name}/{model_name} 失败，降级: {str(model_err)[:100]}")
                    continue
            return description
        except Exception as e:
            print(f"[VisionAnalysis] {issue_key} 分析失败: {e}")
            return ""

    def _load_style_rules(self, category: "str | None", user_id: str = "") -> str:
        """加载 _global.md + user_<id>.md + per-module override。
        优先级（后加载覆盖前）：global → user → module。"""
        from pathlib import Path as _Path
        rules_dir = _Path(__file__).resolve().parent / "data" / "reply_style_rules"
        global_file = rules_dir / "_global.md"
        legacy_file = _Path(__file__).resolve().parent / "data" / "reply_style_rules.md"
        if global_file.exists():
            rules = global_file.read_text(encoding="utf-8")
        elif legacy_file.exists():
            rules = legacy_file.read_text(encoding="utf-8")
        else:
            return self.reply_trainer.get_style_rules()
        if user_id:
            user_file = rules_dir / f"user_{user_id}.md"
            if user_file.exists():
                override = user_file.read_text(encoding="utf-8").strip()
                if override:
                    rules = rules + f"\n\n## 个人风格补充（{user_id}）\n{override}"
            else:
                # 新用户冷启动：叠加通用基础风格（去除产品域术语的提炼版）
                base_file = rules_dir / "_base_new_user.md"
                if base_file.exists():
                    base = base_file.read_text(encoding="utf-8").strip()
                    if base:
                        rules = rules + f"\n\n## 新用户基础风格\n{base}"
        if category:
            module_file = rules_dir / f"{category}.md"
            if module_file.exists():
                override = module_file.read_text(encoding="utf-8").strip()
                if override:
                    rules = rules + f"\n\n## 模块专属风格补充（{category}）\n{override}"
        return rules

    def _generate_styled_reply(self, ai_analysis: Dict, similar_issues: List[Dict],
                                reply_examples: List[Dict], kb_evidence: List[Dict] = None,
                                module_category: "str | None" = None,
                                user_id: str = "",
                                specificity_level: str = "medium") -> str:
        """使用 LLM 生成回复（KB证据+范例风格）。"""
        # 读取风格规则（全局 + per-user + per-module 覆盖）
        style_rules = self._load_style_rules(module_category, user_id=user_id)

        # 构建 system prompt
        # 核心 prompt（可进化 slot：evolution_core 通过 data/reply_prompt_core.md 进化）
        from pathlib import Path as _Path
        _prompt_core_path = _Path(__file__).resolve().parent / "data" / "reply_prompt_core.md"
        if _prompt_core_path.exists():
            system_parts = [_prompt_core_path.read_text(encoding="utf-8").rstrip("\n")]
        else:
            # 降级硬编码（文件缺失时保护服务可用性）
            system_parts = [
                "你是流程中心的技术支持顾问。请严格基于知识库资料和相似工单数据撰写回复。\n"
                "\n## 核心原则（必须遵守）\n"
                "1. 所有具体信息（服务名称、菜单路径、配置参数、操作步骤、API名称等）必须来自知识库资料或相似工单数据，禁止凭空编造\n"
                "2. 如果知识库和相似工单中没有提供某个具体信息，用「请确认具体的XXX」或「建议联系XX团队获取详细配置」替代，绝不虚构路径或参数\n"
                "3. 引用资料时必须标注来源（如「参见《xxx》」），确保可追溯\n"
                "4. 知识库资料优先级高于相似工单，相似工单优先级高于AI分析建议\n"
                "\n## 回复要求\n"
                "- 直接给出操作步骤或配置方案，不要空洞的建议\n"
                "- 如果资料中有具体路径和参数，直接引用；如果没有，明确告知需要进一步确认\n"
                "- 对于不确定的内容，使用「建议确认」「请核实」等措辞，不要伪装成确定信息\n"
                "\n## 严禁行为（违反将导致回复质量评分为0）\n"
                "- 严禁在知识库参考资料中有相关内容时仍回答「暂不支持」「目前不支持」「暂未检索到」\n"
                "- 严禁编造产品功能限制——如果不确定是否支持，必须先引用知识库原文再给出判断\n"
                "- 如果知识库资料和你的推测冲突，以知识库资料为准，引用其原文\n"
                "- 回复中必须至少引用 1 条知识库资料的具体内容（如路径、配置项、设计说明），不能全靠推测"
            ]
        if style_rules:
            # 从 1500 抬到 8000：原值会把训练器每期新增的教训（追加在文件末尾）截断掉，
            # 导致 100 题训练沉淀的规则在智能回复里从未生效。
            system_parts.append(f"\n## 风格规则\n{style_rules[:8000]}")

        # Fix 1: 注入训练器积累的教训到在线生成 (闭合训练回路)
        try:
            from pathlib import Path as _Path
            _trainer_state_path = _Path(__file__).resolve().parent.parent.parent / "conclusion" / "_local" / "training" / "trainer_state.json"
            if _trainer_state_path.exists():
                import json as _json
                _state = _json.loads(_trainer_state_path.read_text(encoding="utf-8"))
                _lessons = _state.get("b_cumulative_lessons", [])
                if _lessons:
                    _recent = _lessons[-30:]  # 取最近 30 条, 避免 prompt 过长
                    system_parts.append("\n## 历史训练教训 (必须遵守)")
                    system_parts.append(f"以下是从 {_state.get('total_questions', 0)}+ 道评估题中积累的改进教训, 务必在生成回复时应用:")
                    for _i, _lesson in enumerate(_recent, 1):
                        system_parts.append(f"{_i}. {_lesson}")
                    print(f"[GenerateReply] 注入 {len(_recent)} 条训练教训到 system prompt")
        except Exception as _e:
            print(f"[GenerateReply] 加载训练教训失败 (不影响生成): {_e}")

        # 产品知识摘要 — Top-N 向量检索（降级：全文截断）
        try:
            from product_facts_indexer import query_facts as _query_facts
            _query_text = _build_query(issue_key, ai_analysis,
                                       cache_fn=self._get_issue_from_cache)
            _top_facts = _query_facts(_query_text, top_k=15)
            if _top_facts:
                _facts_body = "\n".join(f"- {f}" for f in _top_facts)
                system_parts.append(f"\n## 产品知识参考（来自客服纠正，请优先遵守）\n{_facts_body}")
                print(f"[GenerateReply] 检索产品事实 {len(_top_facts)} 条（向量相关度排序）")
            else:
                raise RuntimeError("向量检索返回空，触发降级")
        except Exception as _e:
            # 降级：全文截断（原逻辑）
            try:
                _facts_path = _Path(__file__).resolve().parent / "data" / "product_facts.md"
                if _facts_path.exists():
                    _facts_text = _facts_path.read_text(encoding="utf-8").strip()
                    _facts_lines = [l for l in _facts_text.splitlines()
                                    if not l.startswith(">") and l.strip()]
                    _facts_body = "\n".join(_facts_lines[1:]).strip()
                    if _facts_body and len(_facts_body) > 50:
                        system_parts.append(f"\n## 产品知识参考（来自客服纠正，请优先遵守）\n{_facts_body[:3000]}")
                        print(f"[GenerateReply] 注入产品事实(降级截断) {len([l for l in _facts_lines if l.startswith('- ')])} 条")
            except Exception as _e2:
                print(f"[GenerateReply] 加载产品知识失败 (不影响生成): {_e2}")

        # 云类型约束：禁止 LLM 仅凭产品知识库里的公有云案例就对私有化/未知部署模式的工单给出公有云限制
        system_parts.append(
            "\n## 部署模式约束（必须遵守）\n"
            "产品知识参考中包含公有云、私有化等不同部署场景的案例。"
            "判断适用性时必须严格依据本次工单的「部署模式」字段，不得凭产品知识库中其他工单的部署场景来推断当前工单的限制。\n"
            "- 若工单「部署模式」= 公有云：不支持客开扩展（JS/CSS/单据客开），引导使用标品能力\n"
            "- 若工单「部署模式」= 私有化或专属云：客开方案可行，可给出扩展建议\n"
            "- 若工单「部署模式」字段未提供：不得擅自假设为公有云或私有化，应给出通用方案并建议客户根据自身部署方式确认可行性"
        )

        # Gate 4 具体度指令注入
        _specificity_instructions = {
            "high": (
                "## 输出具体度指令\n"
                "知识库中有明确步骤，请给出编号操作步骤，每步独立可执行，"
                "直接引用知识库中的菜单路径、字段名称和参数名。"
            ),
            "medium": (
                "## 输出具体度指令\n"
                "知识库有相关思路但步骤不完整，请给出解决思路和关键节点，"
                "不要编造具体点击路径或字段名，对不确定的部分用「建议确认」代替。"
            ),
            "low": (
                "## 输出具体度指令\n"
                "知识库证据较弱，请引导用户补充信息，给出排查方向，"
                "并附上相关知识库文档链接供参考，不要给出具体操作步骤。"
            ),
        }
        _spec_instr = _specificity_instructions.get(specificity_level, _specificity_instructions["medium"])
        system_parts.append(f"\n{_spec_instr}")

        system_prompt = "\n".join(system_parts)

        # 构建 user prompt
        user_parts = []

        # KB知识库证据（主要内容来源）
        if kb_evidence:
            user_parts.append("## 知识库参考资料（请基于这些资料给出具体方案）")
            for i, item in enumerate((kb_evidence or [])[:4], 1):
                content = (item.get('raw_content') or item.get('chunk_text', ''))[:1500]
                name = item.get('name', '')
                summary = item.get('summary', '')
                user_parts.append(f"\n### 资料{i}: {name}")
                if summary:
                    user_parts.append(f"摘要：{summary}")
                user_parts.append(f"正文：{content}")

        # 范例（风格参考）— Fix 3: 区分修改版本 (金样本) 和直接采纳版本
        if reply_examples:
            _modified = [ex for ex in reply_examples if ex.get('is_modified') or not ex.get('adopted')]
            _adopted = [ex for ex in reply_examples if ex.get('adopted') and not ex.get('is_modified')]

            if _modified:
                user_parts.append("\n## 用户修改过的回复范例 (请特别学习这些修改, 它们代表用户期望的回复标准)")
                for i, ex in enumerate(_modified[:2], 1):
                    user_parts.append(f"\n### 修改范例 {i}: {ex.get('summary', '')}")
                    user_parts.append(ex.get('reply', '')[:400])

            if _adopted:
                user_parts.append("\n## 直接采纳的回复范例 (风格参考)")
                for i, ex in enumerate(_adopted[:2], 1):
                    user_parts.append(f"\n### 范例 {i}: {ex.get('summary', '')}")
                    user_parts.append(ex.get('reply', '')[:400])

        # 当前工单的 AI 分析
        user_parts.append("\n## 当前工单需要回复")
        user_parts.append(f"标题: {ai_analysis.get('issue_title', '')}")

        # 部署模式 + 产品版本（云类型约束）
        _deploy_mode = ai_analysis.get('deploy_mode', '')
        _product_version = ai_analysis.get('product_version', '')
        if _deploy_mode:
            user_parts.append(f"部署模式: {_deploy_mode}")
        if _product_version:
            user_parts.append(f"产品版本: {_product_version}")
        if '公有云' in _deploy_mode:
            user_parts.append("⚠️ 【公有云限制】此工单客户使用的是公有云产品。公有云不支持任何客开扩展（JS/CSS/单据客开均不可）。回复中不能给出客开方案，应引导使用标品能力或说明功能规划。")
        elif '私有化' in _deploy_mode:
            user_parts.append("ℹ️ 此工单为私有化部署，客开方案可行，可结合业务场景给出扩展建议。")
        elif '专属云' in _deploy_mode:
            user_parts.append("ℹ️ 此工单为专属云环境，客开支持情况请根据合同确认后再给出建议。")

        desc = ai_analysis.get('issue_description', '') or ''
        if desc:
            user_parts.append(f"描述: {desc[:500]}")

        # 附件图片分析（GLM-5V-Turbo 返回的视觉描述）
        image_analysis = ai_analysis.get('image_analysis', '')
        if image_analysis:
            user_parts.append(f"\n### 📷 附件截图分析（视觉模型提取）\n{image_analysis[:800]}")
            user_parts.append("⚠️ 请在回复中引用截图里的具体报错信息或字段，以体现你看过用户的附件。")

        user_parts.append(f"问题分析: {ai_analysis.get('problem_analysis', '')[:300]}")
        user_parts.append(f"解决建议: {ai_analysis.get('solution_suggestion', '')[:500]}")

        impact = ai_analysis.get('functionality_impact', '')
        if impact:
            user_parts.append(f"功能影响: {impact[:200]}")

        # 相似工单参考
        if similar_issues:
            user_parts.append("\n## 相似工单参考")
            for issue in similar_issues[:2]:
                key = issue.get('key', '')
                summary = issue.get('summary', '')
                sol = issue.get('solution_suggestion', '') or issue.get('ai_analysis', {}).get('solution_suggestion', '')
                if sol:
                    user_parts.append(f"- {key}: {summary}\n  方案: {sol[:150]}")

        user_parts.append("\n## 请生成回复")
        user_parts.append("要求：严格基于上述知识库资料和相似工单给出解决方案，模仿范例的风格和语气。直接输出回复内容，不要加标题或前缀。")
        user_parts.append("严禁编造：如果上述资料中没有提供具体的菜单路径、服务名称、配置参数或操作步骤，不要自行编造，应使用「请确认具体的XXX」或「建议联系研发团队获取」等措辞。")
        user_parts.append("长度要求：简单明确的问题60-150字；需要解释原理或给出具体方案步骤的200-500字；不支持/拒绝的回复要简要说明原因和替代建议，不要过于简短。")

        user_prompt = "\n".join(user_parts)

        # 调用 LLM（按 feature routing 降级链逐个尝试）
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        # smart_reply 是用户面功能：凭据优先用户级。先尝试 user-level 路由解析；
        # _blocked（用户级强制但未配）→ 抛 RuntimeError 由上层端点转结构化提示。
        chain = self._resolve_feature_chain("smart_reply")
        try:
            from main import resolve_feature_llm_runtime as _rfr
            _routed = _rfr("smart_reply", user_id=user_id or None)
        except Exception as _e_routed:
            _routed = None
        if _routed is not None and _routed.get("_blocked") and user_id:
            raise RuntimeError("LLM_BLOCKED:smart_reply:feature_requires_user_llm")
        # user-level 命中：把该 provider 置于链首，凭据用用户级
        _user_routed_cfg = None
        if _routed is not None and _routed.get("_source") == "user" and _routed.get("api_key"):
            _user_routed_cfg = _routed
            _rp = _routed.get("provider")
            if _rp and _rp in chain:
                chain = [_rp] + [c for c in chain if c != _rp]
            elif _rp:
                chain = [_rp] + chain
        last_err = ""
        for provider_name in chain:
            # 用户级命中的 provider 用用户凭据，其余仍读系统级 provider config
            if _user_routed_cfg is not None and provider_name == _user_routed_cfg.get("provider"):
                p_cfg = {
                    "api_key": _user_routed_cfg.get("api_key", ""),
                    "model_name": _user_routed_cfg.get("model_name", "") or self._get_default_model(provider_name),
                    "base_url": _user_routed_cfg.get("base_url", ""),
                }
            else:
                p_cfg = self._load_provider_config(provider_name)
            if not p_cfg.get("api_key"):
                continue
            try:
                result = self.llm_service.call_llm(
                    prompt=full_prompt,
                    api_key=p_cfg["api_key"],
                    provider=provider_name,
                    model_name=p_cfg["model_name"],
                    base_url=p_cfg["base_url"],
                    temperature=0.3,
                )
                _err_prefixes = ("Error:", "模型调用失败:", "LLM 分析失败:")
                if result and not any(result.startswith(p) for p in _err_prefixes):
                    for err_prefix in _err_prefixes:
                        err_idx = result.find(err_prefix)
                        if err_idx > 0:
                            result = result[:err_idx].rstrip()
                    if len(result) > 1500:
                        result = result[:1500].rsplit('\n', 1)[0]
                    if result and len(result) > 5:
                        print(f"[GenerateReply] LLM 个性化回复生成成功 provider={provider_name} ({len(result)} 字)")
                        return result
                last_err = result or "(空响应)"
            except Exception as e:
                last_err = str(e)
                print(f"[GenerateReply] {provider_name} 失败，降级: {last_err[:120]}")
                continue

        if last_err:
            print(f"[GenerateReply] 所有 LLM provider 均失败，回退到模板。最后错误: {last_err[:200]}")

        # 回退到模板
        return self._build_detailed_solution(ai_analysis, similar_issues)

    def _compute_specificity_level(self, kb_evidence: list) -> str:
        """
        基于 KB 证据质量计算输出具体度等级。
        返回 "high" | "medium" | "low" | "none"
        """
        if not kb_evidence:
            return "none"

        # 对每条证据打分，取最高分
        best_score = 0.0
        for item in kb_evidence:
            step_density = float(item.get("step_density", 0.0))
            source_tier = float(item.get("source_tier", 1))
            completeness = float(item.get("completeness", 1.0))
            # 加权综合分：步骤密度 0.5 + 来源层级归一化 0.3 + 完整性 0.2
            score = (step_density * 0.5 + (source_tier / 3.0) * 0.3 + completeness * 0.2)
            if score > best_score:
                best_score = score

        if best_score >= 0.60:
            return "high"
        elif best_score >= 0.35:
            return "medium"
        elif best_score >= 0.10:
            return "low"
        else:
            return "none"

    def _run_gate1_completeness(self, issue_key: str) -> Optional[Dict]:
        """
        运行 Gate 1 信息完整性检查。
        返回 None 表示通过（可继续生成回复）；
        返回 dict 表示拦截（直接作为 generate_reply_content 返回值）。
        """
        try:
            import yaml
            from pathlib import Path as _Path
            cfg_path = _Path(PROJECT_ROOT) / "config" / "reply_gates.yaml"
            with open(cfg_path, encoding="utf-8") as _fh:
                _cfg = yaml.safe_load(_fh)
            if not _cfg.get("gates", {}).get("completeness", {}).get("enabled", False):
                return None
        except Exception:
            return None  # 配置读取失败 → 放行

        # 从缓存拿工单信息
        issue_info = self._get_issue_from_cache(issue_key)
        project = ""
        description = ""
        issue_type_confirmed = ""
        attachment_texts: list = []

        if issue_info:
            project = (
                issue_info.get("project_name")
                or issue_info.get("project", "")
            )
            description = issue_info.get("description", "")
            issue_type_confirmed = issue_info.get("dev_issue_type", "")

        try:
            from services import completeness_checker
            result = completeness_checker.check(
                issue_key=issue_key,
                project=project,
                issue_type_confirmed=issue_type_confirmed,
                description=description,
                attachment_texts=attachment_texts,
            )
        except Exception as e:
            print(f"[Gate1] completeness check error, skipping: {e}")
            return None  # 检查失败 → 放行

        if result.passed:
            return None

        # 拦截 — 自动退回支持（填解决方案 + 退回支持 transition）
        _g1_ok, _g1_steps = self._gate1_auto_return_to_support(
            issue_key, result.missing_fields, result.inquiry_draft,
            insufficient_type=result.insufficient_type,
        )
        try:
            from services import gate_decision_log as _gdl1
            _gdl1.log_gate_decision(
                issue_key=issue_key,
                gate_decisions={"completeness": "blocked", "classification": "skipped",
                                 "reuse": "skipped", "specificity": "skipped", "supervisor": "skipped"},
                missing_fields=result.missing_fields,
                final_action="auto_returned",
                operation_steps=_g1_steps,
            )
        except Exception as _e_g1l:
            print(f"[GateLog] Gate1 埋点失败: {_e_g1l}")

        return {
            "reply_content": "",
            "solution_content": "",
            "ai_analysis": None,
            "word_count": 0,
            "gate": "completeness",
            "missing_fields": result.missing_fields,
            "inquiry_draft": result.inquiry_draft,
            "rule_matched": result.rule_matched,
            "auto_returned": _g1_ok,
            "operation_steps": _g1_steps,
            "insufficient_type": result.insufficient_type,
            "gate_decisions": {"completeness": {
                "passed": False,
                "missing_fields": result.missing_fields,
                "rule_matched": result.rule_matched,
            }},
        }

    def _gate1_auto_return_to_support(self, issue_key: str, missing_fields: list, inquiry_draft: str, insufficient_type: str = "") -> tuple:
        """
        Gate 1 自动退回：填解决方案 + 选「退回支持」+ 触发 Jira transition。
        上限 2 次/ticket（防误判反复退回）。计数记录在 data/gate1_inquiry_log.json。
        返回 (success: bool, operation_steps: list)。
        """
        import json as _json
        from pathlib import Path as _Path
        log_path = _Path(PROJECT_ROOT) / "data" / "gate1_inquiry_log.json"
        try:
            log: dict = _json.loads(log_path.read_text()) if log_path.exists() else {}
        except Exception:
            log = {}

        entry = log.get(issue_key, {"count": 0})
        if entry.get("count", 0) >= 2:
            print(f"[Gate1] {issue_key}: auto_return_count >= 2, skipping")
            return False, []

        operation_steps = [
            f"Gate 1 检测缺失字段：{', '.join(missing_fields)}",
            f"自动填写「解决方案」：{inquiry_draft[:60]}{'...' if len(inquiry_draft) > 60 else ''}",
            "自动选择「回复方式」= 退回支持 (customfield_10410 id=15702)",
            "触发 Jira transition「直接回复」，工单状态流转",
        ]
        try:
            from datetime import datetime as _dt
            _custom_fields = {
                "solution": inquiry_draft,
                "reply_method": "退回支持",
            }
            if insufficient_type == "invalid_description":
                _custom_fields["issue_type_confirmed"] = "无效问题"
            result = jira_service.reply_and_close_via_transition(
                issue_id=issue_key,
                comment=inquiry_draft,
                custom_fields=_custom_fields,
                ai_fields=None,
            )
            ok = result.get("success", False)
            if ok:
                entry["count"] = entry.get("count", 0) + 1
                entry["last_sent"] = _dt.now().isoformat()
                log[issue_key] = entry
                log_path.write_text(_json.dumps(log, ensure_ascii=False, indent=2))
                print(f"[Gate1] {issue_key}: auto_return sent (count={entry['count']})")
            else:
                print(f"[Gate1] {issue_key}: reply_and_close failed: {result.get('message')}")
            return ok, operation_steps
        except Exception as e:
            print(f"[Gate1] {issue_key}: auto_return error: {e}")
            return False, operation_steps

    def _run_gate2_classification(self, issue_key: str) -> Optional[Dict]:
        """
        运行 Gate 2 分类正确性检查。
        返回 None 表示通过（继续生成回复）；
        返回 dict 表示需要迁移或有迁移建议（直接作为 generate_reply_content 返回值）。
        """
        try:
            from services.classifier_service import classify_issue
            issue_info = self._get_issue_from_cache(issue_key) or {}
            current_project = issue_info.get("project_name") or issue_info.get("project", "")
            issue_type_confirmed = issue_info.get("dev_issue_type", "")
            issue_type = issue_info.get("issue_type", "")
            title = issue_info.get("summary", "")
            description = issue_info.get("description", "")

            result = classify_issue(
                issue_key=issue_key,
                title=title,
                description=description,
                current_project=current_project,
                current_issue_type=issue_type,
                issue_type_confirmed=issue_type_confirmed,
            )
        except Exception as e:
            print(f"[Gate2] classification error, skipping: {e}")
            return None

        if not result.gate_enabled:
            return None

        transfer_to = {
            "project": result.predicted_project,
            "confidence": result.confidence,
            "reason": result.reasoning,
        }

        if result.confidence >= 0.92 and result.predicted_project and result.predicted_project != current_project:
            # 高置信度误分类 → 尝试 auto-move
            auto_moved = False
            try:
                target_board = self._gate2_find_target_board(result.predicted_project, confidence=result.confidence)
                if target_board:
                    move_result = self.move_issue_to_board(issue_key, target_board, sync_jira=True)
                    auto_moved = move_result.get("success", False)
                    if auto_moved:
                        self._gate2_record_move(
                            issue_key, current_project, result.predicted_project,
                            result.confidence, result.reasoning, target_board
                        )
                        print(f"[Gate2] {issue_key}: auto-moved {current_project} → {result.predicted_project} (board={target_board})")
                        self._gate2_notify_move(issue_key, current_project, result.predicted_project, result.confidence)
                    else:
                        print(f"[Gate2] {issue_key}: move_issue_to_board failed: {move_result.get('error')}")
            except Exception as e:
                print(f"[Gate2] auto-move error: {e}")

            return {
                "reply_content": "",
                "solution_content": "",
                "ai_analysis": None,
                "word_count": 0,
                "gate": "classification",
                "transfer_to": transfer_to,
                "auto_moved": auto_moved,
                "gate_decisions": {"classification": {
                    "passed": False,
                    "confidence": result.confidence,
                    "predicted_project": result.predicted_project,
                    "auto_moved": auto_moved,
                }},
                "suggested_reply_method": None,
                "suggested_issue_type": None,
                "generation_method": "gate_blocked",
                "kb_sources": [],
                "kb_evidence_count": 0,
                "examples_used_count": 0,
                "style_rules_applied": False,
            }

        if 0.70 <= result.confidence < 0.92 and result.predicted_project != current_project:
            # 中置信度 → 添加可疑标签建议，继续生成
            print(f"[Gate2] {issue_key}: suspect misclass {current_project} → {result.predicted_project} ({result.confidence:.2f})")
            # 不中断回复生成，只在返回值里附加 transfer_to
            # 通过设置 _gate2_transfer_to_pending 让最终 return dict 拿到这个信息
            self._gate2_transfer_pending = transfer_to
            return None

        # < 0.7 或预测项目相同 → 通过
        return None

    def _gate2_find_target_board(self, predicted_project: str, summary: str = "", description: str = "", confidence: float = 0.0) -> Optional[str]:
        """从 gate2_routing.json（优先）或 board_config.json（回退）找到目标看板 key。"""
        # NEW: try gate2_routing.json first
        try:
            routing_path = Path(__file__).parent / "data" / "gate2_routing.json"
            routing = json.loads(routing_path.read_text(encoding="utf-8"))
            for rule in routing.get("rules", []):
                if rule.get("predicted_project") == predicted_project and rule.get("auto_move_enabled", False):
                    if confidence < rule.get("min_confidence", 0.92):
                        continue
                    text = f"{summary} {description}".lower()
                    keywords = rule.get("sub_module_keywords", [])
                    if not keywords or any(kw.lower() in text for kw in keywords):
                        return rule.get("target_board_id") or None
        except Exception as e:
            print(f"[Gate2] gate2_routing.json lookup error: {e}")
        # EXISTING: fall through to board_config.json substring matching
        try:
            cfg_path = Path(__file__).parent / "data" / "board_config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for col in cfg.get("columns", []):
                title = col.get("title", "")
                if predicted_project in title or title in predicted_project:
                    return col.get("key")
        except Exception as e:
            print(f"[Gate2] find_target_board error: {e}")
        return None

    def _gate2_record_move(
        self, issue_key: str, original_project: str, predicted_project: str,
        confidence: float, reason: str, target_board: str
    ) -> None:
        """记录 auto-move 到 data/auto_move_log.json，用于追溯和回滚。"""
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path as _P
        log_path = _P(__file__).parent / "data" / "auto_move_log.json"
        try:
            log: dict = _json.loads(log_path.read_text()) if log_path.exists() else {}
        except Exception:
            log = {}
        log[issue_key] = {
            "issue_key": issue_key,
            "original_project": original_project,
            "predicted_project": predicted_project,
            "target_board": target_board,
            "confidence": confidence,
            "reason": reason,
            "moved_at": _dt.now().isoformat(),
            "rolled_back": False,
        }
        log_path.write_text(_json.dumps(log, ensure_ascii=False, indent=2))

    def _gate2_notify_move(self, issue_key: str, from_proj: str, to_proj: str, confidence: float) -> None:
        try:
            from services.feishu_notifier import get_notifier
            get_notifier().send_message(
                f"[Gate2 Auto-Move] {issue_key}: {from_proj} → {to_proj} (置信度={confidence:.0%})\n"
                "如需撤回，请调用 POST /api/board/rollback-auto-move/{issue_key}"
            )
        except Exception:
            pass

    def _get_issue_from_cache(self, issue_key: str) -> Optional[Dict]:
        """从看板缓存获取工单信息"""
        try:
            from jira_service import JiraService
            cache = jira_service.load_board_cache()
            for issue in (cache or []):
                key = issue.key if hasattr(issue, 'key') else issue.get('key', '')
                if key == issue_key:
                    if hasattr(issue, '__dict__'):
                        return {k: v for k, v in vars(issue).items() if v}
                    return issue
        except Exception as e:
            print(f"[GenerateReply] 缓存查找失败: {e}")
        return None

    # KB l1_module 关键词映射（用于 _resolve_module_category 推断）
    _CATEGORY_KEYWORDS: dict = {
        "流程中心": ["工作流", "流程", "审批流", "流程配置", "流程节点", "流程发起", "流程设计", "审批节点"],
        "业务流":   ["业务流", "业务单据", "单据", "表单", "BillHead", "BillBody", "业务实体"],
        "开发框架": ["开发框架", "扩展开发", "客开", "二次开发", "插件", "SDK", "API"],
        "元数据":   ["元数据", "实体配置", "字段配置", "单据配置", "字段扩展", "属性配置"],
        "规则":     ["编码规则", "规则引擎", "规则配置", "自动编号", "编码格式"],
        "公式":     ["公式", "计算公式", "公式配置", "取值公式", "公式报错"],
        "打印":     ["打印", "打印模板", "报表打印", "PDF", "打印配置"],
        "权限":     ["权限", "角色权限", "数据权限", "功能权限", "授权", "访问控制"],
        "组织":     ["组织架构", "部门", "公司层级", "HR组织", "组织配置"],
        "配置迁移": ["配置迁移", "环境迁移", "迁移配置", "迁移工具"],
        "档案和应用": ["档案", "工作台", "基础档案", "核心档案", "业务档案"],
        "消息":     ["消息通知", "消息推送", "通知配置", "提醒设置"],
        "导入导出": ["导入导出", "数据导入", "批量导入", "Excel导入", "数据导出"],
        "应用支撑": ["调度", "任务调度", "定时任务", "国际化", "多语言", "删除引用"],
        "MDD开发框架": ["MDD", "MDD开发", "MDD框架", "MDD配置"],
        "UI模板":   ["UI模板", "界面模板", "页面模板", "前端模板"],
    }

    @staticmethod
    def _resolve_module_category(ai_analysis: dict,
                                  issue_key: str = "",
                                  cache_fn=None) -> "str | None":
        """从工单标题/描述关键词推断 KB l1_module，用于 search_bundle category 过滤。返回 None 时退回全局检索。"""
        from services.query_builder import build_issue_query as _bq
        text = _bq(issue_key, ai_analysis,
                   fields=("issue_title", "issue_description", "problem_analysis"),
                   max_len=600,
                   cache_fn=cache_fn)
        if not text:
            return None
        best_cat, best_count = None, 0
        for cat, keywords in BoardService._CATEGORY_KEYWORDS.items():
            count = sum(1 for kw in keywords if kw in text)
            if count > best_count:
                best_count, best_cat = count, cat
        return best_cat if best_count >= 1 else None

    # 回复方式 ID→值 映射（来源: Jira transitions API 2026-03-28 验证）
    REPLY_METHOD_MAP = {
        '15953': '紧急补丁/发布', '10916': '方案解决', '10917': '指导解决',
        '10918': '后续上线解决', '10919': '无效问题', '15316': '纳入需求库',
        '15317': '无法复现', '15599': '提供第三方代码工具包', '15702': '退回支持',
        '27916': '合集补丁发布',
    }
    # 回复方式 值→ID 反查
    REPLY_METHOD_RMAP = {v: k for k, v in REPLY_METHOD_MAP.items()}

    # 问题类型 ID→值 映射
    ISSUE_TYPE_MAP = {
        '12041': '产品错误', '12042': '需求问题', '12043': '应用操作',
        '15318': '客开问题', '15319': '效率问题', '15320': '实施问题',
        '15321': '无效问题', '17693': '设计问题', '17720': 'API问题',
        '17768': '安全问题', '20008': 'UE问题', '25002': '升级问题',
        '28685': '运维问题', '28686': '数据错误',
    }
    ISSUE_TYPE_RMAP = {v: k for k, v in ISSUE_TYPE_MAP.items()}

    def _suggest_reply_fields(self, ai_analysis: Dict,
                              reply_examples: List[Dict] = None) -> Dict:
        """
        智能推荐回复方式和问题类型。
        优先级: 范例历史数据投票 > AI分析关键词匹配 > 默认值
        """
        reply_method = None
        issue_type = None

        # 策略1: 从检索到的范例中统计最常见的回复方式和问题类型
        if reply_examples:
            method_votes = {}
            type_votes = {}
            for ex in reply_examples:
                # KB 存储时 l1_module=reply_method, l2_module=issue_type
                rm = ex.get('reply_method', '') or ''
                it = ex.get('issue_type', '') or ''
                if rm:
                    method_votes[rm] = method_votes.get(rm, 0) + ex.get('score', 0.5)
                if it:
                    type_votes[it] = type_votes.get(it, 0) + ex.get('score', 0.5)

            if method_votes:
                top_method = max(method_votes, key=method_votes.get)
                mid = self.REPLY_METHOD_RMAP.get(top_method, '')
                if mid:
                    reply_method = {'id': mid, 'value': top_method}
                    print(f"[SuggestFields] 范例投票回复方式: {top_method} (票数={method_votes[top_method]:.2f})")

            if type_votes:
                top_type = max(type_votes, key=type_votes.get)
                tid = self.ISSUE_TYPE_RMAP.get(top_type, '')
                if tid:
                    issue_type = {'id': tid, 'value': top_type}
                    print(f"[SuggestFields] 范例投票问题类型: {top_type} (票数={type_votes[top_type]:.2f})")

        # 策略2: AI分析关键词匹配（回退）
        if not reply_method or not issue_type:
            problem = (ai_analysis.get('problem_analysis', '') or '').lower()
            solution = (ai_analysis.get('solution_suggestion', '') or '').lower()
            title = (ai_analysis.get('issue_title', '') or '').lower()
            combined = problem + ' ' + solution + ' ' + title

            if not reply_method:
                if any(k in combined for k in ['bug', '缺陷', '修复', '补丁', 'patch', '产品错误']):
                    reply_method = {'id': '10916', 'value': '方案解决'}
                elif any(k in combined for k in ['操作', '使用方法', '指导', '配置', '设置']):
                    reply_method = {'id': '10917', 'value': '指导解决'}
                elif any(k in combined for k in ['需求', '功能增强', '新功能', '客开', '定制']):
                    reply_method = {'id': '15316', 'value': '纳入需求库'}
                elif any(k in combined for k in ['无法复现', '无法重现']):
                    reply_method = {'id': '15317', 'value': '无法复现'}
                elif any(k in combined for k in ['后续版本', '计划修复', '排期']):
                    reply_method = {'id': '10918', 'value': '后续上线解决'}
                elif any(k in combined for k in ['无效', '非问题', '误报']):
                    reply_method = {'id': '10919', 'value': '无效问题'}
                else:
                    reply_method = {'id': '10917', 'value': '指导解决'}

            if not issue_type:
                if any(k in combined for k in ['bug', '缺陷', '产品错误', '代码问题']):
                    issue_type = {'id': '12041', 'value': '产品错误'}
                elif any(k in combined for k in ['需求', '功能缺失', '新功能']):
                    issue_type = {'id': '12042', 'value': '需求问题'}
                elif any(k in combined for k in ['客开', '二开', '定制开发']):
                    issue_type = {'id': '15318', 'value': '客开问题'}
                elif any(k in combined for k in ['性能', '慢', '效率', '超时']):
                    issue_type = {'id': '15319', 'value': '效率问题'}
                elif any(k in combined for k in ['api', '接口']):
                    issue_type = {'id': '17720', 'value': 'API问题'}
                elif any(k in combined for k in ['数据', 'sql', '数据异常']):
                    issue_type = {'id': '28686', 'value': '数据错误'}
                else:
                    issue_type = {'id': '12043', 'value': '应用操作'}

        print(f"[SuggestFields] 推荐: 回复方式={reply_method['value']}, 问题类型={issue_type['value']}")
        return {'reply_method': reply_method, 'issue_type': issue_type}

    def _build_solution_content(self, ai_analysis: Dict) -> str:
        """构建标准解决方案内容"""
        parts = []

        # 主要解决方案
        solution_suggestion = ai_analysis.get('solution_suggestion', '')
        if solution_suggestion:
            solution_text = solution_suggestion.replace('**', '').replace('*', '').replace('#', '').strip()
            parts.append(solution_text)

        # 功能影响
        functionality_impact = ai_analysis.get('functionality_impact', '')
        if functionality_impact:
            impact_text = functionality_impact.replace('**', '').replace('*', '').replace('#', '').strip()
            if impact_text:
                parts.append(f"\n【功能影响】\n{impact_text}")

        return '\n'.join(parts).strip()

    def _build_detailed_solution(self, ai_analysis: Dict, similar_issues: List[Dict]) -> str:
        """构建详细解决方案（基于Chroma相似工单）"""
        parts = []

        # 1. 问题分析
        problem_analysis = ai_analysis.get('problem_analysis', '')
        if problem_analysis:
            parts.append("【问题分析】")
            parts.append(problem_analysis.replace('**', '').replace('*', '').replace('#', '').strip())
            parts.append("")

        # 2. 解决方案（核心内容）
        solution_suggestion = ai_analysis.get('solution_suggestion', '')
        if solution_suggestion:
            parts.append("【解决方案】")
            solution_text = solution_suggestion.replace('**', '').replace('*', '').replace('#', '').strip()
            parts.append(solution_text)
            parts.append("")

        # 3. 功能影响
        functionality_impact = ai_analysis.get('functionality_impact', '')
        if functionality_impact:
            parts.append("【功能影响】")
            impact_text = functionality_impact.replace('**', '').replace('*', '').replace('#', '').strip()
            parts.append(impact_text)
            parts.append("")

        # 4. 推荐团队/角色
        recommended_team = ai_analysis.get('recommended_team', '')
        recommended_role = ai_analysis.get('recommended_role', '')
        if recommended_team or recommended_role:
            team_str = "【推荐处理】"
            if recommended_team:
                team_str += f" {recommended_team}"
            if recommended_role:
                team_str += f" {recommended_role}"
            parts.append(team_str)
            parts.append("")

        # 5. 相似工单参考（基于Chroma搜索结果）
        if similar_issues:
            parts.append("【参考案例】")
            # 取前3个最相似的
            for i, issue in enumerate(similar_issues[:3], 1):
                key = issue.get('key', '')
                summary = issue.get('summary', '')
                solution = issue.get('solution_suggestion', '') or issue.get('ai_analysis', {}).get('solution_suggestion', '')

                if solution:
                    # 截取解决方案的前100字
                    solution_preview = solution[:100] + "..." if len(solution) > 100 else solution
                    solution_preview = solution_preview.replace('**', '').replace('*', '').replace('#', '').strip()
                    parts.append(f"{i}. {key}: {summary}")
                    parts.append(f"   解决方案: {solution_preview}")
            parts.append("")

        # 合并并截断
        content = '\n'.join(parts).strip()

        # 限制长度（最多800字，保留完整句子）
        word_count = len(content.replace('\n', '').replace(' ', ''))
        if word_count > 800:
            sentences = content.split('。')
            truncated = []
            current_count = 0
            for sentence in sentences:
                sentence_count = len(sentence.replace(' ', ''))
                if current_count + sentence_count > 750:
                    break
                truncated.append(sentence)
                current_count += sentence_count
            content = '。'.join(truncated) + '。\n\n（内容过长已截断，完整分析请查看AI分析面板）'

        return content

    # ── 自动化任务规则 ──────────────────────────────────────────────────

    def get_automation_rules(self) -> List[Dict]:
        config = self._load_board_config()
        return config.get("automation_rules", [])

    def add_automation_rule(self, rule_data: Dict) -> Dict:
        config = self._load_board_config()
        rules = config.setdefault("automation_rules", [])
        rule = {
            "id": f"rule_{int(time.time() * 1000)}",
            "name": rule_data.get("name", "未命名规则"),
            "enabled": True,
            "conditions": rule_data.get("conditions", {}),
            "action": rule_data.get("action", {}),
            "stats": {
                "total_processed": 0,
                "last_run_at": None,
                "last_run_status": None,
                "last_run_matched": 0,
                "last_run_executed": 0,
                "last_run_errors": 0,
            },
            "created_at": datetime.now().isoformat(),
        }
        rules.append(rule)
        self.save_board_config(config)
        return rule

    def update_automation_rule(self, rule_id: str, updates: Dict) -> Optional[Dict]:
        config = self._load_board_config()
        rules = config.get("automation_rules", [])
        for r in rules:
            if r["id"] == rule_id:
                for key in ("name", "conditions", "action"):
                    if key in updates:
                        r[key] = updates[key]
                self.save_board_config(config)
                return r
        return None

    def delete_automation_rule(self, rule_id: str) -> bool:
        config = self._load_board_config()
        rules = config.get("automation_rules", [])
        before = len(rules)
        config["automation_rules"] = [r for r in rules if r["id"] != rule_id]
        if len(config["automation_rules"]) < before:
            self.save_board_config(config)
            return True
        return False

    def toggle_automation_rule(self, rule_id: str) -> Optional[Dict]:
        config = self._load_board_config()
        for r in config.get("automation_rules", []):
            if r["id"] == rule_id:
                r["enabled"] = not r["enabled"]
                self.save_board_config(config)
                return {"enabled": r["enabled"]}
        return None

    @staticmethod
    def _build_keywords_jql(expr: str) -> str:
        """
        将关键词表达式转为 JQL text 查询片段。
        语法：\ = OR（备选），+ = AND（同时满足），() = 分组
        示例:
          "(监控\\干预\\调整)+流程" → (text ~ "监控" OR text ~ "干预" OR text ~ "调整") AND text ~ "流程"
          "审批\\权限"             → text ~ "审批" OR text ~ "权限"
          "流程+审批"             → text ~ "流程" AND text ~ "审批"
        """
        import re
        expr = expr.replace("\\", " OR ").replace("+", " AND ")
        tokens = re.findall(r'\bAND\b|\bOR\b|[()]|[^\s()]+', expr, flags=re.IGNORECASE)
        parts = []
        for tok in tokens:
            upper = tok.upper()
            if upper in ('AND', 'OR'):
                parts.append(upper)
            elif tok in ('(', ')'):
                parts.append(tok)
            else:
                parts.append(f'text ~ "{tok}"')
        return ' '.join(parts)

    def run_automation_rule(self, rule_id: str, dry_run: bool = False) -> Dict:
        """执行自动化规则：JQL 查询匹配工单 → 去重 → 执行动作"""
        config = self._load_board_config()
        rule = None
        for r in config.get("automation_rules", []):
            if r["id"] == rule_id:
                rule = r
                break
        if not rule:
            return {"success": False, "error": "规则不存在"}

        # 1. 用 JQL 查询匹配工单
        conds = rule.get("conditions", {})
        jql_parts = ['issuetype = "支持问题"', "resolution = Unresolved"]
        if conds.get("project"):
            jql_parts.append(f'project = {conds["project"]}')
        if conds.get("customer_issue_type"):
            jql_parts.append(f'cf[10402] = "{conds["customer_issue_type"]}"')
        if conds.get("assignee"):
            jql_parts.append(f'assignee = "{conds["assignee"]}"')
        if conds.get("keywords"):
            kw_jql = self._build_keywords_jql(conds["keywords"])
            if kw_jql:
                jql_parts.append(f"({kw_jql})")
        if conds.get("due_within"):
            from datetime import timedelta
            _delta = {"4h": 0, "1d": 1, "3d": 3, "5d": 5}
            days = _delta.get(conds["due_within"], 0)
            # cf[11919] 是 datetime 类型，JQL 必须带时间部分，否则比较失效
            cutoff = (datetime.now().date() + timedelta(days=days)).strftime("%Y-%m-%d") + " 23:59"
            # 本 Jira 实例到期日存储于 customfield_11919（非标准 duedate 字段）
            jql_parts.append("cf[11919] is not EMPTY")
            jql_parts.append(f'cf[11919] <= "{cutoff}"')
        jql = " AND ".join(jql_parts) + " ORDER BY created DESC"

        try:
            from services.user_session_pool import pick_jira_service_for_bg
            jira = pick_jira_service_for_bg("automation_rule")
            if jira is None:
                return {"success": False, "error": "strict 模式：无活跃用户会话，跳过自动化规则执行"}
            result_data = jira.search_issues(jql, max_results=200)
            if "error" in result_data:
                err_msg = str(result_data['error'])
                # 会话失效检测
                if '401' in err_msg or 'Unauthorized' in err_msg:
                    self._update_rule_stats(config, rule, 0, 0, 0, "session_expired")
                    return {"success": False, "error": "Jira 会话已过期，请先在看板页面刷新会话"}
                return {"success": False, "error": f"Jira 查询失败: {err_msg}"}
            raw_issues = result_data.get("issues", [])
        except Exception as e:
            err_msg = str(e)
            if '401' in err_msg or 'Unauthorized' in err_msg:
                self._update_rule_stats(config, rule, 0, 0, 0, "session_expired")
                return {"success": False, "error": "Jira 会话已过期，请先在看板页面刷新会话"}
            return {"success": False, "error": f"Jira 查询失败: {e}"}

        # 2. 去重：排除已处理过的工单
        processed_keys = set(rule.get("processed_keys", []))
        action = rule.get("action", {})
        action_type = action.get("type", "")

        matched = []
        skipped_dedup = 0
        for issue in raw_issues:
            fields = issue.get("fields", {})
            key = issue.get("key", "")
            summary = fields.get("summary", "")
            assignee_obj = fields.get("assignee") or {}
            assignee = assignee_obj.get("name", "") if isinstance(assignee_obj, dict) else str(assignee_obj)

            # assign 去重：目标经办人已经是当前经办人则跳过
            if action_type == "assign" and assignee == action.get("assign_to", ""):
                skipped_dedup += 1
                continue

            # 通用去重：已处理过的 key（非 dry_run 模式才检查）
            if not dry_run and key in processed_keys:
                skipped_dedup += 1
                continue

            if action_type == "assign":
                would_do = f"分配 → {action.get('assign_to', '?')}"
            elif action_type == "move_project":
                would_do = f"移动 → {action.get('target_project', '?')}/{action.get('target_module', '?')}"
            elif action_type == "auto_reply":
                preview = (action.get("comment_text") or "")[:20]
                would_do = f"回复并关闭: {preview}..." if len(action.get('comment_text','')) > 20 else f"回复并关闭: {preview}"
            else:
                would_do = "未知动作"

            matched.append({
                "key": key,
                "summary": summary[:60],
                "current_assignee": assignee,
                "would_do": would_do,
            })

        result = {
            "success": True,
            "dry_run": dry_run,
            "matched": len(matched),
            "skipped_dedup": skipped_dedup,
            "executed": 0,
            "errors": 0,
            "issues": matched,
        }

        if dry_run or not matched:
            return result

        # 3. 正式执行
        executed = 0
        errors = 0
        from services.user_session_pool import pick_jira_service_for_bg
        jira = pick_jira_service_for_bg("automation_rule_execute")
        if jira is None:
            result["success"] = False
            result["error"] = "strict 模式：无活跃用户会话，跳过执行步骤"
            return result

        _comment_text = action.get("comment_text", "").strip()
        for item in matched:
            try:
                if action_type == "assign":
                    auto_comment = _comment_text or f"[自动化规则] {rule['name']}"
                    resp = jira.assign_issue(item["key"], action["assign_to"],
                                             comment=auto_comment)
                    if resp.get("success"):
                        item["result"] = "成功"
                        executed += 1
                        processed_keys.add(item["key"])
                    else:
                        msg = resp.get('message', '')
                        if '401' in msg or 'Unauthorized' in msg:
                            item["result"] = "会话过期"
                            errors += 1
                            break
                        item["result"] = f"失败: {msg}"
                        errors += 1

                elif action_type == "move_project":
                    target_project = action.get("target_project", "")
                    target_module = action.get("target_module", "")
                    targets = jira.get_move_targets(item["key"])
                    proj_id = None
                    for t in targets:
                        if t.get("key") == target_project:
                            proj_id = t.get("id")
                            break
                    if proj_id:
                        field_values = {}
                        if target_module:
                            field_values["customfield_10123"] = target_module
                        resp = jira.move_issue(item["key"], proj_id,
                                               field_values=field_values)
                        if resp.get("success"):
                            item["result"] = "成功"
                            executed += 1
                            processed_keys.add(item["key"])
                            if _comment_text:
                                jira.add_comment(item["key"], _comment_text)
                        else:
                            item["result"] = f"失败: {resp.get('message', '')}"
                            errors += 1
                    else:
                        item["result"] = f"失败: 找不到项目 {target_project}"
                        errors += 1

                elif action_type == "auto_reply":
                    if not _comment_text:
                        item["result"] = "跳过: 回复内容为空"
                        errors += 1
                        continue
                    resp = jira.reply_and_close_via_transition(
                        item["key"], _comment_text,
                        custom_fields={"solution": _comment_text}
                    )
                    if resp.get("success"):
                        item["result"] = "成功"
                        executed += 1
                        processed_keys.add(item["key"])
                    else:
                        msg = resp.get('message', '')
                        if '401' in msg or 'Unauthorized' in msg:
                            item["result"] = "会话过期"
                            errors += 1
                            break
                        item["result"] = f"失败: {msg}"
                        errors += 1

            except Exception as e:
                item["result"] = f"异常: {str(e)}"
                errors += 1

        # 4. 更新统计 + 已处理列表（保留最近 500 个 key 防止无限膨胀）
        rule["processed_keys"] = list(processed_keys)[-500:]
        self._update_rule_stats(config, rule, len(matched), executed, errors)

        result["executed"] = executed
        result["errors"] = errors
        print(f"[Automation] 规则 '{rule['name']}' 执行完成: 匹配{len(matched)}, 去重跳过{skipped_dedup}, 成功{executed}, 失败{errors}")
        return result

    def _update_rule_stats(self, config, rule, matched, executed, errors, status=None):
        """更新规则执行统计并持久化"""
        stats = rule.setdefault("stats", {})
        stats["total_processed"] = stats.get("total_processed", 0) + executed
        stats["last_run_at"] = datetime.now().isoformat()
        if status:
            stats["last_run_status"] = status
        else:
            stats["last_run_status"] = "success" if errors == 0 else ("partial" if executed > 0 else "failed")
        stats["last_run_matched"] = matched
        stats["last_run_executed"] = executed
        stats["last_run_errors"] = errors
        self.save_board_config(config)

    def run_all_enabled_rules(self) -> Dict:
        """定时轮询入口：自动执行所有 enabled 规则"""
        rules = self.get_automation_rules()
        enabled = [r for r in rules if r.get("enabled")]
        if not enabled:
            return {"ran": 0, "results": []}

        print(f"[Automation] 定时轮询: {len(enabled)} 条启用规则")
        results = []
        for rule in enabled:
            try:
                res = self.run_automation_rule(rule["id"], dry_run=False)
                results.append({"rule": rule["name"], "matched": res.get("matched", 0),
                                "executed": res.get("executed", 0), "errors": res.get("errors", 0),
                                "status": res.get("success")})
                # 会话过期时停止后续规则
                if "会话已过期" in res.get("error", ""):
                    print(f"[Automation] Jira 会话过期，停止后续规则执行")
                    break
            except Exception as e:
                print(f"[Automation] 规则 '{rule['name']}' 异常: {e}")
                results.append({"rule": rule["name"], "error": str(e)})

        total_executed = sum(r.get("executed", 0) for r in results)
        total_errors = sum(r.get("errors", 0) for r in results)
        print(f"[Automation] 轮询完成: {len(results)}条规则, 共执行{total_executed}条, 失败{total_errors}条")
        return {"ran": len(results), "total_executed": total_executed, "total_errors": total_errors, "results": results}

    def cleanup(self):
        """清理资源"""
        self.worker.stop()


# 工具函数
def asdict(obj):
    return {k: v for k, v in obj.__dict__.items()}

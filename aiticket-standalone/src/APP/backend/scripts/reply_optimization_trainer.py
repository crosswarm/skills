#!/usr/bin/env python3
"""
智能回复优化训练器 v1.0
三智能体协作框架：

A (知识分析师 + 出题员)
  - 深度理解KB知识库 + 历史人工回复的思维模式
  - 归纳各主题问题的常见处理套路，提供给B学习
  - 从真实工单数据中精简、仿制、聚焦后出题

B (学习者 + 答题员)
  - 学习KB + A提供的思维模式套路
  - 答A的测试题
  - 实时从C的审核意见中学习成长，不断完善知识体系

C (质量审核员)
  - 评审维度（优先级降序）：
    1. 实际可解决问题度（有切实依据）
    2. 操作步骤正确性
    3. 拒绝问题的专业程度（需提供依据）
    4. 符合用户人工回复风格
  - 跑100个问题，输出验证报告
  - 汇总经验 → 反哺智能回复模块

用法：
  python reply_optimization_trainer.py                 # 运行100题
  python reply_optimization_trainer.py --questions 20  # 快速测试
  python reply_optimization_trainer.py --resume        # 继续上次未完成的会话
"""

import sys
import os
import json
import time
import argparse
import hashlib
import textwrap
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── 路径设置 ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent

sys.path.insert(0, str(BACKEND_DIR))

TRAINING_DIR = PROJECT_ROOT / "conclusion" / "_local" / "training"
SESSIONS_DIR = TRAINING_DIR / "sessions"
STATE_FILE = TRAINING_DIR / "trainer_state.json"
PATTERN_LIBRARY_FILE = TRAINING_DIR / "pattern_library.json"

TRAINING_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)

# ── QCL 连接配置 ───────────────────────────────────────────────────────────────
# QCL 后端 URL（从 Mini 通过 SSH 连接，默认端口18000）
# Mini 上通过 `ssh qcl` 可直接到达 QCL
QCL_BACKEND_URL = os.environ.get("QCL_BACKEND_URL", "http://ticket.spux.cn")
QCL_SSH_HOST = os.environ.get("QCL_SSH_HOST", "qcl")
QCL_REMOTE_DIR = os.environ.get("QCL_REMOTE_DIR", "/opt/ai-ticket")


def pull_qcl_examples(trainer, since_hours: int = 168) -> int:
    """
    从 QCL 后端拉取最近 since_hours 小时的用户回复样本，
    导入到本地训练器的 KB（增量同步，已有则跳过）。
    返回：导入的新样本数量
    """
    import urllib.request
    url = f"{QCL_BACKEND_URL}/api/trainer/export-recent?since_hours={since_hours}"
    print(f"[Sync] 从 QCL 拉取样本: {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[Sync] 拉取失败（跳过）: {e}")
        return 0

    entries = data.get("entries", [])
    if not entries:
        print(f"[Sync] QCL 无新样本（{since_hours}h内）")
        return 0

    # 读取本地已有的 issue_key 集合（避免重复导入）
    log_path = BACKEND_DIR / "data" / "reply_trainer" / "feedback_log.jsonl"
    local_keys = set()
    if log_path.exists():
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    local_keys.add(json.loads(line.strip()).get("issue_key", ""))
                except Exception:
                    pass

    imported = 0
    for entry in entries:
        key = entry.get("issue_key", "")
        if not key or key in local_keys:
            continue
        try:
            trainer.record_feedback(
                issue_key=key,
                ticket_summary=entry.get("ticket_summary", ""),
                ticket_desc=entry.get("ticket_desc", ""),
                ai_original=entry.get("ai_original", ""),
                user_final=entry.get("user_final", ""),
                adopted=entry.get("adopted", True),
                reply_method=entry.get("reply_method", ""),
                issue_type=entry.get("issue_type", ""),
                is_style_owner=entry.get("is_style_owner", True),
            )
            local_keys.add(key)
            imported += 1
        except Exception as e:
            print(f"[Sync] 导入 {key} 失败: {e}")

    print(f"[Sync] 从 QCL 导入新样本: {imported} 条")
    return imported


def push_artifacts_to_qcl():
    """
    将训练产物推送到 QCL 服务器（rsync）：
    - data/reply_style_rules.md（C 汇总的改进规则，智能回复直接读取）
    - conclusion/_local/training/pattern_library.json（A 提炼的思维模式库）
    - conclusion/_local/training/trainer_state.json（B 的累积学习状态）
    """
    import subprocess
    artifacts = [
        (
            str(BACKEND_DIR / "data" / "reply_style_rules.md"),
            f"{QCL_SSH_HOST}:{QCL_REMOTE_DIR}/APP/backend/data/reply_style_rules.md",
        ),
        (
            str(PATTERN_LIBRARY_FILE),
            f"{QCL_SSH_HOST}:{QCL_REMOTE_DIR}/conclusion/_local/training/pattern_library.json",
        ),
        (
            str(STATE_FILE),
            f"{QCL_SSH_HOST}:{QCL_REMOTE_DIR}/conclusion/_local/training/trainer_state.json",
        ),
    ]
    success = 0
    for src, dst in artifacts:
        if not Path(src).exists():
            continue
        try:
            # 确保远端目录存在
            remote_dir = dst.rsplit("/", 1)[0].split(":", 1)[1]
            subprocess.run(
                ["ssh", QCL_SSH_HOST, f"mkdir -p {remote_dir}"],
                timeout=10, capture_output=True
            )
            result = subprocess.run(
                ["rsync", "-az", src, dst],
                timeout=30, capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"[Sync→QCL] ✓ {Path(src).name}")
                success += 1
            else:
                print(f"[Sync→QCL] ✗ {Path(src).name}: {result.stderr[:100]}")
        except Exception as e:
            print(f"[Sync→QCL] ✗ {Path(src).name}: {e}")
    print(f"[Sync→QCL] 推送完成 {success}/{len(artifacts)} 项")
    return success


# ── mem0 记忆服务（B 的跨会话语义记忆）────────────────────────────────────────
def _init_memory_service():
    """
    初始化 MemoryService（复用项目已有 services/memory_service.py）。
    失败时返回 None，训练可继续（降级为 JSON 模式）。
    """
    try:
        from services.memory_service import MemoryService
        svc = MemoryService()
        print("[mem0] MemoryService 初始化成功")
        return svc
    except Exception as e:
        print(f"[mem0] 初始化失败，降级为 JSON 模式: {e}")
        return None


# ── LLM 调用 ──────────────────────────────────────────────────────────────────
def _load_llm_config() -> dict:
    cfg_path = BACKEND_DIR / "llm_config.json"
    with open(cfg_path, encoding="utf-8") as f:
        raw = json.load(f)
    # Check feature routing first (reply_trainer → _default → last_provider)
    routing_path = BACKEND_DIR / "llm_feature_routing.json"
    provider = raw.get("last_provider", "minimax")
    if routing_path.exists():
        try:
            with open(routing_path, encoding="utf-8") as rf:
                routing = json.load(rf)
            provider = routing.get("reply_trainer") or routing.get("_default") or provider
        except Exception:
            pass
    pc = raw.get(provider, {})
    return {
        "provider": provider,
        "api_key": pc.get("api_key", ""),
        "model_name": pc.get("model_name", ""),
        "base_url": pc.get("base_url", ""),
    }


def llm_call(system: str, user: str, cfg: dict, max_retries: int = 2) -> str:
    """OpenAI-compatible 单次同步调用，返回完整回复文本。"""
    from openai import OpenAI

    client = OpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"] or "https://api.openai.com/v1",
        timeout=60,
    )
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=cfg["model_name"],
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                stream=False,
                temperature=0.3,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            if attempt < max_retries:
                time.sleep(3)
            else:
                print(f"[LLM] 调用失败: {e}")
                return ""


# ── 数据加载工具 ───────────────────────────────────────────────────────────────
def load_kb_runtime():
    """加载KB运行时服务（延迟导入）"""
    try:
        from kb_runtime_service import KnowledgeRuntimeService
        return KnowledgeRuntimeService()
    except Exception as e:
        print(f"[KB] 初始化失败: {e}")
        return None


def load_reply_trainer():
    """加载回复训练器（用于访问历史人工回复）"""
    try:
        from reply_trainer import ReplyTrainer
        return ReplyTrainer()
    except Exception as e:
        print(f"[ReplyTrainer] 初始化失败: {e}")
        return None


def load_board_data_sample(max_issues: int = 200) -> list:
    """从 board 数据缓存读取真实工单样本（不调用Jira API）"""
    issues = []
    # 尝试从分析缓存读取
    cache_dir = BACKEND_DIR / "data_cache"
    for cache_file in sorted(cache_dir.glob("*.json")):
        if len(issues) >= max_issues:
            break
        try:
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("issue_key"):
                issues.append({
                    "key": data["issue_key"],
                    "summary": data.get("issue_title", ""),
                    "description": data.get("issue_description", ""),
                    "problem_type": data.get("problem_type", ""),
                    "solution": data.get("solution_suggestion", ""),
                })
        except Exception:
            continue

    # 如果缓存不足，读取board_config
    board_config = BACKEND_DIR / "data" / "board_config.json"
    if board_config.exists() and len(issues) < 20:
        try:
            with open(board_config, encoding="utf-8") as f:
                cfg = json.load(f)
            recent = cfg.get("recent_issues", [])
            for iss in recent[:max_issues]:
                issues.append({
                    "key": iss.get("key", ""),
                    "summary": iss.get("summary", ""),
                    "description": iss.get("description", ""),
                    "problem_type": "",
                    "solution": "",
                })
        except Exception:
            pass
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Agent A：知识分析师 + 出题员
# ─────────────────────────────────────────────────────────────────────────────
class AgentA:
    """
    职责：
    1. 分析KB知识库 + 历史人工回复，提炼思维模式 & 套路
    2. 从真实工单中精简、仿制出测试题
    3. 将分析结果（pattern_library）写入磁盘，供B学习
    """

    def __init__(self, cfg: dict, kb, trainer):
        self.cfg = cfg
        self.kb = kb
        self.trainer = trainer
        self.patterns: dict = {}

    def analyze_patterns(self) -> dict:
        """
        深度分析人工回复思维模式。
        返回：{思维模式: {描述, 触发场景, 常见套路[], 示例}}
        """
        print("[A] 正在分析KB + 历史回复模式...")

        # 1. 读取风格规则文件
        style_rules_text = ""
        rules_file = BACKEND_DIR / "data" / "reply_style_rules.md"
        if rules_file.exists():
            style_rules_text = rules_file.read_text(encoding="utf-8")[:3000]

        # 2. 读取历史回复示例
        examples_text = ""
        if self.trainer:
            categories = ["方案解决", "指导解决", "暂不支持", "纳入需求库", "后续上线解决"]
            all_examples = []
            for cat in categories:
                hits = self.trainer.search_examples(cat, top_k=3)
                all_examples.extend(hits)
            examples_text = "\n\n".join([
                f"[{e.get('reply_method', '')} / {e.get('issue_type', '')}]\n"
                f"题目: {e.get('summary', '')}\n"
                f"回复: {e.get('reply', '')[:300]}"
                for e in all_examples[:15]
            ])

        # 3. 读取KB主要内容
        kb_summary = ""
        if self.kb:
            try:
                bundle = self.kb.search_bundle("工作流审批流程 常见问题", top_k=5)
                kb_items = bundle.get("items", [])[:5]
                kb_summary = "\n".join([
                    f"- [{i.get('l1_module','')}/{i.get('l2_module','')}] {i.get('name','')}: {i.get('chunk_preview','')[:200]}"
                    for i in kb_items
                ])
            except Exception:
                pass

        # 4. 请LLM分析思维模式
        system = "你是一位经验丰富的客服知识库分析专家，擅长提炼支持团队的回复思维模式。"
        user = f"""请分析以下材料，提炼出支持工程师处理工单时的**核心思维模式**。

## 风格规则摘要
{style_rules_text[:1500]}

## 历史回复示例
{examples_text[:2000]}

## KB知识库摘要
{kb_summary[:1000]}

请输出JSON格式：
{{
  "thinking_modes": [
    {{
      "name": "思维模式名称",
      "description": "一句话描述这种模式的本质",
      "trigger_scenarios": ["触发场景1", "触发场景2"],
      "common_routines": [
        "套路1：先确认问题再给方案",
        "套路2：操作步骤分点列出"
      ],
      "key_phrases": ["常用短语1", "常用短语2"],
      "example_reply_prefix": "回复开头示例"
    }}
  ],
  "topic_handling": {{
    "审批人问题": ["定位审批模板", "检查分支条件", "确认人员权限"],
    "流程卡住": ["查看当前节点", "检查条件表达式", "手动干预"],
    "功能不支持": ["说明当前版本限制", "提供替代方案或时间线"]
  }},
  "style_signature": {{
    "greeting": "您好！",
    "closing_options": ["谢谢理解", "感谢配合", "谢谢"],
    "tone": "正式礼貌，结论先行"
  }}
}}"""

        raw = llm_call(system, user, self.cfg)
        patterns = {}
        try:
            # 提取JSON
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                patterns = json.loads(raw[start:end])
        except Exception:
            patterns = {"thinking_modes": [], "topic_handling": {}, "style_signature": {}}

        self.patterns = patterns
        # 持久化
        PATTERN_LIBRARY_FILE.write_text(
            json.dumps(patterns, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[A] 模式分析完成，归纳 {len(patterns.get('thinking_modes', []))} 种思维模式")
        return patterns

    def generate_questions(self, n: int = 100, real_issues: list = None) -> list:
        """
        从真实工单中精简、仿制、聚焦后出题。
        返回：[{id, issue_key, summary, description, category, difficulty, hint}]
        """
        print(f"[A] 正在出 {n} 道测试题...")
        if not real_issues:
            real_issues = load_board_data_sample(max_issues=300)

        # 过滤有效工单
        valid = [
            i for i in real_issues
            if len(i.get("summary", "")) > 5
        ][:n * 3]  # 取3倍备用

        if not valid:
            print("[A] 没有可用的真实工单，使用模板题")
            return self._template_questions(n)

        # 批量让LLM精简/仿制
        batch_size = 10
        questions = []
        q_id = 0

        for batch_start in range(0, min(len(valid), n * 2), batch_size):
            if len(questions) >= n:
                break
            batch = valid[batch_start: batch_start + batch_size]
            batch_text = "\n".join([
                f"{idx+1}. [KEY:{iss['key']}] {iss['summary']}\n   描述片段: {iss.get('description','')[:150]}"
                for idx, iss in enumerate(batch)
            ])

            system = "你是一位出题专家，负责从真实工单中提炼精简、聚焦的测试题。"
            user = f"""从以下真实工单中，精选并改写为测试题。要求：
1. 保留核心技术问题，去除客户名称等敏感信息
2. 描述要足够具体，让答题者能给出操作步骤
3. 每题分配一个类别和难度

真实工单：
{batch_text}

输出JSON数组（每条工单对应一个题目）：
[
  {{
    "source_key": "原工单KEY",
    "summary": "精简后的问题标题",
    "description": "改写后的问题描述（100-200字）",
    "category": "类别（如：审批人问题/流程卡住/功能咨询/数据错误/配置问题）",
    "difficulty": "easy/medium/hard",
    "scoring_hint": "评分提示（期待答案应包含的关键点）"
  }}
]"""

            raw = llm_call(system, user, self.cfg)
            try:
                start = raw.find("[")
                end = raw.rfind("]") + 1
                if start >= 0 and end > start:
                    items = json.loads(raw[start:end])
                    for item in items:
                        if len(questions) >= n:
                            break
                        q_id += 1
                        questions.append({
                            "id": q_id,
                            "source_key": item.get("source_key", ""),
                            "summary": item.get("summary", ""),
                            "description": item.get("description", ""),
                            "category": item.get("category", "未分类"),
                            "difficulty": item.get("difficulty", "medium"),
                            "scoring_hint": item.get("scoring_hint", ""),
                        })
            except Exception as e:
                print(f"[A] 出题批次解析失败: {e}")

            time.sleep(1)  # 限速

        # 若题目不够，用模板补全
        if len(questions) < n:
            questions.extend(self._template_questions(n - len(questions)))
            for i, q in enumerate(questions):
                q["id"] = i + 1

        print(f"[A] 共出 {len(questions)} 道题")
        return questions[:n]

    def _template_questions(self, n: int) -> list:
        """内置模板题（当真实工单不足时使用）"""
        templates = [
            {"summary": "审批流中找不到下一级审批人", "description": "用户反映发起审批后，系统一直在等待审批人处理，但被指定的审批人登录后看不到任何待审批工单。请问如何定位问题并解决？", "category": "审批人问题", "difficulty": "medium"},
            {"summary": "条件分支走错了路径", "description": "配置了金额>10000走总经理审批，但实际金额15000的单子走了部门经理审批。审批模板条件表达式如何排查？", "category": "流程配置", "difficulty": "hard"},
            {"summary": "流程节点卡住无法继续", "description": "某张采购申请单已在'财务审核'节点停了3天，审批人说已经点了同意，但工单状态还是'审核中'。", "category": "流程卡住", "difficulty": "medium"},
            {"summary": "能否支持多人同时会签", "description": "客户问工作流是否支持配置多个人同时会签，所有人都同意后才能进入下一节点。目前版本是否有此功能？", "category": "功能咨询", "difficulty": "easy"},
            {"summary": "撤回申请后重新发起报错", "description": "用户撤回了一张已发起的申请，修改内容后重新发起，系统提示'流程实例已存在'错误，无法提交。", "category": "数据错误", "difficulty": "hard"},
            {"summary": "委托审批后权限问题", "description": "A将审批权委托给B后，B在移动端可以看到工单但点击审批按钮时提示'无操作权限'。", "category": "权限问题", "difficulty": "medium"},
            {"summary": "表单字段在审批环节消失了", "description": "发起表单时有15个字段，但到了第二个审批节点，有3个字段不显示了，审批人无法查看完整信息。", "category": "表单问题", "difficulty": "medium"},
            {"summary": "工作流通知邮件发送失败", "description": "配置了审批节点邮件通知，测试环境正常，但生产环境发起流程后相关人员没有收到邮件。日志显示SMTP连接超时。", "category": "通知问题", "difficulty": "hard"},
            {"summary": "如何配置自动化触发流程", "description": "客户希望当某个字段值变化时（如状态从'草稿'变为'正式'），自动触发一个审批流，不需要人工手动发起，是否支持？", "category": "功能咨询", "difficulty": "medium"},
            {"summary": "历史数据流程状态显示异常", "description": "系统升级后，之前已完成的工单在列表中显示为'异常'状态，但实际业务上已经处理完毕。需要修复这批历史数据。", "category": "数据修复", "difficulty": "hard"},
        ]
        result = []
        for i in range(n):
            t = templates[i % len(templates)].copy()
            t["id"] = i + 1
            t["source_key"] = f"TEMPLATE-{i+1}"
            t["difficulty"] = t.get("difficulty", "medium")
            t["scoring_hint"] = ""
            result.append(t)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Agent B：学习者 + 答题员（有实时学习能力）
# ─────────────────────────────────────────────────────────────────────────────
class AgentB:
    """
    职责：
    1. 学习KB + A的思维模式
    2. 答题（查KB → 分析 → 生成回复）
    3. 实时从C的反馈中学习，将教训融入后续回复
    """

    MEM_USER_B = "trainer_agent_b"   # mem0 user_id：B 的专属记忆空间
    MEM_USER_C = "trainer_agent_c"   # C 的洞察也存这里，B 可检索

    def __init__(self, cfg: dict, kb, trainer, patterns: dict, mem_service=None):
        self.cfg = cfg
        self.kb = kb
        self.trainer = trainer
        self.patterns = patterns
        self.mem = mem_service        # MemoryService 实例（可为 None，降级 JSON）
        # 降级缓冲（mem0 不可用时使用）
        self.lessons: list[str] = []
        self.weak_areas: dict = {}
        self.strengths: list[str] = []
        self.total_feedback_absorbed = 0
        self._style_sig = patterns.get("style_signature", {})

    def _build_study_context(self, query: str = "") -> str:
        """
        构建学习材料摘要（A的思维模式 + mem0语义检索到的相关教训）。
        query: 当前问题关键词，用于精准检索相关记忆。
        """
        mode_list = self.patterns.get("thinking_modes", [])
        modes_text = "\n".join([
            f"  【{m['name']}】: {m.get('description','')} | 套路: {'; '.join(m.get('common_routines',[])[:2])}"
            for m in mode_list[:5]
        ])
        topic_handling = json.dumps(
            self.patterns.get("topic_handling", {}),
            ensure_ascii=False
        )[:500]

        # ── mem0 语义检索：只取与当前题目相关的教训 ──────────────────────────
        lessons_text = ""
        if self.mem and query:
            try:
                # B 自己的教训
                b_mems = self.mem.get_context(self.MEM_USER_B, query)
                # C 对类似问题的洞察（跨角色检索）
                c_mems = self.mem.get_context(self.MEM_USER_C, query)
                all_mems = b_mems[:5] + c_mems[:3]
                if all_mems:
                    lines = [f"  - {m.get('memory', '')}" for m in all_mems if m.get("memory")]
                    lessons_text = "\n【相关历史教训（语义匹配，置信度≥0.6）】\n" + "\n".join(lines)
            except Exception as e:
                pass  # 降级到 JSON 模式

        # 降级：mem0 不可用或无匹配时使用 JSON 缓冲
        if not lessons_text and self.lessons:
            recent = self.lessons[-8:]
            lessons_text = "\n【从C的审核中学到的教训】\n" + "\n".join(f"  - {l}" for l in recent)

        weak_text = ""
        if self.weak_areas:
            weak_text = "\n【需要特别加强的领域】\n" + "\n".join(
                f"  {cat}: {'; '.join(issues[:2])}"
                for cat, issues in list(self.weak_areas.items())[:3]
            )

        return f"""## 思维模式
{modes_text}

## 各主题处理套路
{topic_handling}

## 回复风格要求
- 开头: {self._style_sig.get('greeting', '您好！')}
- 结尾: {' / '.join(self._style_sig.get('closing_options', ['谢谢']))}
- 基调: {self._style_sig.get('tone', '正式礼貌，结论先行')}
{lessons_text}{weak_text}"""

    def generate_reply(self, question: dict) -> dict:
        """
        为一道题生成回复。
        返回：{reply, kb_sources, method, word_count}
        """
        q_summary = question.get("summary", "")
        q_desc = question.get("description", "")
        category = question.get("category", "")

        # 1. 从KB搜索相关知识
        kb_context = ""
        kb_sources = []
        if self.kb:
            try:
                query = f"{q_summary} {q_desc[:100]}"
                bundle = self.kb.search_bundle(query, top_k=4)
                items = bundle.get("items", [])[:4]
                kb_context = "\n".join([
                    f"[{i.get('name','')}] {i.get('chunk_preview','')[:300]}"
                    for i in items
                ])
                kb_sources = [i.get("name", "") for i in items]
            except Exception:
                pass

        # 2. 从历史回复中找风格参考
        style_refs = ""
        if self.trainer:
            try:
                examples = self.trainer.search_examples(q_summary, top_k=2)
                style_refs = "\n".join([
                    f"[参考-{e.get('reply_method','')}] {e.get('reply','')[:250]}"
                    for e in examples if e.get("reply")
                ])
            except Exception:
                pass

        # 3. 构建学习材料（传入当前题目关键词，mem0 精准检索相关教训）
        mem_query = f"{category} {q_summary}"
        study_ctx = self._build_study_context(query=mem_query)

        # 4. LLM生成回复
        system = f"""你是一位专业的工作流/审批流产品支持工程师，正在接受训练以提升回复质量。

{study_ctx}"""

        user = f"""请对以下工单问题生成一条专业支持回复。

**问题类别**: {category}
**问题标题**: {q_summary}
**问题描述**:
{q_desc}

**KB知识库参考**:
{kb_context or '（无相关KB内容）'}

**历史回复风格参考**:
{style_refs or '（无参考）'}

要求：
1. 直接输出回复正文，不要输出JSON或元数据
2. 开头必须是"您好！"
3. 提供具体可执行的操作步骤（如有）
4. 如果功能不支持，说明依据并提供替代方案
5. 结尾礼貌收尾
6. 控制在150-400字

回复："""

        reply_text = llm_call(system, user, self.cfg)
        return {
            "reply": reply_text,
            "kb_sources": kb_sources,
            "word_count": len(reply_text.replace(" ", "").replace("\n", "")),
        }

    def learn_from_feedback(self, question: dict, reply: str, evaluation: dict):
        """
        实时从C的审核结果中学习。
        优先写入 mem0（语义向量存储，跨会话持久化）；
        同步维护 JSON 降级缓冲（mem0 不可用时使用）。
        """
        scores = evaluation.get("scores", {})
        feedback = evaluation.get("feedback", "")
        improvements = evaluation.get("improvements", [])
        strong_points = evaluation.get("strong_points", [])
        total = evaluation.get("total_score", 0)
        cat = question.get("category", "未知")
        src_key = question.get("source_key") or question.get("id", "training")

        # ── 写入 mem0 ─────────────────────────────────────────────────────────
        if self.mem:
            # 1. 改进教训：每条单独存为一条记忆（便于精准检索）
            for imp in improvements:
                if imp and len(imp) > 5:
                    lesson = f"[{cat}] {imp}"
                    try:
                        self.mem.add_learning(
                            user_id=self.MEM_USER_B,
                            content=lesson,
                            metadata={
                                "source_ticket_id": str(src_key),
                                "category": cat,
                                "dimension": "improvement",
                                "score": total,
                                "question_summary": question.get("summary", "")[:100],
                            }
                        )
                    except Exception:
                        pass

            # 2. 强项：总分≥8时记录为正向记忆（置信度随使用提升）
            if total >= 8:
                for sp in strong_points[:2]:
                    if sp and len(sp) > 5:
                        try:
                            self.mem.add_learning(
                                user_id=self.MEM_USER_B,
                                content=f"[{cat}] 有效做法: {sp}",
                                metadata={
                                    "source_ticket_id": str(src_key),
                                    "category": cat,
                                    "dimension": "strength",
                                    "score": total,
                                }
                            )
                        except Exception:
                            pass

            # 3. 低分弱项：综合反馈存为警示记忆
            solvability = scores.get("solvability", 10)
            if solvability < 6 and feedback:
                try:
                    self.mem.add_learning(
                        user_id=self.MEM_USER_B,
                        content=f"[{cat}] 警示（可解决性低分）: {feedback[:200]}",
                        metadata={
                            "source_ticket_id": str(src_key),
                            "category": cat,
                            "dimension": "weak_solvability",
                            "score": solvability,
                        }
                    )
                except Exception:
                    pass

        # ── JSON 降级缓冲（mem0 不可用 或 作为本会话即时缓存）─────────────────
        for imp in improvements:
            if imp and len(imp) > 5:
                lesson = f"[{cat}] {imp}"
                if lesson not in self.lessons:
                    self.lessons.append(lesson)

        solvability = scores.get("solvability", 10)
        if solvability < 7:
            self.weak_areas.setdefault(cat, [])
            if feedback and len(self.weak_areas[cat]) < 5:
                self.weak_areas[cat].append(feedback[:80])

        if total >= 8:
            for sp in strong_points[:2]:
                if sp and sp not in self.strengths:
                    self.strengths.append(sp)

        self.total_feedback_absorbed += 1
        if self.total_feedback_absorbed % 10 == 0:
            mem_hint = "mem0✓" if self.mem else "JSON降级"
            print(f"  [B/{mem_hint}] 已吸收 {self.total_feedback_absorbed} 条反馈，"
                  f"本会话教训 {len(self.lessons)} 条，弱项类别 {len(self.weak_areas)} 个")

    def get_learning_summary(self) -> dict:
        """返回B当前的学习状态摘要（含 mem0 跨会话记忆总量）"""
        mem0_total = 0
        mem0_health = {}
        if self.mem:
            try:
                all_b = self.mem.list_memories(self.MEM_USER_B)
                mem0_total = len(all_b)
                mem0_health = self.mem.get_health_report(self.MEM_USER_B)
            except Exception:
                pass
        return {
            "total_feedback_absorbed": self.total_feedback_absorbed,
            "lessons_count": len(self.lessons),        # 本会话 JSON 缓冲
            "lessons_sample": self.lessons[-5:],
            "mem0_total_memories": mem0_total,          # 跨会话 mem0 总记忆数
            "mem0_health": {k: v for k, v in mem0_health.items()
                            if k in ("accuracy", "diversity", "recency", "contradiction_rate")},
            "weak_areas": self.weak_areas,
            "strengths_count": len(self.strengths),
            "strengths_sample": self.strengths[:5],
            "backend": "mem0+json" if self.mem else "json_only",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Agent C：质量审核员
# ─────────────────────────────────────────────────────────────────────────────
class AgentC:
    """
    职责：
    1. 按4个维度评审B的回复（优先级降序）
    2. 提供具体改进意见
    3. 汇总100题报告 + 经验总结
    """

    DIMENSIONS = [
        ("solvability", "实际可解决问题度", 40),    # 权重40%
        ("correctness", "操作步骤正确性", 30),       # 权重30%
        ("professionalism", "拒绝/边界问题专业度", 20),  # 权重20%
        ("style", "符合人工回复风格", 10),           # 权重10%
    ]

    MEM_USER_C = "trainer_agent_c"   # C 的系统性洞察存储空间

    def __init__(self, cfg: dict, kb, mem_service=None):
        self.cfg = cfg
        self.kb = kb
        self.mem = mem_service
        self.evaluations: list = []

    def evaluate_reply(self, question: dict, reply: str) -> dict:
        """
        对单条回复进行多维度评审。
        返回：{scores, total_score, feedback, improvements, strong_points, pass}
        """
        q_summary = question.get("summary", "")
        q_desc = question.get("description", "")
        category = question.get("category", "")
        scoring_hint = question.get("scoring_hint", "")

        # 搜索KB用于核实步骤正确性
        kb_verify = ""
        if self.kb:
            try:
                bundle = self.kb.search_bundle(q_summary, top_k=2)
                items = bundle.get("items", [])[:2]
                kb_verify = "\n".join([i.get("chunk_preview", "")[:200] for i in items])
            except Exception:
                pass

        system = """你是一位严格的客服回复质量审核专家。你的评审必须有具体依据，不接受模糊评价。"""

        user = f"""请对以下工单回复进行多维度质量评审。

## 工单信息
- 类别: {category}
- 标题: {q_summary}
- 描述: {q_desc}
- 评分提示: {scoring_hint or '无'}

## KB参考（用于核实步骤正确性）
{kb_verify or '无'}

## 待评审的回复
{reply}

## 评审维度（按优先级降序，必须有具体依据）

1. **实际可解决问题度（0-10分，权重最高）**
   - 回复是否真的能帮用户解决问题？
   - 操作步骤是否可执行？
   - 是否有遗漏的关键信息？

2. **操作步骤正确性（0-10分）**
   - 步骤顺序是否正确？
   - 技术细节是否准确？
   - 是否与KB知识库一致？

3. **拒绝/边界问题专业度（0-10分）**
   - 如涉及"不支持"/"无法实现"，是否提供了依据？
   - 是否给出了替代方案？
   - 措辞是否专业得体？（"暂不支持"优于"不能"）

4. **符合人工回复风格（0-10分，权重最低）**
   - 是否以"您好！"开头？
   - 结尾是否礼貌收尾？
   - 语气是否符合"结论先行+简洁说明"风格？

请输出JSON：
{{
  "scores": {{
    "solvability": <0-10>,
    "correctness": <0-10>,
    "professionalism": <0-10>,
    "style": <0-10>
  }},
  "feedback": "综合评价（2-3句话，具体指出最主要的问题）",
  "improvements": [
    "改进意见1（具体、可执行）",
    "改进意见2（如有）"
  ],
  "strong_points": [
    "做得好的地方1（如有）"
  ],
  "verdict": "pass/fail（总分>=70分视为pass）"
}}"""

        raw = llm_call(system, user, self.cfg)
        result = {
            "scores": {"solvability": 5, "correctness": 5, "professionalism": 5, "style": 5},
            "feedback": "评审解析失败",
            "improvements": [],
            "strong_points": [],
            "verdict": "fail",
            "total_score": 50,
            "passed": False,
        }

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(raw[start:end])
                scores = parsed.get("scores", {})
                # 加权总分
                s = scores.get("solvability", 5)
                c = scores.get("correctness", 5)
                p = scores.get("professionalism", 5)
                st = scores.get("style", 5)
                total = s * 0.4 + c * 0.3 + p * 0.2 + st * 0.1
                result.update({
                    "scores": scores,
                    "total_score": round(total, 1),
                    "feedback": parsed.get("feedback", ""),
                    "improvements": parsed.get("improvements", []),
                    "strong_points": parsed.get("strong_points", []),
                    "verdict": parsed.get("verdict", "fail"),
                    "passed": total >= 7.0,
                })
                # ── 把系统性洞察存入 mem0，供 B 跨会话学习 ──────────────────
                if self.mem:
                    feedback_text = parsed.get("feedback", "")
                    improvements = parsed.get("improvements", [])
                    # 低分 + 有具体反馈：存为 C 的结构性洞察
                    if total < 7.0 and feedback_text and len(improvements) > 0:
                        insight = (
                            f"[{category}] 常见失误: {feedback_text[:150]} "
                            f"| 改进: {improvements[0][:100]}"
                        )
                        try:
                            self.mem.add_learning(
                                user_id=self.MEM_USER_C,
                                content=insight,
                                metadata={
                                    "source_ticket_id": f"eval_{category}",
                                    "category": category,
                                    "total_score": round(total, 1),
                                    "dimension": "c_insight",
                                }
                            )
                        except Exception:
                            pass
        except Exception as e:
            print(f"[C] 评审解析失败: {e}")

        return result

    def generate_session_report(self, questions: list, replies: list,
                                evaluations: list, b_learning: dict,
                                session_dir: Path) -> str:
        """
        生成会话完整报告，汇总经验并输出改进建议。
        """
        print("[C] 正在生成完整验证报告...")
        n = len(evaluations)
        if n == 0:
            return "无数据"

        # 统计
        total_scores = [e.get("total_score", 0) for e in evaluations]
        pass_count = sum(1 for e in evaluations if e.get("passed"))
        avg_score = sum(total_scores) / n
        dim_avgs = {}
        for dim, _, _ in self.DIMENSIONS:
            vals = [e["scores"].get(dim, 0) for e in evaluations if e.get("scores")]
            dim_avgs[dim] = round(sum(vals) / len(vals), 1) if vals else 0

        # 按类别分析
        cat_stats = {}
        for q, e in zip(questions, evaluations):
            cat = q.get("category", "未知")
            cat_stats.setdefault(cat, {"count": 0, "total": 0})
            cat_stats[cat]["count"] += 1
            cat_stats[cat]["total"] += e.get("total_score", 0)
        cat_summary = {c: round(v["total"] / v["count"], 1) for c, v in cat_stats.items()}

        # 最差5题
        worst = sorted(
            zip(questions, evaluations),
            key=lambda x: x[1].get("total_score", 0)
        )[:5]

        # 收集所有改进意见
        all_improvements = []
        for e in evaluations:
            all_improvements.extend(e.get("improvements", []))
        improvement_freq = {}
        for imp in all_improvements:
            key = imp[:50]
            improvement_freq[key] = improvement_freq.get(key, 0) + 1
        top_improvements = sorted(improvement_freq.items(), key=lambda x: -x[1])[:10]

        # 请LLM生成深度改进建议
        system = "你是一位AI训练质量总监，负责基于评审数据提出系统性改进建议。"
        user = f"""基于以下评审数据，为智能回复系统提出具体改进建议。

## 评审统计
- 总题数: {n}
- 通过率: {pass_count}/{n} ({round(pass_count/n*100,1)}%)
- 平均总分: {avg_score:.1f}/10
- 各维度均分:
  - 实际可解决问题度: {dim_avgs.get('solvability',0)}
  - 操作步骤正确性: {dim_avgs.get('correctness',0)}
  - 专业度: {dim_avgs.get('professionalism',0)}
  - 风格: {dim_avgs.get('style',0)}

## 各类别平均分
{json.dumps(cat_summary, ensure_ascii=False)}

## 最高频改进意见
{json.dumps([i[0] for i in top_improvements[:8]], ensure_ascii=False)}

## B智能体学习状态
- 吸收反馈数: {b_learning.get('total_feedback_absorbed',0)}
- 累积教训数: {b_learning.get('lessons_count',0)}
- 弱项类别: {list(b_learning.get('weak_areas',{}).keys())}

请输出：
1. 系统性问题诊断（3-5条，每条一段话）
2. KB知识库补充建议（应该补充哪些内容）
3. 回复风格规则更新建议（具体条目）
4. B智能体下一轮训练重点

用Markdown格式输出。"""

        improvement_md = llm_call(system, user, self.cfg)

        # 组装报告
        worst_cases_md = "\n".join([
            f"- [{q.get('category','')}] **{q.get('summary','')}** → 得分 {e.get('total_score',0):.1f} | {e.get('feedback','')[:80]}"
            for q, e in worst
        ])

        report = f"""# 智能回复优化训练报告
**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}
**题目数量**: {n}

---

## 一、总体评分

| 指标 | 数值 |
|------|------|
| 通过率 | {pass_count}/{n} ({round(pass_count/n*100,1)}%) |
| 平均总分 | {avg_score:.1f}/10 |
| 实际可解决问题度 | {dim_avgs.get('solvability',0)}/10 |
| 操作步骤正确性 | {dim_avgs.get('correctness',0)}/10 |
| 专业度 | {dim_avgs.get('professionalism',0)}/10 |
| 回复风格 | {dim_avgs.get('style',0)}/10 |

## 二、各类别表现

| 类别 | 平均分 |
|------|--------|
{"".join(f"| {c} | {s} |\n" for c, s in sorted(cat_summary.items(), key=lambda x: x[1]))}

## 三、B智能体学习成长情况

- 本次吸收反馈: **{b_learning.get('total_feedback_absorbed',0)} 条**
- 累积教训总结: **{b_learning.get('lessons_count',0)} 条**
- 识别弱项领域: {', '.join(b_learning.get('weak_areas',{}).keys()) or '无'}
- 积累强项数量: {b_learning.get('strengths_count',0)} 条

## 四、最需改进的5道题

{worst_cases_md}

## 五、高频改进意见（Top 10）

{"".join(f"{i+1}. {imp[0]} (出现{imp[1]}次)\n" for i, imp in enumerate(top_improvements[:10]))}

---

## 六、系统性改进建议（C审核员分析）

{improvement_md}

---
*本报告由智能回复优化训练器自动生成*
"""

        report_path = session_dir / "report.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"[C] 报告已保存: {report_path}")
        return report

    def apply_improvements_to_system(self, evaluations: list, b_learning: dict):
        """
        将经验成果反哺到智能回复系统：
        1. 将高质量回复样本添加到训练库（通过reply_trainer）
        2. 将B的教训追加到风格规则文件
        """
        # 追加教训到风格规则文件（带去重 + 文件大小控制，避免被智能回复 8000 字符截断）
        rules_file = BACKEND_DIR / "data" / "reply_style_rules.md"
        if rules_file.exists() and b_learning.get("lessons_count", 0) > 0:
            lessons = b_learning.get("lessons_sample", [])
            if lessons:
                import re as _re
                existing_content = rules_file.read_text(encoding="utf-8")

                # 1. 收集已存在规则的归一化签名（取每条规则前 60 个去空白字符）
                existing_sigs = set()
                for line in existing_content.splitlines():
                    stripped = line.strip()
                    m = _re.match(r'^(?:\d+\.|[-•])\s*(.+)', stripped)
                    if m:
                        sig = _re.sub(r'\s+', '', m.group(1))[:60]
                        if sig:
                            existing_sigs.add(sig)

                # 2. 过滤：只保留真正新的教训
                unique_lessons = []
                for lesson in lessons:
                    sig = _re.sub(r'\s+', '', lesson)[:60]
                    if sig and sig not in existing_sigs:
                        unique_lessons.append(lesson)
                        existing_sigs.add(sig)

                if not unique_lessons:
                    print(f"[C→系统] 本期 {len(lessons)} 条教训全部与历史重复，跳过追加")
                else:
                    appendix = f"\n\n---\n## 训练器补充规则（{datetime.now().strftime('%Y-%m-%d')}）\n\n"
                    appendix += "以下规则来自C审核员对100道题的经验总结：\n"
                    for i, lesson in enumerate(unique_lessons, 1):
                        appendix += f"\n{i}. {lesson}"

                    # 3. 文件大小控制：如果追加后超过 7500 字符，先压缩（保留头部基础规则 + 最近 3 期追加）
                    projected_size = len(existing_content) + len(appendix)
                    if projected_size > 7500:
                        # 按 "## 训练器补充规则（" 分段，保留头部（分隔符前）+ 最近 3 期
                        sections = existing_content.split("\n\n---\n## 训练器补充规则（")
                        head = sections[0]
                        recent_sections = sections[-2:] if len(sections) > 3 else sections[1:]  # 保留最近 2 期旧的 + 本期新的
                        rebuilt = head
                        for s in recent_sections:
                            rebuilt += "\n\n---\n## 训练器补充规则（" + s
                        rules_file.write_text(rebuilt, encoding="utf-8")
                        print(f"[C→系统] 风格规则超过 7500 字符，已压缩（保留头部 + 最近 {len(recent_sections)} 期）")

                    with open(rules_file, "a", encoding="utf-8") as f:
                        f.write(appendix)
                    print(f"[C→系统] 已将 {len(unique_lessons)}/{len(lessons)} 条新教训追加到风格规则文件")

        # 记录本次会话指标（供下次训练参考）
        metrics_file = TRAINING_DIR / "training_metrics.jsonl"
        session_metric = {
            "timestamp": datetime.now().isoformat(),
            "n": len(evaluations),
            "avg_score": round(sum(e.get("total_score",0) for e in evaluations) / max(len(evaluations),1), 1),
            "pass_rate": round(sum(1 for e in evaluations if e.get("passed")) / max(len(evaluations),1), 3),
            "b_lessons_count": b_learning.get("lessons_count", 0),
        }
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(session_metric, ensure_ascii=False) + "\n")
        print(f"[C→系统] 指标已记录到 {metrics_file}")


# ─────────────────────────────────────────────────────────────────────────────
# 训练循环主控
# ─────────────────────────────────────────────────────────────────────────────
class TrainingLoop:
    def __init__(self, n_questions: int = 100, resume: bool = False, stop_hour: int = 0):
        self.n = n_questions
        self.stop_hour = stop_hour  # 0 = 不限制
        self.cfg = _load_llm_config()
        print(f"[训练器] 使用 LLM: {self.cfg['provider']} / {self.cfg['model_name']}")

        # 初始化KB
        print("[训练器] 加载KB服务...")
        self.kb = load_kb_runtime()
        self.trainer = load_reply_trainer()

        # 初始化 mem0 记忆服务（B 的跨会话语义记忆后端）
        print("[训练器] 初始化 mem0 记忆服务...")
        self.mem = _init_memory_service()

        # 加载持久化状态
        self.state = self._load_state() if resume else self._fresh_state()

        # 创建会话目录
        sess_id = self.state.get("session_count", 0) + 1
        self.state["session_count"] = sess_id
        self.session_dir = SESSIONS_DIR / f"session_{sess_id:03d}_{datetime.now().strftime('%Y%m%d_%H%M')}"
        self.session_dir.mkdir(exist_ok=True)
        print(f"[训练器] 会话目录: {self.session_dir}")

    def _fresh_state(self) -> dict:
        return {"session_count": 0, "total_questions": 0, "b_cumulative_lessons": []}

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return self._fresh_state()

    def _save_state(self, state: dict):
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def run(self):
        print(f"\n{'='*60}")
        print(f"  智能回复优化训练器 — 第 {self.state['session_count']} 期")
        print(f"  计划: {self.n} 道题")
        print(f"{'='*60}\n")

        # ── Phase 0: 同步 QCL 的最新用户回复样本到本地 KB ─────────────────────
        print("\n[Phase 0] 从 QCL 拉取用户最新回复样本（过去7天）")
        if self.trainer:
            pulled = pull_qcl_examples(self.trainer, since_hours=168)
            if pulled > 0:
                print(f"  ✓ 新增 {pulled} 条 QCL 真实回复样本，已进入训练 KB")
        else:
            print("  ⚠ trainer 未初始化，跳过 QCL 同步")

        # ── Phase 1: A 出题 + 分析模式 ───────────────────────────────────────
        print("\n[Phase 1] Agent A: 分析知识库 + 出题")
        # 加载或从磁盘恢复 patterns
        if PATTERN_LIBRARY_FILE.exists():
            try:
                patterns = json.loads(PATTERN_LIBRARY_FILE.read_text(encoding="utf-8"))
                print("[A] 从磁盘加载已有模式库")
            except Exception:
                patterns = {}
        else:
            patterns = {}

        agent_a = AgentA(self.cfg, self.kb, self.trainer)
        if not patterns.get("thinking_modes"):
            patterns = agent_a.analyze_patterns()
        else:
            agent_a.patterns = patterns

        real_issues = load_board_data_sample(max_issues=400)
        questions = agent_a.generate_questions(n=self.n, real_issues=real_issues)
        (self.session_dir / "questions.json").write_text(
            json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[A] 题目已保存")

        # ── Phase 2 & 3: B 答题 + C 实时评审（带B学习循环）────────────────────
        print(f"\n[Phase 2+3] Agent B 答题 / Agent C 实时评审（共{len(questions)}题）")

        # B：注入 mem0（历史教训跨会话语义持久化，无需手动恢复 JSON 列表）
        agent_b = AgentB(self.cfg, self.kb, self.trainer, patterns, mem_service=self.mem)
        # JSON 降级缓冲仍保留（mem0 不可用时使用）
        agent_b.lessons = list(self.state.get("b_cumulative_lessons", []))[-15:]

        # C：注入 mem0（评审洞察存储，供 B 检索）
        agent_c = AgentC(self.cfg, self.kb, mem_service=self.mem)

        replies = []
        evaluations = []

        for i, q in enumerate(questions):
            q_num = i + 1
            if self.stop_hour and datetime.now().hour >= self.stop_hour:
                print(f"\n[stop] 已到 {self.stop_hour}:00，停止训练（已完成 {i}/{len(questions)} 题）")
                break
            print(f"\n  ── 题 {q_num}/{len(questions)}: [{q.get('category','')}] {q.get('summary','')[:40]}")

            # B 生成回复
            reply_result = agent_b.generate_reply(q)
            reply_text = reply_result.get("reply", "（生成失败）")
            print(f"  [B] 回复: {reply_text[:60]}... ({reply_result.get('word_count',0)}字)")

            # C 评审
            evaluation = agent_c.evaluate_reply(q, reply_text)
            score = evaluation.get("total_score", 0)
            passed = "✓" if evaluation.get("passed") else "✗"
            print(f"  [C] 得分: {score:.1f}/10 {passed} | {evaluation.get('feedback','')[:60]}")

            # B 从C的反馈中学习
            agent_b.learn_from_feedback(q, reply_text, evaluation)

            # 记录
            record = {
                "question_id": q.get("id"),
                "question": q,
                "reply": reply_text,
                "kb_sources": reply_result.get("kb_sources", []),
                "evaluation": evaluation,
                "b_lessons_count_at_time": len(agent_b.lessons),
            }
            replies.append({"question_id": q.get("id"), "reply": reply_text})
            evaluations.append(evaluation)

            # 每10题保存中间结果
            if q_num % 10 == 0:
                (self.session_dir / f"progress_{q_num:03d}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                b_sum = agent_b.get_learning_summary()
                print(f"  [进度] B已吸收 {b_sum['total_feedback_absorbed']} 条反馈，"
                      f"累计教训 {b_sum['lessons_count']} 条")

            time.sleep(0.5)  # 避免API限速

        # 保存完整数据
        (self.session_dir / "replies.json").write_text(
            json.dumps(replies, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.session_dir / "evaluations.json").write_text(
            json.dumps(evaluations, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.session_dir / "b_learning.json").write_text(
            json.dumps(agent_b.get_learning_summary(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ── Phase 4: C 生成完整报告 + 反哺系统 ───────────────────────────────
        print(f"\n[Phase 4] Agent C: 生成验证报告 + 反哺系统")
        b_learning = agent_b.get_learning_summary()
        report_md = agent_c.generate_session_report(
            questions, replies, evaluations, b_learning, self.session_dir
        )
        agent_c.apply_improvements_to_system(evaluations, b_learning)

        # ── 更新持久化状态 ────────────────────────────────────────────────────
        self.state["total_questions"] = self.state.get("total_questions", 0) + len(questions)
        # 将B的教训持久化（跨会话积累）
        existing_lessons = self.state.get("b_cumulative_lessons", [])
        new_lessons = agent_b.lessons
        merged = existing_lessons + [l for l in new_lessons if l not in existing_lessons]
        self.state["b_cumulative_lessons"] = merged[-50:]  # 保留最近50条
        self._save_state(self.state)

        # ── mem0 维护：时效衰减 + 低质量清理（保持记忆库健康）─────────────────
        if self.mem:
            try:
                decayed = self.mem.run_time_decay(AgentB.MEM_USER_B)
                removed = self.mem.cleanup_low_quality(AgentB.MEM_USER_B, threshold=0.2)
                self.mem.run_time_decay(AgentC.MEM_USER_C)
                self.mem.cleanup_low_quality(AgentC.MEM_USER_C, threshold=0.2)
                print(f"  [mem0] 时效衰减 {decayed} 条，清理低质量 {removed} 条")
            except Exception as e:
                print(f"  [mem0] 维护失败（不影响结果）: {e}")

        # ── Phase 4.5: 自动提炼风格规则 (Fix 2: 闭合训练回路) ──────────────
        print("\n[Phase 4.5] 重新提炼风格规则 (基于最新反馈 + 修改样本)...")
        try:
            from reply_trainer import ReplyTrainer
            _rt = ReplyTrainer()
            _llm_cfg = _load_llm_config()
            def _llm_fn(prompt):
                return svc.call_llm(
                    prompt, api_key=_llm_cfg.get("api_key", ""),
                    provider="openai" if _llm_cfg.get("base_url") else "gemini",
                    model_name=_llm_cfg.get("model_name", ""),
                    base_url=_llm_cfg.get("base_url", ""),
                )
            _rules = _rt.evolve_style_rules(_llm_fn)
            print(f"  [Phase 4.5] 风格规则已更新 ({len(_rules)} 字)")
        except Exception as e:
            print(f"  [Phase 4.5] 风格规则更新失败 (不影响主流程): {e}")

        # ── Phase 5: 推送训练产物到 QCL ──────────────────────────────────────
        print("\n[Phase 5] 推送训练产物到 QCL（style_rules + pattern_library + state）")
        pushed = push_artifacts_to_qcl()
        if pushed == 0:
            print("  ⚠ 推送失败或 QCL 不可达（本地结果已保存，可手动执行 sync）")

        # ── 完成 ──────────────────────────────────────────────────────────────
        pass_count = sum(1 for e in evaluations if e.get("passed"))
        avg = sum(e.get("total_score", 0) for e in evaluations) / max(len(evaluations), 1)
        print(f"\n{'='*60}")
        print(f"  训练完成！第 {self.state['session_count']} 期")
        print(f"  通过率: {pass_count}/{len(questions)} ({round(pass_count/len(questions)*100,1)}%)")
        print(f"  平均分: {avg:.1f}/10")
        print(f"  B累积教训: {len(agent_b.lessons)} 条")
        print(f"  报告: {self.session_dir}/report.md")
        print(f"{'='*60}\n")

        return {
            "session": self.state["session_count"],
            "n_questions": len(questions),
            "pass_rate": round(pass_count / len(questions), 3),
            "avg_score": round(avg, 1),
            "b_lessons": len(agent_b.lessons),
            "report_path": str(self.session_dir / "report.md"),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="智能回复优化训练器")
    parser.add_argument("--questions", "-n", type=int, default=100,
                        help="本次训练题目数（默认100）")
    parser.add_argument("--resume", action="store_true",
                        help="继续上次会话状态（B的积累教训会延续）")
    parser.add_argument("--status", action="store_true",
                        help="查看训练历史状态")
    parser.add_argument("--stop-hour", type=int, default=0,
                        help="到达该小时（0-23）时停止新题目，0=不限制（夜间调度用）")
    args = parser.parse_args()

    if args.status:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print("尚无训练记录。")
        metrics_file = TRAINING_DIR / "training_metrics.jsonl"
        if metrics_file.exists():
            lines = metrics_file.read_text(encoding="utf-8").strip().split("\n")
            print(f"\n历史会话记录（共 {len(lines)} 期）：")
            for line in lines[-5:]:
                try:
                    m = json.loads(line)
                    print(f"  {m['timestamp'][:16]} | {m['n']}题 | 均分{m['avg_score']} | 通过率{m['pass_rate']*100:.0f}% | B教训{m['b_lessons_count']}条")
                except Exception:
                    pass
        return

    loop = TrainingLoop(n_questions=args.questions, resume=args.resume,
                        stop_hour=args.stop_hour)
    loop.run()


if __name__ == "__main__":
    main()

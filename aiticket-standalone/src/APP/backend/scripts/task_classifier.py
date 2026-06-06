"""
task_classifier.py — 快速分类用户提问是否为「重型+非紧急」任务
读 stdin，输出 DEFERRED 或 IMMEDIATE（无 LLM 调用，纯关键词规则）
"""
import sys
import re

HEAVY_PATTERNS = [
    r"调研", r"分析", r"生成视频", r"录制", r"批量", r"大量", r"所有工单",
    r"竞品", r"PRD", r"需求库", r"演示脚本", r"演示视频", r"脚本.*视频",
    r"视频.*生成", r"全量", r"全部.*分析", r"整理.*报告", r"汇总",
    r"自动化.*测试", r"完整.*流程", r"系统.*优化", r"重构",
    r"提取.*知识", r"知识库.*更新", r"训练", r"爬取", r"探索.*网站",
    r"报告.*生成", r"周报", r"月报",
]

URGENT_PATTERNS = [
    r"出问题", r"报错", r"失败", r"紧急", r"刚才", r"怎么了", r"不对",
    r"没有.*收到", r"现在.*执行", r"立即", r"马上", r"快速.*查",
    r"检查.*状态", r"健康检查", r"ping", r"是否.*在线", r"还在跑",
    r"^(帮我?查|看下|查下|确认下)", r"重启", r"停止", r"kill",
]

NON_URGENT_BOOST = [
    r"帮我.*整理", r"帮我.*分析", r"帮我.*生成", r"帮我.*调研",
    r"以后", r"慢慢", r"不急", r"排好", r"计划.*做", r"准备.*材料",
    r"可以.*之后", r"安排.*时间",
]


def classify(prompt: str) -> str:
    p = prompt.strip()

    # 1. 明确紧急 → IMMEDIATE
    for pat in URGENT_PATTERNS:
        if re.search(pat, p):
            return "IMMEDIATE"

    # 2. 计算重量分
    heavy_score = sum(1 for pat in HEAVY_PATTERNS if re.search(pat, p))
    non_urgent_bonus = sum(1 for pat in NON_URGENT_BOOST if re.search(pat, p))

    # 3. 字数也是信号：超过 80 字的复杂请求偏重型
    length_bonus = 1 if len(p) > 80 else 0

    total = heavy_score + non_urgent_bonus + length_bonus

    # 重量分 >= 2 → DEFERRED
    if total >= 2:
        return "DEFERRED"
    # 仅一项重型关键词但无紧急 → 也 DEFERRED（单项重任务）
    if heavy_score >= 1 and non_urgent_bonus == 0 and len(p) > 30:
        return "DEFERRED"

    return "IMMEDIATE"


if __name__ == "__main__":
    prompt = sys.stdin.read()
    print(classify(prompt))

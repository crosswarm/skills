"""
对话记录器 - 自动记录项目对话到Nowledge Mem

使用方法:
1. 在对话结束时调用 log_current_conversation()
2. 或者在脚本中导入并调用 save_conversation_summary()
"""

import os
import sys
from datetime import datetime
from typing import Optional, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nowledge_mem_client import save_project_conversation, get_client


def save_conversation_summary(
    topic: str,
    summary: str,
    decisions: Optional[List[str]] = None,
    actions: Optional[List[str]] = None,
    tags: Optional[List[str]] = None
) -> Optional[str]:
    """
    保存对话摘要

    Args:
        topic: 对话主题
        summary: 对话摘要
        decisions: 决策列表
        actions: 行动项列表
        tags: 标签

    Returns:
        memory_id: 成功返回memory ID
    """
    content = f"""## 对话摘要

{summary}
"""

    if decisions:
        content += "\n## 重要决策\n"
        for i, decision in enumerate(decisions, 1):
            content += f"{i}. {decision}\n"

    if actions:
        content += "\n## 行动项\n"
        for i, action in enumerate(actions, 1):
            content += f"{i}. {action}\n"

    return save_project_conversation(
        topic=topic,
        content=content,
        tags=tags
    )


def log_current_conversation():
    """
    记录当前对话的便捷函数
    在对话结束时调用此函数
    """
    print("\n" + "="*50)
    print("📝 记录当前对话到Nowledge Mem...")

    # 获取用户输入的主题和摘要
    topic = input("请输入对话主题: ").strip()
    if not topic:
        topic = "项目对话"

    print("请输入对话摘要（多行输入，输入空行结束）:")
    lines = []
    while True:
        line = input()
        if line.strip() == "":
            break
        lines.append(line)
    summary = "\n".join(lines)

    # 可选的决策和行动项
    decisions = []
    actions = []

    print("请输入重要决策（每行一个，空行结束）:")
    while True:
        line = input()
        if line.strip() == "":
            break
        decisions.append(line)

    print("请输入行动项（每行一个，空行结束）:")
    while True:
        line = input()
        if line.strip() == "":
            break
        actions.append(line)

    # 保存
    memory_id = save_conversation_summary(
        topic=topic,
        summary=summary,
        decisions=decisions if decisions else None,
        actions=actions if actions else None,
        tags=["项目对话"]
    )

    if memory_id:
        print(f"✅ 对话已记录，Memory ID: {memory_id}")
    else:
        print("❌ 记录失败")

    print("="*50 + "\n")


# 记录历史对话的批量导入功能
def import_historical_conversations():
    """
    批量导入历史对话记录
    用于迁移之前的对话数据
    """
    historical_data = [
        {
            "topic": "月报功能开发",
            "summary": """完成了月报功能开发，包括：
- MonthlyAnalyzer/YoYAnalyzer/MonthlyReportGenerator三个核心类
- 前端周报/月报Tab切换
- 同比指标卡片
- 单元测试覆盖85%""",
            "decisions": ["采用混合数据源（CSV优先，周报聚合fallback）"],
            "actions": ["修复代码审查发现的6个问题"],
            "tags": ["月报功能", "开发完成"]
        },
        {
            "topic": "质量门禁系统建立",
            "summary": """建立了全流程质量门禁体系：
- 五位专业评委
- 五个必审环节
- 浏览器仿真测试
- 线上回归验证
- 飞书通知集成""",
            "decisions": [
                "评审委员会作用不是投票，而是专业发现问题",
                "每次需求/代码/测试前都需质量评审",
                "提交git前需浏览器仿真测试",
                "上线后需线上回归验证"
            ],
            "actions": [
                "制定质量标准规范",
                "制定开发流程规范",
                "写入长期记忆"
            ],
            "tags": ["质量门禁", "规范制定"]
        }
    ]

    print("📥 导入历史对话记录...")
    for item in historical_data:
        memory_id = save_conversation_summary(
            topic=item["topic"],
            summary=item["summary"],
            decisions=item.get("decisions"),
            actions=item.get("actions"),
            tags=item.get("tags", ["项目对话"])
        )
        if memory_id:
            print(f"✅ {item['topic']} -> {memory_id}")
        else:
            print(f"❌ {item['topic']} 失败")
    print("导入完成")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "import":
        import_historical_conversations()
    elif len(sys.argv) > 1 and sys.argv[1] == "log":
        log_current_conversation()
    else:
        print("""
对话记录器 - Nowledge Mem集成

用法:
  python conversation_logger.py log      # 交互式记录当前对话
  python conversation_logger.py import   # 批量导入历史对话
        """)

"""
质量审查委员会 - 飞书通知集成
在评审完成后自动发送报告到Aiticket会话
"""

import os
import sys
from datetime import datetime
from typing import Optional, List, Dict, Any

# 添加backend到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.feishu_notifier import FeishuNotifier, get_notifier


class QualityReviewCommitteeNotifier:
    """
    质量审查委员会通知器
    整合五位评委的评审结果，发送综合报告
    """
    
    def __init__(self):
        self.notifier = get_notifier()
        self.chat_id = "oc_72ef8553bb8b552435cd91b0fb1e86ab"  # Aiticket会话
    
    def notify_review_started(self, review_target: str, scope: str) -> bool:
        """通知评审开始"""
        message = f"""🚀 **质量审查启动**

**评审对象**: {review_target}
**评审范围**: {scope}
**启动时间**: {datetime.now().strftime("%Y-%m-%d %H:%M")}

---

五位评委已就位：
• 👔 市场竞争力评委
• 📋 需求质量评委  
• 🎨 用户体验评委
• 💻 代码质量评委
• 🧪 测试场景评委

---
⏳ 评审进行中，请稍候...
"""
        return self.notifier.send_message(message, self.chat_id)
    
    def notify_judge_completed(
        self,
        judge_name: str,
        review_target: str,
        rating: str,
        issues_count: int
    ) -> bool:
        """通知单个评委完成评审"""
        message = f"""✅ **{judge_name}完成评审**

**评审对象**: {review_target}
**评级**: {rating}
**发现问题**: {issues_count} 个

---
🔄 等待其他评委完成...
"""
        return self.notifier.send_message(message, self.chat_id)
    
    def notify_cross_review_started(self, review_target: str) -> bool:
        """通知交叉复审开始"""
        message = f"""🔄 **交叉复审阶段**

**评审对象**: {review_target}

评委之间正在相互验证发现的问题...
"""
        return self.notifier.send_message(message, self.chat_id)
    
    def notify_voting_started(self, review_target: str, p0_count: int, p1_count: int) -> bool:
        """通知投票阶段开始"""
        message = f"""🗳️ **委员会投票阶段**

**评审对象**: {review_target}
**待投票问题**: P0={p0_count}个, P1={p1_count}个

委员会正在对重要问题进行投票决策...
"""
        return self.notifier.send_message(message, self.chat_id)
    
    def notify_review_completed(
        self,
        review_target: str,
        overall_rating: str,
        p0_count: int,
        p1_count: int,
        p2_count: int,
        release_recommendation: str,
        fix_count: int,
        accept_risk_count: int,
        defer_count: int,
        report_path: str
    ) -> bool:
        """通知评审完成，发送最终报告"""
        rating_emoji = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🔴"}.get(overall_rating, "⚪")
        release_emoji = {"建议发布": "✅", "条件发布": "⚠️", "不建议发布": "🚫"}.get(release_recommendation, "❓")
        
        message = f"""📊 **质量审查委员会 - 最终报告**

**评审对象**: {review_target}
**完成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M")}

---

## 总体评级: {rating_emoji} {overall_rating}

## 问题统计
| 级别 | 数量 | 状态 |
|-----|-----|-----|
| P0-阻断级 | {p0_count} | 需立即处理 |
| P1-严重级 | {p1_count} | 建议修复 |
| P2-重要级 | {p2_count} | 可延后 |

## 发布建议: {release_emoji} {release_recommendation}

## 委员会决议
• 修复: {fix_count} 项
• 接受风险: {accept_risk_count} 项
• 延后处理: {defer_count} 项

---

📄 详细报告: `{report_path}`

---
💡 **下一步行动**:
1. 相关责任人查看详细报告
2. 按决议执行修复
3. 修复完成后申请复评

---
🤖 本消息由质量审查委员会系统自动发送
"""
        return self.notifier.send_message(message, self.chat_id)
    
    def notify_issue_fixed(
        self,
        issue_id: str,
        issue_description: str,
        fix_by: str,
        review_target: str
    ) -> bool:
        """通知问题已修复"""
        message = f"""✅ **问题已修复**

**评审对象**: {review_target}
**问题编号**: {issue_id}
**问题描述**: {issue_description}
**修复人**: {fix_by}
**修复时间**: {datetime.now().strftime("%Y-%m-%d %H:%M")}

---
🔄 将在下次复评中验证
"""
        return self.notifier.send_message(message, self.chat_id)


# 便捷函数
def get_committee_notifier() -> QualityReviewCommitteeNotifier:
    """获取委员会通知器实例"""
    return QualityReviewCommitteeNotifier()


if __name__ == "__main__":
    # 测试发送
    notifier = get_committee_notifier()
    
    # 测试：模拟评审完成通知
    print("发送质量审查报告到Aiticket会话...")
    
    success = notifier.notify_review_completed(
        review_target="月报功能",
        overall_rating="B",
        p0_count=0,
        p1_count=3,
        p2_count=5,
        release_recommendation="建议发布",
        fix_count=3,
        accept_risk_count=0,
        defer_count=5,
        report_path="conclusion/_local/quality-reviews/2026-02-21-月报功能-委员会报告.md"
    )
    
    if success:
        print("✅ 报告已发送到Aiticket会话")
    else:
        print("❌ 发送失败")

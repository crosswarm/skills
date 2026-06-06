"""
Nowledge Mem 客户端 - 对话记录服务
用于记录所有项目对话到本地记忆系统
"""

import urllib.request
import urllib.error
import json
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class NowledgeMemClient:
    """Nowledge Mem API 客户端"""

    def __init__(self, base_url: str = "http://127.0.0.1:14242"):
        self.base_url = base_url
        self.memories_endpoint = f"{base_url}/memories"

    def save_conversation(
        self,
        title: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        保存对话记录

        Args:
            title: 对话标题
            content: 对话内容
            metadata: 可选的元数据

        Returns:
            memory_id: 成功返回memory ID，失败返回None
        """
        try:
            data = {
                'title': title,
                'content': content,
            }

            if metadata:
                # Nowledge Mem 可能不直接支持自定义metadata字段
                # 将metadata附加到content中
                meta_str = json.dumps(metadata, ensure_ascii=False, indent=2)
                data['content'] = f"{content}\n\n**元数据**:\n```json\n{meta_str}\n```"

            req = urllib.request.Request(
                self.memories_endpoint,
                data=json.dumps(data).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json'
                },
                method='POST'
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
                memory_id = result.get('memory', {}).get('id')
                logger.info(f"✅ 对话已保存到Nowledge Mem: {memory_id}")
                return memory_id

        except urllib.error.HTTPError as e:
            logger.error(f"❌ HTTP错误 {e.code}: {e.reason}")
            return None
        except urllib.error.URLError as e:
            logger.error(f"❌ 连接错误: {e.reason}")
            return None
        except Exception as e:
            logger.error(f"❌ 保存对话失败: {e}")
            return None

    def get_all_memories(self) -> List[Dict]:
        """获取所有memories"""
        try:
            req = urllib.request.Request(self.memories_endpoint)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                return data.get('memories', [])
        except Exception as e:
            logger.error(f"❌ 获取memories失败: {e}")
            return []

    def search_by_title(self, keyword: str) -> List[Dict]:
        """根据标题关键词搜索"""
        memories = self.get_all_memories()
        return [m for m in memories if keyword.lower() in m.get('title', '').lower()]

    def delete_memory(self, memory_id: str) -> bool:
        """删除memory"""
        try:
            req = urllib.request.Request(
                f"{self.memories_endpoint}/{memory_id}",
                method='DELETE'
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status == 200
        except Exception as e:
            logger.error(f"❌ 删除memory失败: {e}")
            return False


# 全局实例
_client: Optional[NowledgeMemClient] = None


def get_client() -> NowledgeMemClient:
    """获取NowledgeMemClient单例"""
    global _client
    if _client is None:
        _client = NowledgeMemClient()
    return _client


def save_project_conversation(
    topic: str,
    content: str,
    session_date: Optional[str] = None,
    tags: Optional[List[str]] = None
) -> Optional[str]:
    """
    便捷函数：保存项目对话

    Args:
        topic: 对话主题
        content: 对话内容
        session_date: 会话日期，默认为今天
        tags: 标签列表

    Returns:
        memory_id: 成功返回memory ID
    """
    if session_date is None:
        session_date = datetime.now().strftime("%Y-%m-%d")

    title = f"AI工单项目 - {topic} ({session_date})"

    # 构建格式化内容
    formatted_content = f"""**项目**: AI工单系统
**日期**: {session_date}
**主题**: {topic}

**对话内容**:
{content}
"""

    if tags:
        formatted_content += f"\n**标签**: {', '.join(tags)}\n"

    metadata = {
        'project': 'AI工单系统',
        'date': session_date,
        'topic': topic,
        'tags': tags or []
    }

    return get_client().save_conversation(title, formatted_content, metadata)


if __name__ == "__main__":
    # 测试
    client = get_client()

    # 保存测试对话
    memory_id = save_project_conversation(
        topic="质量门禁系统建立",
        content="""用户: 设立质量审查委员会
AI: 已建立五位评委体系，包括市场竞争力、需求质量、用户体验、代码质量、测试场景评委。
用户: 需要飞书通知
AI: 已集成飞书通知服务，可以发送评审报告到Aiticket会话。""",
        tags=["质量门禁", "飞书通知"]
    )

    if memory_id:
        print(f"✅ 测试成功，Memory ID: {memory_id}")
    else:
        print("❌ 测试失败")

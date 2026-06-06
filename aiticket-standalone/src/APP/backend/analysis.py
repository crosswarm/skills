import os
try:
    import pandas as pd
    _PANDAS_AVAILABLE = True
except ImportError:
    pd = None
    _PANDAS_AVAILABLE = False
import re
from datetime import datetime
from typing import Dict, List, Set, Optional


CREWLIST_PATH = "../../_local/notes/crewlist.md"
SRC_DIR = "../../src"
CONCLUSION_DIR = "../../conclusion"
TOPIC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "topic.md")

# Jira project_key → 产品中文名（用于转出工单 JQL 和报告标题）
PROJECT_DISPLAY_NAMES: Dict[str, str] = {
    "MYPROJECT": "云平台-流程中心",
}


class TopicParser:
    """Parse topic.md to extract topic hierarchy with L3 leaf topic support"""
    def __init__(self, path: str, project_key: str = "MYPROJECT"):
        self.path = path
        self.project_key = project_key
        self.topics: List[Dict] = []  # 完整主题树
        self.leaf_topics: List[Dict] = []  # 末级主题列表
        self.parse()

    def _extract_project_lines(self, lines: List[str]) -> List[str]:
        """Extract only the lines belonging to self.project_key section."""
        section_header = f"## [PROJECT:{self.project_key}]"
        in_section = False
        result = []
        for line in lines:
            stripped = line.strip()
            if stripped == section_header:
                in_section = True
                continue
            if stripped.startswith("## [PROJECT:") and in_section:
                break
            if in_section:
                result.append(line)
        return result

    def parse(self):
        if not os.path.exists(self.path):
            print(f"Warning: {self.path} not found. Using default project-based categorization.")
            return

        with open(self.path, 'r', encoding='utf-8') as f:
            content = f.read()

        current_l1 = None
        current_l2 = None
        current_l3 = None
        l2_has_l3_children = set()  # 记录哪些L2有L3子节点

        lines = content.split('\n')
        # Multi-project support: filter to project section if headers exist
        if any(l.strip().startswith("## [PROJECT:") for l in lines):
            lines = self._extract_project_lines(lines)

        for line in lines:
            stripped = line.strip()

            # Skip non-list items and comments
            if not stripped.startswith('-') or stripped.startswith('//'):
                continue

            # Count indent level (4 spaces = 1 level)
            indent = len(line) - len(line.lstrip())
            level = indent // 4

            # Extract topic name (remove leading -)
            topic_name = stripped.lstrip('- ').strip()

            if level == 0:
                # L1主题
                current_l1 = topic_name
                current_l2 = None
                current_l3 = None
                self.topics.append({
                    "level": 1,
                    "level1": current_l1,
                    "level2": None,
                    "level3": None,
                    "full_path": current_l1,
                    "is_leaf": False
                })
            elif level == 1:
                # L2主题
                current_l2 = topic_name
                current_l3 = None
                self.topics.append({
                    "level": 2,
                    "level1": current_l1,
                    "level2": current_l2,
                    "level3": None,
                    "full_path": f"{current_l1}/{current_l2}",
                    "is_leaf": False  # 暂定为非末级，后续检查
                })
            elif level >= 2:
                # L3+ 都是末级主题
                current_l3 = topic_name
                topic_entry = {
                    "level": 3,
                    "level1": current_l1,
                    "level2": current_l2,
                    "level3": current_l3,
                    "full_path": f"{current_l1}/{current_l2}/{current_l3}",
                    "is_leaf": True
                }
                self.topics.append(topic_entry)
                self.leaf_topics.append(topic_entry)
                if current_l2:
                    l2_has_l3_children.add(f"{current_l1}/{current_l2}")

        # 标记没有L3子节点的L2为末级
        self._mark_l2_as_leaf(l2_has_l3_children)

        print(f"[TopicParser:{self.project_key}] 解析完成: {len(self.topics)} 个主题, {len(self.leaf_topics)} 个末级主题")

    def _mark_l2_as_leaf(self, l2_with_l3_children: Set[str]):
        """标记没有L3子节点的L2主题为末级"""
        for t in self.topics:
            if t["level"] == 2 and t["full_path"] not in l2_with_l3_children:
                t["is_leaf"] = True
                self.leaf_topics.append(t)

    def get_leaf_topics(self) -> List[Dict]:
        """获取所有末级主题"""
        return self.leaf_topics

    def get_unique_l1l2_topics(self) -> List[Dict]:
        """Return unique L1 and L1/L2 combinations for file generation"""
        seen = set()
        result = []
        for t in self.topics:
            key = t["full_path"]
            if key not in seen:
                seen.add(key)
                result.append(t)
        return result

    def classify_ticket_with_leaf_priority(self, summary: str, solution: str,
                                            min_cluster_size: int = 5) -> Optional[Dict]:
        """
        末级主题优先聚类策略

        Args:
            summary: 工单摘要
            solution: 解决方案
            min_cluster_size: 最小聚类数量阈值，小于此值向上聚合

        Returns:
            匹配的主题字典
        """
        text = f"{summary} {solution}".lower()

        # 1. 先尝试匹配末级主题（L3或无L3的L2）
        for topic in self.leaf_topics:
            if self._match_topic(topic, text):
                return topic

        # 2. 末级主题无匹配，尝试L2
        for topic in self.topics:
            if topic["level"] == 2 and self._match_topic(topic, text):
                return topic

        # 3. 最后尝试L1
        for topic in self.topics:
            if topic["level"] == 1 and self._match_topic(topic, text):
                return topic

        return None

    def _match_topic(self, topic: Dict, text: str) -> bool:
        """匹配主题关键词"""
        # L3主题匹配
        if topic.get("level3"):
            if topic["level3"].lower() in text:
                return True
        # L2主题匹配
        if topic.get("level2"):
            if topic["level2"].lower() in text:
                return True
        # L1主题匹配
        if topic.get("level1"):
            if topic["level1"].lower() in text:
                return True
        return False

    def classify_ticket(self, summary: str, solution: str) -> Optional[Dict]:
        """Try to classify a ticket into a topic based on keywords. On miss, enqueue for review."""
        result = self.classify_ticket_with_leaf_priority(summary, solution)
        if result is None and self.project_key and self.project_key != "MYPROJECT":
            self._enqueue_review(summary)
        return result

    def _enqueue_review(self, summary: str) -> None:
        """Write unclassified ticket summary to topic_review_queue for human review."""
        queue_dir = os.path.join(os.path.dirname(TOPIC_PATH), "topic_review_queue")
        os.makedirs(queue_dir, exist_ok=True)
        queue_file = os.path.join(queue_dir, f"{self.project_key}.md")
        line = f"- {summary.strip()}\n"
        try:
            if os.path.exists(queue_file):
                with open(queue_file, "r", encoding="utf-8") as f:
                    if line in f.read():
                        return
            with open(queue_file, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _match_keywords(self, topic_name: str, text: str) -> bool:
        """Match common keyword variations"""
        # Simple Chinese keyword matching
        keywords = topic_name.lower().replace('、', ' ').split()
        for kw in keywords:
            if len(kw) >= 2 and kw in text:
                return True
        return False

    def build_topic_prompt(self) -> str:
        """基于topic.md动态构建主题提示，用于AI分析"""
        if not self.leaf_topics:
            return "主题结构参考：工作流产品结构（流程引擎、字段权限、工作流设计器等）"

        # 按L1分组
        grouped = {}
        for t in self.leaf_topics:
            l1 = t["level1"]
            if l1 not in grouped:
                grouped[l1] = {}
            l2 = t.get("level2")
            if l2:
                if l2 not in grouped[l1]:
                    grouped[l1][l2] = []
                l3 = t.get("level3")
                if l3:
                    grouped[l1][l2].append(l3)

        prompt = "主题结构参考（优先使用末级主题）：\n"
        for l1, l2_dict in grouped.items():
            l2_strs = []
            for l2, l3_list in l2_dict.items():
                if l3_list:
                    l2_strs.append(f"{l2}（{', '.join(l3_list)}）")
                else:
                    l2_strs.append(l2)
            if l2_strs:
                prompt += f"- {l1}（{', '.join(l2_strs)}）\n"
            else:
                prompt += f"- {l1}\n"

        return prompt


class CrewListParser:
    def __init__(self, path: str):
        self.path = path
        self.product_managers = set()
        self.developers = set()
        self.testers = set()
        self.name_to_role = {}  # 支持中文名和username映射到角色
        self.parse()

    def parse(self):
        if not os.path.exists(self.path):
            print(f"Warning: {self.path} not found.")
            return

        with open(self.path, 'r', encoding='utf-8') as f:
            content = f.read()

        current_section = None

        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if "## 产品经理" in line:
                current_section = "PM"
                continue
            elif "## 开发" in line:
                current_section = "DEV"
                continue
            elif "## 测试" in line:
                current_section = "QA"
                continue

            if line.startswith("-"):
                clean_line = line.lstrip("- ").strip()
                parts = re.split(r'[,，]', clean_line)
                if parts:
                    username = parts[0].strip()
                    chinese_name = parts[1].strip() if len(parts) > 1 else ""

                    # 存储username和chinese_name到角色的映射
                    if current_section == "PM":
                        self.product_managers.add(username)
                        self.name_to_role[username] = "产品经理"
                        if chinese_name:
                            self.name_to_role[chinese_name] = "产品经理"
                    elif current_section == "DEV":
                        self.developers.add(username)
                        self.name_to_role[username] = "开发"
                        if chinese_name:
                            self.name_to_role[chinese_name] = "开发"
                    elif current_section == "QA":
                        self.testers.add(username)
                        self.name_to_role[username] = "测试"
                        if chinese_name:
                            self.name_to_role[chinese_name] = "测试"

    def get_role(self, name: str) -> str:
        # 支持中文名和username查找
        if name in self.name_to_role:
            return self.name_to_role[name]
        if name in self.product_managers:
            return "产品经理"
        if name in self.developers:
            return "开发"
        if name in self.testers:
            return "测试"
        return "其他"


class TicketAnalyzer:
    def __init__(self):
        self.crew_parser = CrewListParser(CREWLIST_PATH)
        self.topic_parser = TopicParser(TOPIC_PATH)
        self.topics_dir = os.path.join(CONCLUSION_DIR, "Topics")
        os.makedirs(self.topics_dir, exist_ok=True)

    def run(self):
        # Data buffer: topic_path -> list of ticket_data
        topic_buffer: Dict[str, List[Dict]] = {}
        all_tickets: List[Dict] = []
        
        # 1. Iterate source files
        if not os.path.exists(SRC_DIR):
            return
            
        csv_files = [f for f in os.listdir(SRC_DIR) if f.endswith(".csv")]
        print(f"Found {len(csv_files)} CSV files to process.")
        
        for filename in csv_files:
            self.process_file(os.path.join(SRC_DIR, filename), topic_buffer, all_tickets)

        # 2. Write Topic Files (by topic structure)
        print("Writing Topic Files...")
        for topic_path, tickets in topic_buffer.items():
            self.write_topic_file(topic_path, tickets)

        # 3. Write Index File
        print("Writing Index File...")
        self.write_index_file(all_tickets)

    def process_file(self, filepath: str, topic_buffer: Dict[str, List[Dict]], all_tickets: List[Dict]):
        try:
            df = pd.read_csv(filepath)
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
            return

        for _, row in df.iterrows():
            if '问题关键字' not in row or pd.isna(row['问题关键字']):
                continue
                
            key = str(row['问题关键字'])
            summary = str(row['概要']) if '概要' in df.columns else ""
            
            solution = row['自定义字段(解决方案)'] if '自定义字段(解决方案)' in df.columns and pd.notna(row['自定义字段(解决方案)']) else "无"
            confirmed_type = row['自定义字段(研发确认问题类型)'] if '自定义字段(研发确认问题类型)' in df.columns and pd.notna(row['自定义字段(研发确认问题类型)']) else "未知"
            assignee = row['经办人'] if '经办人' in df.columns and pd.notna(row['经办人']) else "Unknown"
            created_date = str(row['创建日期']) if '创建日期' in df.columns else ""
            status = str(row['状态']) if '状态' in df.columns else ""
            raw_project = str(row['项目名称']) if '项目名称' in df.columns else "未分类"
            
            # Classify by topic.md
            topic_info = self.topic_parser.classify_ticket(summary, str(solution))
            
            if topic_info:
                topic_path = topic_info["full_path"]
            else:
                # Fallback to project-based classification
                # Fallback to project-based classification
                safe_project = re.sub(r'[\\/*?:\"<>|]', '_', raw_project).strip() or 'Unknown'
                topic_path = f"其他/{safe_project}"

            is_team_resolved = (status == "支持确认完成" and raw_project == "云平台-流程中心")
            role = self.crew_parser.get_role(assignee)

            ticket_data = {
                "key": key,
                "summary": summary,
                "solution": solution,
                "confirmed_type": confirmed_type,
                "assignee": assignee,
                "role": role,
                "created_date": created_date,
                "is_team_resolved": is_team_resolved,
                "project_name": raw_project,
                "topic_path": topic_path
            }

            if topic_path not in topic_buffer:
                topic_buffer[topic_path] = []
            
            topic_buffer[topic_path].append(ticket_data)
            all_tickets.append(ticket_data)

    def write_topic_file(self, topic_path: str, tickets: List[Dict]):
        # Convert topic_path like "工作流产品结构/流程引擎" to filename
        safe_filename = topic_path.replace("/", "_").replace(" ", "_")
        safe_filename = re.sub(r'[\\/*?:"<>|]', '_', safe_filename)
        filename = f"{safe_filename}.md"
        path = os.path.join(self.topics_dir, filename)
        
        content = f"# {topic_path} 问题汇总\n\n"
        content += f"更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        content += f"包含工单数: {len(tickets)}\n\n"
        
        for t in tickets:
            content += f"## [{t['key']}] {t['summary']}\n"
            content += f"- **日期**: {t['created_date']} | **经办**: {t['assignee']} ({t['role']})\n"
            content += f"- **类型**: {t['confirmed_type']} | **状态**: {'内部闭环' if t['is_team_resolved'] else '转交/外部'}\n\n"
            content += f"### 解决方案\n{t['solution']}\n\n"
            content += "---\n\n"
            
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    def write_index_file(self, all_tickets: List[Dict]):
        path = os.path.join(CONCLUSION_DIR, "index.md")
        
        content = "# 工单索引 (Index)\n\n"
        content += "| 问题关键字 | 概要 | 所属主题 | 经办人 |\n"
        content += "| --- | --- | --- | --- |\n"

        for t in all_tickets:
            safe_filename = t['topic_path'].replace("/", "_").replace(" ", "_")
            safe_filename = re.sub(r'[\\/*?:"<>|]', '_', safe_filename)
            topic_file = f"Topics/{safe_filename}.md"
            
            content += f"| {t['key']} | {t['summary']} | [{t['topic_path']}]({topic_file}) | {t['assignee']} |\n"
            
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)


if __name__ == "__main__":
    analyzer = TicketAnalyzer()
    analyzer.run()

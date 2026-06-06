from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import json
from jira_service import jira_service, JiraIssue
from llm_service import LLMService
from search import SearchEngine
import os

AI_CACHE_FILE = "board_ai_cache.json"

class BoardService:
    def __init__(self, llm_service: LLMService, search_engine: SearchEngine):
        self.llm_service = llm_service
        self.search_engine = search_engine
        self.ai_cache = self._load_cache()

    def get_board_data(self, project_key: str = "MYPROJECT", assignee: str = "currentUser()") -> Dict[str, List[Dict]]:
        """
        Get issues organized by board columns (Today, Tomorrow, Week, Overdue, etc.)
        """
        # Search issues
        jql = f"project = {project_key} AND resolution = Unresolved AND assignee in ({assignee}) ORDER BY due ASC, updated DESC"
        issues_data = jira_service.search_issues(jql)
        
        # Parse issues using the regex parser in JiraService
        issues = jira_service.parse_issue_table_response(issues_data)
        
        # Fallback to mock only if explicitly enabled or for debugging (commented out for now)
        # if not issues:
        #    issues = self._parse_issues(issues_data)
        
        # Organize into columns
        columns = {
            "overdue": [],
            "today": [],
            "tomorrow": [],
            "week": [],
            "future": [],
            "nodate": []
        }
        
        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)
        next_week = today + timedelta(days=7)
        
        for issue in issues:
            if not issue.due_date:
                columns["nodate"].append(asdict(issue))
                continue
                
            try:
                # Handle both "YYYY-MM-DD" and "YYYY-MM-DD HH:MM:SS" formats
                date_str = issue.due_date.strip()
                if ' ' in date_str:
                    due = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").date()
                else:
                    due = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                columns["nodate"].append(asdict(issue))
                continue
            
            if due < today:
                columns["overdue"].append(asdict(issue))
            elif due == today:
                columns["today"].append(asdict(issue))
            elif due == tomorrow:
                columns["tomorrow"].append(asdict(issue))
            elif due <= next_week:
                columns["week"].append(asdict(issue))
            else:
                columns["future"].append(asdict(issue))
                
        # Merge AI data from cache
        for col_name in columns:
            for issue_dict in columns[col_name]:
                key = issue_dict["key"]
                if key in self.ai_cache:
                    issue_dict["ai_analysis"] = self.ai_cache[key]
                else:
                    issue_dict["ai_analysis"] = None # Front-end can show "Analyzing..."
        
        return columns

    def _load_cache(self) -> Dict[str, Any]:
        """Load AI analysis cache from file"""
        if os.path.exists(AI_CACHE_FILE):
            try:
                with open(AI_CACHE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading AI cache: {e}")
        return {}

    def _save_cache(self):
        """Save AI analysis cache to file"""
        try:
            with open(AI_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.ai_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving AI cache: {e}")

    def trigger_board_analysis(self):
        """
        Background task to analyze issues on the board that are missing AI analysis.
        This ignores issues already in cache.
        """
        print("Starting background board analysis...")
        # Get all current issues
        jql = "project = MYPROJECT AND resolution = Unresolved ORDER BY updated DESC"
        issues_data = jira_service.search_issues(jql)
        issues = jira_service.parse_issue_table_response(issues_data)
        
        updates_made = False
        for issue in issues:
            if issue.key not in self.ai_cache:
                print(f"Analyzing {issue.key}...")
                analysis = self.analyze_issue(issue)
                self.ai_cache[issue.key] = analysis
                updates_made = True
                
        if updates_made:
            self._save_cache()
            print("Board analysis updated and saved.")
        else:
            print("No new issues to analyze.")

    def analyze_issue(self, issue: JiraIssue) -> Dict[str, Any]:
        """
        Generate AI analysis for a single issue.
        """
        # 1. Search similar issues
        query = f"{issue.summary} {issue.description}"
        search_results = self.search_engine.search(query)
        similar_issues = []
        if search_results.get("results"):
            for res in search_results["results"][:3]: # Top 3
                similar_issues.append({
                    "key": res.get("key"),
                    "summary": res.get("display_summary"),
                    "score": res.get("score")
                })

        # 2. Construct LLM Prompt
        context_text = ""
        for sim in similar_issues:
            context_text += f"- {sim['key']}: {sim['summary']} (Similarity: {sim['score']:.2f})\n"
            
        prompt = f"""
        Analyze the following Jira issue and provide recommendations.
        
        Issue: [{issue.key}] {issue.summary}
        Description: {issue.description}
        Reporter: {issue.reporter}
        Status: {issue.status}
        
        Similar Historical Issues:
        {context_text}
        
        Please provide a JSON response with the following fields:
        1. recommended_team: (string) The team that should handle this (e.g., "云平台-流程中心", "财务组", etc.)
        2. recommended_role: (string) The role (e.g., "开发人员", "产品经理")
        3. functionality_impact: (string) Brief description of functionality impact.
        4. solution_suggestion: (string) A concise suggestion on how to solve it based on similar issues.
        
        Return ONLY valid JSON.
        """
        
        # 3. Call LLM
        try:
            response_text = self.llm_service.call_llm(prompt, model_name="gemini-2.0-flash")
            # Parse JSON from response
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                data["similar_issues"] = similar_issues
                return data
        except Exception as e:
            print(f"Error analyzing issue {issue.key}: {e}")
        
        # Fallback if LLM fails
        return {
            "recommended_team": "待分析",
            "recommended_role": "待分析",
            "functionality_impact": "未知",
            "solution_suggestion": "自动分析失败，请人工检查。",
            "similar_issues": similar_issues
        }

def asdict(obj):
    return {k: v for k, v in obj.__dict__.items()}

# Singleton
# board_service = BoardService(llm_service_instance)

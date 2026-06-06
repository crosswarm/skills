"""
测试智能看板查询条件构建

测试用例清单:
- TC001: 默认参数查询 - 返回当前用户未解决工单
- TC002: 按创建时间范围查询 - JQL 包含 created 条件
- TC003: 按标签查询 - JQL 包含 labels 条件
- TC004: 按研发问题类型查询 - JQL 包含 cf[10729] 条件
- TC005: 按客户问题类型查询 - JQL 包含 cf[10402] 条件
- TC006: 按解决方式查询 - JQL 包含 cf[10906] 条件
- TC007: 多条件组合查询 - JQL 正确拼接所有条件
- TC008: 空参数查询 - 忽略空参数，使用默认条件
"""

import pytest
import sys
import os
from unittest.mock import MagicMock
from pathlib import Path
from datetime import datetime, timedelta

# 添加backend目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBoardJQLBuilder:
    """测试 JQL 构建逻辑"""

    def test_tc001_default_parameters(self):
        """
        TC001: 默认参数查询
        预期: JQL 包含默认条件 project=MYPROJECT, resolution=Unresolved, assignee=currentUser()
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)  # 不调用 __init__
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="currentUser()"
        )

        assert "project = MYPROJECT" in jql
        assert "resolution = Unresolved" in jql
        assert "assignee in (currentUser())" in jql
        assert "ORDER BY due ASC, updated DESC" in jql

    def test_tc002_created_time_range(self):
        """
        TC002: 按创建时间范围查询
        预期: JQL 包含 created >= 和 created <= 条件
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="currentUser()",
            created_start="2024-01-01",
            created_end="2024-12-31"
        )

        assert 'created >= "2024-01-01"' in jql
        assert 'created <= "2024-12-31"' in jql

    def test_tc003_labels_filter(self):
        """
        TC003: 按标签查询
        预期: JQL 包含 labels = "xxx" 条件
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="currentUser()",
            labels="紧急"
        )

        assert 'labels = "紧急"' in jql

    def test_tc004_dev_issue_type_filter(self):
        """
        TC004: 按研发问题类型查询 (customfield_10729)
        预期: JQL 包含 cf[10729] = "xxx" 条件
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="currentUser()",
            dev_issue_type="BUG"
        )

        assert 'cf[10729] = "BUG"' in jql

    def test_tc005_customer_issue_type_filter(self):
        """
        TC005: 按客户问题类型查询 (customfield_10402)
        预期: JQL 包含 cf[10402] = "xxx" 条件
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="currentUser()",
            customer_issue_type="功能咨询"
        )

        assert 'cf[10402] = "功能咨询"' in jql

    def test_tc006_resolution_method_filter(self):
        """
        TC006: 按解决方式查询 (customfield_10906)
        预期: JQL 包含 cf[10906] = "xxx" 条件
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="currentUser()",
            resolution_method="远程指导"
        )

        assert 'cf[10906] = "远程指导"' in jql

    def test_tc007_combined_filters(self):
        """
        TC007: 多条件组合查询
        预期: JQL 正确拼接所有条件
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="currentUser()",
            created_start="2024-01-01",
            created_end="2024-12-31",
            labels="紧急",
            dev_issue_type="BUG",
            customer_issue_type="功能咨询",
            resolution_method="远程指导"
        )

        assert "project = MYPROJECT" in jql
        assert "resolution = Unresolved" in jql
        assert "assignee in (currentUser())" in jql
        assert 'created >= "2024-01-01"' in jql
        assert 'created <= "2024-12-31"' in jql
        assert 'labels = "紧急"' in jql
        assert 'cf[10729] = "BUG"' in jql
        assert 'cf[10402] = "功能咨询"' in jql
        assert 'cf[10906] = "远程指导"' in jql
        assert "ORDER BY due ASC, updated DESC" in jql

        # 验证条件之间用 AND 连接
        assert jql.count(" AND ") >= 8  # 至少8个 AND 连接

    def test_tc008_empty_parameters(self):
        """
        TC008: 空参数查询
        预期: 空参数被忽略，JQL 只包含默认条件
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="currentUser()",
            created_start="",
            created_end="",
            labels="",
            dev_issue_type="",
            customer_issue_type="",
            resolution_method=""
        )

        assert "project = MYPROJECT" in jql
        assert "resolution = Unresolved" in jql
        assert "assignee in (currentUser())" in jql

        # 空参数不应该出现在 JQL 中
        assert "created >=" not in jql
        assert "created <=" not in jql
        assert "labels =" not in jql
        assert "cf[10729]" not in jql
        assert "cf[10402]" not in jql
        assert "cf[10906]" not in jql

    def test_tc009_assignee_all(self):
        """
        TC009: 经办人选择"全部"
        预期: 不添加 assignee 条件
        """
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        jql = service._build_jql(
            project_key="MYPROJECT",
            assignee="ALL"
        )

        assert "project = MYPROJECT" in jql
        assert "resolution = Unresolved" in jql
        assert "assignee" not in jql


class TestBoardAPI:
    """测试 API 接口参数传递"""

    def test_api_parameter_parsing(self):
        """
        测试 API 参数是否能正确解析
        """
        # 模拟 FastAPI Query 参数
        params = {
            "project_key": "MYPROJECT",
            "assignee": "currentUser()",
            "created_start": "2024-01-01",
            "created_end": "2024-12-31",
            "labels": "紧急",
            "dev_issue_type": "BUG",
            "customer_issue_type": "",
            "resolution_method": ""
        }

        # 验证参数存在
        assert params["project_key"] == "MYPROJECT"
        assert params["assignee"] == "currentUser()"
        assert params["created_start"] == "2024-01-01"
        assert params["created_end"] == "2024-12-31"
        assert params["labels"] == "紧急"
        assert params["dev_issue_type"] == "BUG"
        assert params["customer_issue_type"] == ""
        assert params["resolution_method"] == ""

    def test_api_board_response_includes_fetch_metadata(self, monkeypatch):
        """GET /api/board 应返回数据来源元信息，便于前端识别缓存快照"""
        import main

        mock_board_service = MagicMock()
        mock_board_service.get_board_data.return_value = {"today": []}
        mock_board_service.get_stats.return_value = {"total": 0}
        mock_board_service.get_last_board_fetch_meta.return_value = {
            "data_source": "local_cache",
            "cache_timestamp": "2026-03-17 09:45:59",
            "jira_error": "jira_direct: Read timed out",
        }
        monkeypatch.setattr(main, "board_service", mock_board_service)

        response = main.get_board_data()

        assert response["data_source"] == "local_cache"
        assert response["cache_timestamp"] == "2026-03-17 09:45:59"
        assert response["jira_error"] == "jira_direct: Read timed out"

    def test_api_board_diagnose_reports_server_unified_strategy(self, monkeypatch):
        """/api/board/diagnose 应暴露服务端统一取数策略与 FRP 端口约定。"""
        import main

        jira_svc = MagicMock()
        jira_svc.diagnose_connection.return_value = {"status": "error", "message": "timeout"}
        jira_svc.get_cache_info.return_value = {"exists": False}
        cache_service = MagicMock()
        cache_service.config = {
            "proxy_nodes": [
                {
                    "name": "mini",
                    "base_url": "http://localhost:8080/jira_proxy",
                    "enabled": True,
                    "weight": 1,
                }
            ]
        }
        cache_service.get_metrics.return_value = {
            "status": "success",
            "data": {"nodes": [{"name": "mini", "healthy": True}]},
        }
        monkeypatch.setattr(main, "jira_svc", jira_svc)
        monkeypatch.setattr(main, "jira_cache_service", cache_service)
        monkeypatch.setattr(
            main,
            "FRP_EXPECTED_PORTS",
            {
                "bind_port": 7000,
                "vhost_http_port": 8080,
                "dashboard_port": 7500,
                "mini_proxy_port": 5001,
            },
        )
        monkeypatch.setattr(main, "DEFAULT_PROXY_BASE_URL", "http://localhost:8080/jira_proxy")
        mock_board_service = MagicMock()
        mock_board_service.get_fetch_strategy_state.return_value = {
            "prefer_proxy": True,
            "effective_fetch_order": ["jira_proxy", "jira_direct", "local_cache"],
            "jira_direct_cooldown_active": False,
            "jira_direct_cooldown_remaining_seconds": 0,
            "jira_direct_last_error": None,
        }
        monkeypatch.setattr(main, "board_service", mock_board_service)
        monkeypatch.setenv("CLIENT_NETWORK_BYPASS_ENABLED", "false")

        response = main.diagnose_board_datasources()

        assert response["fetch_strategy"] == "server_unified"
        assert response["fetch_order"] == ["jira_direct", "jira_proxy", "local_cache"]
        assert response["board_fetch_state"]["effective_fetch_order"] == ["jira_proxy", "jira_direct", "local_cache"]
        assert response["board_fetch_state"]["prefer_proxy"] is True
        assert response["client_network_bypass_enabled"] is False
        assert response["frp_expected_ports"]["mini_proxy_port"] == 5001
        assert response["mini_proxy"]["expected_base_url"] == "http://localhost:8080/jira_proxy"
        assert response["env"]["PROXY_NODES"][0]["base_url"] == "http://localhost:8080/jira_proxy"
        assert response["mini_proxy"]["metrics"]["data"]["nodes"][0]["healthy"] is True

    def test_network_config_files_use_standard_proxy_ports(self):
        """仓库内网络配置应统一到 mini:5001 和 qcl:8080/jira_proxy。"""
        repo_root = Path(__file__).resolve().parents[3]
        host1_config = (repo_root / "APP/backend/config/network_config_host1.yaml").read_text(encoding="utf-8")
        qcl_config = (repo_root / "APP/backend/config/network_config_qcl.yaml").read_text(encoding="utf-8")
        frpc_config = (repo_root / "APP/backend/config/frpc.ini").read_text(encoding="utf-8")

        assert "local_proxy_port: 5001" in host1_config
        assert "port: 5001" in host1_config
        assert 'base_url: "http://localhost:8080/jira_proxy"' in qcl_config
        assert "local_port = 5001" in frpc_config


class TestBoardFetchSource:
    """测试看板数据来源选择与元信息记录"""

    @staticmethod
    def _build_stub_service():
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        service.vector_store = MagicMock()
        service.vector_store.get_cached_analysis.return_value = None
        service.analysis_status = {}
        service.jira_cache_service = None
        service._load_board_config = lambda: {}
        service._build_jql = lambda **_: "project = MYPROJECT"
        service._auto_submit_analysis = lambda issues: None
        service._organize_columns = lambda issues: {
            "today": [
                {
                    "key": issues[0].key,
                    "summary": issues[0].summary,
                    "status": issues[0].status,
                    "description": issues[0].description,
                }
            ]
        }
        return service

    @staticmethod
    def _fake_issue():
        from jira_service import JiraIssue

        return JiraIssue(
            key="MYPROJECT-1",
            summary="测试工单",
            status="处理中",
            assignee="张三",
            reporter="李四",
            created="2026-03-18 10:00:00",
            updated="2026-03-18 10:05:00",
            due_date="2026-03-18",
            priority="High",
            issue_type="Support",
            project_name="流程中心",
            description="描述",
        )

    def test_get_board_data_uses_proxy_after_direct_jira_failure(self, monkeypatch):
        """QCL 直连 Jira 失败后，应回退到 mini 代理拿最新数据。"""
        import board_service_chroma

        service = self._build_stub_service()
        issue = self._fake_issue()
        service.jira_cache_service = MagicMock()
        service.jira_cache_service.search_issues.return_value = {
            "status": "success",
            "data": {"issues": [{"key": issue.key, "fields": {}}]},
        }

        parse_mock = MagicMock(return_value=[issue])
        save_mock = MagicMock()
        monkeypatch.setattr(board_service_chroma.jira_service, "parse_search_response", parse_mock)
        monkeypatch.setattr(board_service_chroma.jira_service, "save_board_cache", save_mock)
        monkeypatch.setattr(
            board_service_chroma.jira_service,
            "search_issues_rest_api",
            lambda jql: {"error": "ConnectTimeout"},
        )
        monkeypatch.setattr(board_service_chroma, "load_file_cached_analysis", lambda *args, **kwargs: None)

        columns = service.get_board_data()

        assert columns["today"][0]["key"] == "MYPROJECT-1"
        assert service.get_last_board_fetch_meta()["data_source"] == "jira_proxy"
        assert "jira_direct" in service.get_last_board_fetch_meta()["jira_error"]
        parse_mock.assert_called_once_with({"issues": [{"key": issue.key, "fields": {}}]})
        save_mock.assert_called_once_with([issue])

    def test_get_board_data_falls_back_to_local_cache_with_error_metadata(self, monkeypatch):
        """代理层和直连 Jira 都失败时，应回退本地缓存并记录错误上下文"""
        import board_service_chroma

        service = self._build_stub_service()
        issue = self._fake_issue()
        service.jira_cache_service = MagicMock()
        service.jira_cache_service.search_issues.return_value = {
            "status": "error",
            "code": "PROXY_ERROR",
            "message": "代理节点响应失败",
        }

        monkeypatch.setattr(
            board_service_chroma.jira_service,
            "search_issues_rest_api",
            lambda jql: {"error": "Read timed out"},
        )
        monkeypatch.setattr(
            board_service_chroma.jira_service,
            "get_cache_info",
            lambda: {"exists": True, "timestamp": "2026-03-17 09:45:59", "count": 1},
        )
        monkeypatch.setattr(board_service_chroma.jira_service, "load_board_cache", lambda: [issue])
        monkeypatch.setattr(board_service_chroma, "load_file_cached_analysis", lambda *args, **kwargs: None)

        columns = service.get_board_data()
        meta = service.get_last_board_fetch_meta()

        assert columns["today"][0]["key"] == "MYPROJECT-1"
        assert meta["data_source"] == "local_cache"
        assert meta["cache_timestamp"] == "2026-03-17 09:45:59"
        assert "jira_cache_service" in meta["jira_error"]
        assert "jira_direct" in meta["jira_error"]

    def test_get_board_data_prefers_proxy_when_proxy_mode_enabled(self, monkeypatch):
        """开启代理优先后，应直接命中 mini 代理，不再先等待 Jira 直连。"""
        import board_service_chroma

        service = self._build_stub_service()
        issue = self._fake_issue()
        service.jira_cache_service = MagicMock()
        service.jira_cache_service.search_issues.return_value = {
            "status": "success",
            "data": {"issues": [{"key": issue.key, "fields": {}}]},
        }
        service._board_fetch_prefer_proxy = True

        direct_mock = MagicMock(side_effect=AssertionError("jira direct should be skipped"))
        parse_mock = MagicMock(return_value=[issue])

        monkeypatch.setattr(board_service_chroma.jira_service, "search_issues_rest_api", direct_mock)
        monkeypatch.setattr(board_service_chroma.jira_service, "parse_search_response", parse_mock)
        monkeypatch.setattr(board_service_chroma.jira_service, "save_board_cache", MagicMock())
        monkeypatch.setattr(board_service_chroma, "load_file_cached_analysis", lambda *args, **kwargs: None)

        columns = service.get_board_data()

        assert columns["today"][0]["key"] == "MYPROJECT-1"
        assert service.get_last_board_fetch_meta()["data_source"] == "jira_proxy"
        direct_mock.assert_not_called()

    def test_get_board_data_skips_direct_during_cooldown_after_timeout(self, monkeypatch):
        """直连超时后，冷却期内的后续请求应先走代理，避免每次白等超时。"""
        import board_service_chroma

        service = self._build_stub_service()
        issue = self._fake_issue()
        service.jira_cache_service = MagicMock()
        service.jira_cache_service.search_issues.return_value = {
            "status": "success",
            "data": {"issues": [{"key": issue.key, "fields": {}}]},
        }

        direct_mock = MagicMock(return_value={"error": "ConnectTimeout"})
        parse_mock = MagicMock(return_value=[issue])

        monkeypatch.setattr(board_service_chroma.jira_service, "search_issues_rest_api", direct_mock)
        monkeypatch.setattr(board_service_chroma.jira_service, "parse_search_response", parse_mock)
        monkeypatch.setattr(board_service_chroma.jira_service, "save_board_cache", MagicMock())
        monkeypatch.setattr(board_service_chroma, "load_file_cached_analysis", lambda *args, **kwargs: None)

        first_columns = service.get_board_data()
        second_columns = service.get_board_data()

        assert first_columns["today"][0]["key"] == "MYPROJECT-1"
        assert second_columns["today"][0]["key"] == "MYPROJECT-1"
        assert service.get_last_board_fetch_meta()["data_source"] == "jira_proxy"
        assert direct_mock.call_count == 1


class TestBoardColumnAssignment:
    """测试智能看板日期列在可见性变化下的归类逻辑"""

    @staticmethod
    def _build_service(columns):
        from board_service_chroma import BoardService

        service = BoardService.__new__(BoardService)
        service.board_config = {"columns": columns}
        return service

    @staticmethod
    def _build_issue(key: str, due_date: str):
        from jira_service import JiraIssue

        return JiraIssue(
            key=key,
            summary=f"{key} summary",
            status="处理中",
            assignee="张三",
            reporter="李四",
            created="2026-03-18 10:00:00",
            updated="2026-03-18 10:05:00",
            due_date=due_date,
            priority="High",
            issue_type="Support",
            project_name="流程中心",
            description="描述",
        )

    @staticmethod
    def _system_columns(today_visible=True, tomorrow_visible=True):
        return [
            {"key": "today", "title": "今天到期", "type": "system", "rule": "today", "visible": today_visible},
            {"key": "tomorrow", "title": "明天到期", "type": "system", "rule": "tomorrow", "visible": tomorrow_visible},
            {"key": "this_week", "title": "本周到期", "type": "system", "rule": "this_week", "visible": True},
            {"key": "next_week", "title": "下周到期", "type": "system", "rule": "next_week", "visible": True},
            {"key": "future", "title": "更晚", "type": "system", "rule": "future", "visible": True},
            {"key": "no_date", "title": "无到期日", "type": "system", "rule": "no_date", "visible": True},
        ]

    def test_hidden_today_and_tomorrow_fall_back_to_weekly_bucket(self):
        """today/tomorrow 隐藏时，工单应回落到对应的周范围列，而不是从看板消失。"""
        service = self._build_service(self._system_columns(today_visible=False, tomorrow_visible=False))

        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        this_week_end = today + timedelta(days=(6 - today.weekday()))
        tomorrow_bucket = "this_week" if tomorrow <= this_week_end else "next_week"

        issues = [
            self._build_issue("MYPROJECT-TODAY", today.strftime("%Y-%m-%d")),
            self._build_issue("MYPROJECT-TOMORROW", tomorrow.strftime("%Y-%m-%d")),
        ]

        columns = service._organize_columns(issues)

        expected_this_week = ["MYPROJECT-TODAY"]
        if tomorrow_bucket == "this_week":
            expected_this_week.append("MYPROJECT-TOMORROW")

        assert [item["key"] for item in columns["this_week"]] == expected_this_week
        if tomorrow_bucket == "next_week":
            assert [item["key"] for item in columns["next_week"]] == ["MYPROJECT-TOMORROW"]
        assert columns.get("today", []) == []
        assert columns.get("tomorrow", []) == []

    def test_visible_today_and_tomorrow_take_priority_over_weekly_bucket(self):
        """today/tomorrow 可见时，对应工单应优先进入专属列，不再落入周范围列。"""
        service = self._build_service(self._system_columns(today_visible=True, tomorrow_visible=True))

        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)

        issues = [
            self._build_issue("MYPROJECT-TODAY", today.strftime("%Y-%m-%d")),
            self._build_issue("MYPROJECT-TOMORROW", tomorrow.strftime("%Y-%m-%d")),
        ]

        columns = service._organize_columns(issues)

        assert [item["key"] for item in columns["today"]] == ["MYPROJECT-TODAY"]
        assert [item["key"] for item in columns["tomorrow"]] == ["MYPROJECT-TOMORROW"]
        assert columns["this_week"] == []
        assert columns["next_week"] == []

    def test_visibility_priority_is_evaluated_per_column(self):
        """today 隐藏、tomorrow 可见时，应分别回落和优先，不应一起走同一套逻辑。"""
        service = self._build_service(self._system_columns(today_visible=False, tomorrow_visible=True))

        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)

        issues = [
            self._build_issue("MYPROJECT-TODAY", today.strftime("%Y-%m-%d")),
            self._build_issue("MYPROJECT-TOMORROW", tomorrow.strftime("%Y-%m-%d")),
        ]

        columns = service._organize_columns(issues)

        assert [item["key"] for item in columns["this_week"]] == ["MYPROJECT-TODAY"]
        assert [item["key"] for item in columns["tomorrow"]] == ["MYPROJECT-TOMORROW"]
        assert columns.get("today", []) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

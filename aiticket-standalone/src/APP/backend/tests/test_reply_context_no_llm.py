"""回归测试 — context_only（纯 MCP 委托）路径不得触发会 call_llm / 写 Jira 的 gate。

QA 军团 P0：context_only 分支原在 Gate1/Gate2 之后，Gate2 无条件 call_llm 且高置信写 Jira。
修复：context_only=True 时强制 force_pass_gate1/2=True 跳过 _run_gate1/2，并跳过 Gate3 direct。
本测试钉住该不变量，防回归。
"""
import os

os.environ.setdefault("APP_AUTH_DB_PATH", "/tmp/test_reply_context_auth.db")

from unittest.mock import patch

import main


def test_context_only_does_not_invoke_llm_gates():
    """context_only=True 时 Gate1(completeness)/Gate2(classification) 方法均不应被调用
    —— 这两个方法内部分别 call_llm，Gate2 高置信还会 move_issue_to_board(sync_jira=True)。"""
    bs = main.board_service
    with patch.object(bs, "_run_gate1_completeness", return_value=None) as m1, \
         patch.object(bs, "_run_gate2_classification", return_value=None) as m2:
        # 用不存在的工单：会在 ai_analysis 查找处返回 error，但 gate 阶段已先于此执行
        result = bs.generate_reply_content("ZZZZ-NONEXIST-99999", context_only=True)
        m1.assert_not_called()
        m2.assert_not_called()
    assert isinstance(result, dict)


def test_normal_path_still_runs_gates():
    """对照：context_only=False（默认）时 Gate1 应被调用（确认守卫只对 context_only 生效，
    未误伤正常路径）。"""
    bs = main.board_service
    with patch.object(bs, "_run_gate1_completeness", return_value=None) as m1, \
         patch.object(bs, "_run_gate2_classification", return_value=None) as m2:
        bs.generate_reply_content("ZZZZ-NONEXIST-99999", context_only=False)
        # 正常路径未传 force_pass，故 Gate1 必被调用
        assert m1.called, "正常路径 Gate1 未被调用，守卫可能误伤"

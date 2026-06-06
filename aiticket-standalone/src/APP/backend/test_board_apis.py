#!/usr/bin/env python3
"""
智能看板分配和回复接口测试脚本
测试 assign 和 reply_and_close 接口
"""

import requests
import json
import sys

BASE_URL = "http://localhost:3000"

def test_field_options():
    """测试字段选项获取接口"""
    print("=" * 60)
    print("测试1: 获取字段选项")
    print("=" * 60)

    # 首先刷新缓存
    print("\n1.1 刷新字段选项缓存...")
    try:
        response = requests.post(
            f"{BASE_URL}/api/jira/field-options-refresh",
            json={"project_key": "MYPROJECT", "issue_type_id": "10001"},
            timeout=30
        )
        print(f"响应状态: {response.status_code}")
        print(f"响应内容: {json.dumps(response.json(), ensure_ascii=False, indent=2)}")
    except Exception as e:
        print(f"刷新缓存失败: {e}")

    # 获取字段选项
    print("\n1.2 获取字段选项...")
    try:
        response = requests.post(
            f"{BASE_URL}/api/jira/field-options",
            json={
                "issue_id": "MYPROJECT-59031",
                "field_ids": ["customfield_10410", "customfield_10729"]
            },
            timeout=10
        )
        print(f"响应状态: {response.status_code}")
        data = response.json()
        print(f"响应内容: {json.dumps(data, ensure_ascii=False, indent=2)}")

        if data.get('status') == 'success':
            print("\n✓ 字段选项获取成功")
            return data.get('data', {})
        else:
            print(f"\n✗ 字段选项获取失败: {data.get('message')}")
            return None
    except Exception as e:
        print(f"✗ 请求失败: {e}")
        return None

def test_assign_issue(issue_id: str, assignee: str):
    """测试分配接口"""
    print("\n" + "=" * 60)
    print("测试2: 分配工单")
    print("=" * 60)
    print(f"\n工单: {issue_id}")
    print(f"分配给: {assignee}")

    try:
        response = requests.post(
            f"{BASE_URL}/api/jira/action",
            json={
                "issue_id": issue_id,
                "action": "assign",
                "value": assignee,
                "extra": {"comment": "通过API测试分配"}
            },
            timeout=15
        )
        print(f"\n响应状态: {response.status_code}")
        data = response.json()
        print(f"响应内容: {json.dumps(data, ensure_ascii=False, indent=2)}")

        if data.get('status') == 'success':
            print("\n✓ 分配成功")
            return True
        else:
            print(f"\n✗ 分配失败: {data.get('detail')}")
            return False
    except Exception as e:
        print(f"\n✗ 请求失败: {e}")
        return False

def test_reply_and_close(issue_id: str, field_options: dict):
    """测试回复并关闭接口"""
    print("\n" + "=" * 60)
    print("测试3: 回复并关闭工单")
    print("=" * 60)
    print(f"\n工单: {issue_id}")

    # 获取字段选项ID
    reply_method_id = "10307"  # 默认：指导解决
    issue_type_id = "10407"    # 默认：操作类问题

    if field_options:
        cf_10410 = field_options.get('customfield_10410', [])
        cf_10729 = field_options.get('customfield_10729', [])

        if cf_10410:
            reply_method_id = cf_10410[0].get('id', reply_method_id)
            print(f"回复方式: {cf_10410[0].get('value')} (ID: {reply_method_id})")

        if cf_10729:
            issue_type_id = cf_10729[0].get('id', issue_type_id)
            print(f"问题类型: {cf_10729[0].get('value')} (ID: {issue_type_id})")

    solution = f"这是通过API测试提交的解决方案。\n\n测试时间: {__import__('time').strftime('%Y-%m-%d %H:%M:%S')}\n\n问题已确认并解决。"

    try:
        response = requests.post(
            f"{BASE_URL}/api/jira/action",
            json={
                "issue_id": issue_id,
                "action": "reply_and_close",
                "value": solution,
                "custom_fields": {
                    "solution": solution,
                    "reply_method": reply_method_id,
                    "issue_type_confirmed": issue_type_id
                }
            },
            timeout=15
        )
        print(f"\n响应状态: {response.status_code}")
        data = response.json()
        print(f"响应内容: {json.dumps(data, ensure_ascii=False, indent=2)}")

        if data.get('status') == 'success':
            print("\n✓ 回复并关闭成功")
            return True
        else:
            print(f"\n✗ 回复并关闭失败: {data.get('detail')}")
            return False
    except Exception as e:
        print(f"\n✗ 请求失败: {e}")
        return False

def test_field_mapping():
    """测试字段值到ID的映射"""
    print("\n" + "=" * 60)
    print("测试4: 字段值映射验证")
    print("=" * 60)

    from jira_service import jira_service

    test_cases = [
        ('customfield_10410', '指导解决'),
        ('customfield_10410', '10307'),  # ID本身
        ('customfield_10729', '操作类问题'),
        ('customfield_10729', '10407'),  # ID本身
        ('customfield_10410', '不存在的值'),
    ]

    print("\n测试字段值映射:")
    for field_id, value in test_cases:
        mapped_id = jira_service._get_field_id_by_value(field_id, value)
        print(f"  {field_id}: '{value}' -> '{mapped_id}'")

def main():
    print("智能看板API测试")
    print("=" * 60)

    # 检查服务是否运行
    try:
        response = requests.get(f"{BASE_URL}/api/health", timeout=5)
        print(f"✓ 服务运行正常: {response.status_code}")
    except:
        print(f"✗ 服务未运行，请确保后端服务已启动: python main.py")
        sys.exit(1)

    # 测试字段选项
    field_options = test_field_options()

    # 获取测试工单ID（从命令行参数或交互式输入）
    test_issue_id = None
    if len(sys.argv) > 1:
        test_issue_id = sys.argv[1]
    else:
        print("\n请输入测试工单ID (如: MYPROJECT-59031，留空跳过分配/回复测试):")
        user_input = input().strip()
        if user_input:
            test_issue_id = user_input

    if test_issue_id:
        # 测试字段映射
        test_field_mapping()

        # 测试分配
        print("\n请输入要分配的用户名 (如: zhangsan，留空跳过分配测试):")
        assignee = input().strip()
        if assignee:
            test_assign_issue(test_issue_id, assignee)

        # 测试回复并关闭
        print("\n是否测试回复并关闭工单? (y/n):")
        confirm = input().strip().lower()
        if confirm == 'y':
            # 注意：这会实际关闭工单，请谨慎使用
            print("⚠️ 警告: 这将实际关闭工单，确认继续? (yes/no):")
            confirm2 = input().strip().lower()
            if confirm2 == 'yes':
                test_reply_and_close(test_issue_id, field_options)
            else:
                print("已取消回复并关闭测试")
    else:
        print("\n跳过分配/回复测试")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)

if __name__ == "__main__":
    main()

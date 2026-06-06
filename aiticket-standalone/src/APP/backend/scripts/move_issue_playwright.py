#!/usr/bin/env python3
"""
移动 JIRA 工单的独立 Playwright 脚本。
通过 subprocess 调用，避免阻塞 uvicorn 线程池。

用法：
    python move_issue_playwright.py <issue_id> <target_project_id> <state_path> \
        [--base-url URL] [--ssl-verify 1|0] [--proxy URL] \
        [--username USER] [--password PASS] \
        [--field customfield_10123=VALUE]

输出：单行 JSON，格式 {"success": bool, "message": str, "new_key": str}
"""
import sys, os, json, re, argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("issue_id")
    parser.add_argument("target_project_id")
    parser.add_argument("state_path")
    parser.add_argument("--base-url", default="https://jira.example.com")
    parser.add_argument("--ssl-verify", default="1")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--field", action="append", default=[])
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    ssl_verify = args.ssl_verify != "0"
    proxy_cfg = {"server": args.proxy} if args.proxy else None
    field_values = {}
    for f in (args.field or []):
        if "=" in f:
            k, v = f.split("=", 1)
            field_values[k] = v

    def out(result: dict):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                proxy=proxy_cfg,
            )

            # 加载已有 session（存在则用，否则从空白开始）
            storage = args.state_path if os.path.exists(args.state_path) else None
            ctx = browser.new_context(
                storage_state=storage,
                ignore_https_errors=not ssl_verify,
            )
            page = ctx.new_page()

            # ── 1. 打开 MoveIssue 页面 ──────────────────────────────────────
            page.goto(
                f"{base_url}/secure/MoveIssue!default.jspa?id={args.issue_id}",
                wait_until="networkidle", timeout=25000,
            )

            def _is_login_page():
                url = page.url.lower()
                if "login" in url or "authlogin" in url:
                    return True
                content = page.text_content("body") or ""
                return "还没有登录" in content or "匿名用户" in content or "请先登录" in content

            # ── 2. 如果被重定向到登录页，执行浏览器登录 ───────────────────
            if _is_login_page():
                # 先删除失效的 session 文件
                try: os.remove(args.state_path)
                except: pass

                # 检测是否是标准 Jira 登录表单（非 SSO/LDAP）
                std_form_selector = 'input[name="os_username"], #login-form-username'
                try:
                    page.wait_for_selector(std_form_selector, timeout=2000)
                    has_std_form = True
                except Exception:
                    has_std_form = False

                if not has_std_form:
                    # SSO / LDAP 登录页，无法自动填写，立即提示
                    browser.close()
                    out({"success": False,
                         "message": "Jira session 已过期（跳转到 SSO 登录页），请点击「刷新授权」后重试",
                         "new_key": ""})

                if not args.username or not args.password:
                    browser.close()
                    out({"success": False,
                         "message": "Jira session 过期，且未配置账号密码无法自动登录，请点击「刷新授权」",
                         "new_key": ""})

                # 填写标准 Jira 登录表单
                try:
                    page.fill(
                        'input[name="os_username"], #login-form-username, input[id*="username"]',
                        args.username, timeout=5000
                    )
                    page.fill(
                        'input[name="os_password"], #login-form-password, input[id*="password"]',
                        args.password, timeout=5000
                    )
                    page.click(
                        '#login-form-submit, input[type="submit"], button[type="submit"]',
                        timeout=5000
                    )
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception as login_e:
                    browser.close()
                    out({"success": False,
                         "message": f"自动登录操作失败: {login_e}",
                         "new_key": ""})

                if _is_login_page():
                    browser.close()
                    out({"success": False,
                         "message": "Jira 自动登录失败（账号/密码错误或需要 MFA）",
                         "new_key": ""})

                # 保存新 session（供下次复用）
                try:
                    ctx.storage_state(path=args.state_path)
                except Exception:
                    pass

                # 登录后重新导航到 MoveIssue
                page.goto(
                    f"{base_url}/secure/MoveIssue!default.jspa?id={args.issue_id}",
                    wait_until="networkidle", timeout=25000,
                )

                if _is_login_page():
                    browser.close()
                    out({"success": False,
                         "message": "登录后仍无法打开 MoveIssue 页面，请检查工单权限",
                         "new_key": ""})

            # ── 3. 确认在 MoveIssue 页面 ──────────────────────────────────
            if "MoveIssue" not in page.url and "移动" not in (page.title() or ""):
                browser.close()
                out({"success": False,
                     "message": f"MoveIssue 页面未加载（当前: {page.url}）",
                     "new_key": ""})

            # ── 4. 选择目标项目 ────────────────────────────────────────────
            proj_key = ""
            try:
                proj_key = page.evaluate(f"""
                    (async () => {{
                        const r = await fetch('/rest/api/2/project/{args.target_project_id}');
                        const d = await r.json();
                        return d.key || '';
                    }})()
                """) or ""
            except Exception:
                pass

            if proj_key:
                page.fill("#project-field", "")
                page.type("#project-field", proj_key, delay=50)
                page.wait_for_timeout(500)
                try:
                    page.click("#project-suggestions a:first-child")
                    page.wait_for_timeout(400)
                except Exception:
                    pass

            # ── 5. 下一步 ──────────────────────────────────────────────────
            # 用 page.click() 而非 page.evaluate().click()：
            # evaluate 触发导航时 JS context 被销毁，抛 "Execution context was destroyed"
            try:
                page.click('input[value="下一步 >>"]', timeout=8000)
            except Exception:
                # 尝试英文 label
                try:
                    page.click('input[value="Next >"]', timeout=3000)
                except Exception:
                    pass
            page.wait_for_load_state("networkidle", timeout=20000)

            # ── 6. UpdateFields 页面（可选字段填写）────────────────────────
            if "UpdateFields" in page.url:
                domain_val = field_values.get("customfield_10123", "")
                sub_domain_val = field_values.get("customfield_10123:1", "")
                if domain_val:
                    page.evaluate(f"""
                        var s = document.querySelector('[name="customfield_10123"]');
                        if (s) {{ s.value = '{domain_val}'; s.dispatchEvent(new Event('change', {{bubbles: true}})); }}
                    """)
                    page.wait_for_timeout(1000)
                if sub_domain_val:
                    page.evaluate(f"""
                        var s = document.querySelector('[name="customfield_10123:1"]');
                        if (s) s.value = '{sub_domain_val}';
                    """)
                try:
                    page.click('input[value="下一步 >>"]', timeout=8000)
                except Exception:
                    try:
                        page.click('input[value="Next >"]', timeout=3000)
                    except Exception:
                        pass
                page.wait_for_load_state("networkidle", timeout=20000)

            # ── 7. 确认移动 ────────────────────────────────────────────────
            try:
                page.click('#move_submit', timeout=8000)
            except Exception:
                # 降级：submit 类型按钮
                try:
                    page.click('input[type="submit"]', timeout=3000)
                except Exception:
                    pass
            page.wait_for_load_state("networkidle", timeout=20000)

            final_url = page.url
            final_title = page.title()
            browser.close()

            # 成功路径 1：正常跳转到 /browse/NEW-KEY
            if "browse/" in final_url and "错误" not in final_title:
                m = re.search(r'/browse/([A-Z]+-\d+)', final_url)
                new_key = m.group(1) if m else ""
                out({"success": True, "message": f"工单移动成功 → {new_key}", "new_key": new_key})

            # 成功路径 2：Jira 某些版本移动后仍停留在 MoveIssue 页，但标题已含新 key
            # 例如：title = "移动问题: OMST-46 - 股份Jira", url = ".../MoveIssue.jspa"
            m2 = re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', final_title)
            if m2 and "MoveIssue" in final_url and "错误" not in final_title:
                new_key = m2.group(1)
                out({"success": True, "message": f"工单移动成功（标题检测） → {new_key}", "new_key": new_key})

            out({"success": False,
                 "message": f"移动可能失败（最终页: {final_title} | {final_url}）",
                 "new_key": ""})

    except PWTimeout:
        if browser:
            try: browser.close()
            except: pass
        out({"success": False, "message": "Playwright 操作超时（25s），请检查 Jira 连通性", "new_key": ""})
    except Exception as e:
        if browser:
            try: browser.close()
            except: pass
        out({"success": False, "message": f"移动失败: {str(e)}", "new_key": ""})


if __name__ == "__main__":
    main()

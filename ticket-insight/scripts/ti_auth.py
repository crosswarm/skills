#!/usr/bin/env python3
"""ticket-insight S0 认证
用法:
  python3 ti_auth.py --setup                 # 引导式配置（密码隐藏输入+Fernet机器绑定加密）
  python3 ti_auth.py --test                  # 认证级联+身份校验（显示登录者）
  python3 ti_auth.py --paste-cookie <JSESSIONID>   # 手动粘贴浏览器 cookie
"""
import argparse, getpass, sys
from ti_common import (DEFAULT_BASE, banner, base_url, encrypt_password, get_cookie,
                       load_config, save_config, validate_session, _cookie_header, jira_get)


def cmd_setup():
    print(banner(0))
    print('── ticket-insight 首次配置（密码用机器专属密钥加密，config 无明文，不可跨机器复制）──')
    cfg = load_config()
    url = input(f'Jira 地址 [{cfg.get("jira_base_url", DEFAULT_BASE)}]: ').strip() or cfg.get('jira_base_url', DEFAULT_BASE)
    user = input(f'Jira 用户名 [{cfg.get("username", "")}]: ').strip() or cfg.get('username', '')
    pwd = getpass.getpass('Jira 密码（输入不可见，可留空=只用浏览器登录态）: ')
    outd = input('报告默认输出目录 [系统下载目录]: ').strip()
    cfg.update({'jira_base_url': url.rstrip('/'), 'username': user})
    if pwd:
        cfg['password_enc'] = encrypt_password(pwd)
    if outd:
        cfg['default_output_dir'] = outd
    save_config(cfg)
    print('✅ 配置已保存。开始验证登录…')
    cmd_test()


def cmd_test():
    cookie, source, who = get_cookie()
    me = jira_get('/rest/api/2/myself', {}, cookie)
    print(banner(0))
    print(f'✅ 已登录: {me.get("displayName")}({me.get("name")}) · 来源: {source} · session 有效')
    print('下一步 → 选择分析范围: python3 scripts/ti_scope.py --project <KEY> --probe')
    return 0


def cmd_paste(jsid: str):
    jsid = jsid.strip().split('=')[-1]          # 容错: 用户可能连 "JSESSIONID=" 一起粘
    ch = _cookie_header({'JSESSIONID': jsid})
    who = validate_session(ch)
    if not who:
        print('❌ 这个 JSESSIONID 无效或已过期。请确认从已登录的浏览器复制（F12→Application→Cookies）')
        return 2
    cfg = load_config(); cfg['jsessionid'] = jsid; save_config(cfg)
    print(f'✅ cookie 有效，已保存。登录者: {who}')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--setup', action='store_true')
    ap.add_argument('--test', action='store_true')
    ap.add_argument('--paste-cookie', metavar='JSESSIONID')
    a = ap.parse_args()
    if a.setup:
        cmd_setup()
    elif a.paste_cookie:
        sys.exit(cmd_paste(a.paste_cookie))
    else:
        sys.exit(cmd_test())

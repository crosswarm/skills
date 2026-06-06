#!/usr/bin/env python3
"""aiticket compact 安装器 — 跨平台一条命令本地装好并起服务。

流程：取源码 → 建 venv(uv 优先) → 装 requirements-core → 写配置(deployment.yaml/env.json)
      → init_db → seed_admin → 注册开机自启 → 启动 → /api/liveness 探活。

布局见 aiticket_paths.py。LLM key 为可选（默认不填 = 纯 MCP 委托，由调用方 Agent 生成回复），
后续用 /aiticket-config 再补 key。

用法（典型）：
    python install.py --src .                       # 从当前 checkout 安装（开发/验证）
    python install.py --repo <url> --branch main    # 从 git 克隆安装（发布）
    python install.py --admin-user admin --admin-password '***' --jira-url https://jira.example.com
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiticket_paths as P  # noqa: E402
import service_manager as SM  # noqa: E402

REPO_DEFAULT = "https://github.com/crosswarm/aiticket.git"
BRANCH_DEFAULT = "aiticket-standalone"


# ---------- 小工具 ----------

def _run(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       env=env, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"✗ 命令失败（exit {r.returncode}）：{' '.join(map(str, cmd))}")
    return r


def _step(msg: str) -> None:
    print(f"\n▶ {msg}")


def _ping(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(P.liveness_url(port), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ---------- 各步骤 ----------

def make_layout(home: Path) -> None:
    _step(f"创建目录布局 → {home}")
    for d in (home, P.data_dir(home), P.sqlite_dir(home), P.logs_dir(home),
              P.kb_dir(home), P.config_dir(home)):
        d.mkdir(parents=True, exist_ok=True)
        print(f"  ✓ {d}")


def obtain_src(home: Path, src: str | None, repo: str, branch: str, force: bool) -> None:
    _step("获取源码")
    dst = P.src_dir(home)
    if dst.exists() or dst.is_symlink():
        if force:
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        else:
            print(f"  ✓ src 已存在，跳过（--force 可重建）：{dst}")
            return
    if src:
        abs_src = Path(src).expanduser().resolve()
        if not (abs_src / "APP" / "backend" / "main.py").exists():
            raise SystemExit(f"✗ --src 不是有效 checkout（缺 APP/backend/main.py）：{abs_src}")
        # 复制源码（独立安装，避免运行期数据写回 skill 目录 / 符号链接失效）
        shutil.copytree(abs_src, dst, ignore=shutil.ignore_patterns(
            ".git", ".venv*", "__pycache__", "node_modules", "data", "*.log",
            "*.db", "*.db-shm", "*.db-wal", "*.key"))
        print(f"  ✓ 复制源码 {dst}（来自 {abs_src}）")
    else:
        _run(["git", "clone", "--depth", "1", "-b", branch, repo, str(dst)])
        print(f"  ✓ 克隆完成 {dst}")


def ensure_venv(home: Path, full: bool, force: bool) -> None:
    _step("创建 venv 并安装依赖")
    venv = P.venv_dir(home)
    vpy = P.venv_python(home)
    req_name = "requirements-full.txt" if full else "requirements-core.txt"
    req = P.backend_dir(home) / req_name
    if not req.exists():
        raise SystemExit(f"✗ 找不到依赖清单：{req}")

    have_uv = shutil.which("uv") is not None
    if force and venv.exists():
        shutil.rmtree(venv)

    if have_uv:
        if not vpy.exists():
            _run(["uv", "venv", str(venv), "--python", "3.12"])
        _run(["uv", "pip", "install", "--python", str(vpy), "-r", str(req)])
    else:
        print("  ⚠ 未找到 uv，回退 python -m venv + pip（较慢；建议装 uv 提速）")
        if not vpy.exists():
            _run([sys.executable, "-m", "venv", str(venv)])
        _run([str(vpy), "-m", "pip", "install", "--upgrade", "pip"])
        _run([str(vpy), "-m", "pip", "install", "-r", str(req)])
    print(f"  ✓ 依赖安装完成（{req_name}）")


def write_config(home: Path, port: int, jira_url: str, kb_dir: str | None) -> None:
    _step("写配置 deployment.yaml + env.json")
    kb_root = str(Path(kb_dir).expanduser().resolve()) if kb_dir else str(P.kb_dir(home))
    # deployment.yaml（config.loader 经 CONFIG_FILE 读取）
    # 单引号 YAML + 转义（install.py 跑在系统 python，无 PyYAML 依赖；
    # 单引号串只需把 ' 翻倍，可安全容纳 URL 里的 : / " \\ 等特殊字符）
    def _yq(v: str) -> str:
        return str(v).replace("'", "''")
    yaml_text = (
        "# aiticket compact 实例配置（由 install.py 生成，可手改后 restart 生效）\n"
        "instance:\n"
        "  name: aiticket compact\n"
        "  slug: aiticket\n"
        "jira:\n"
        f"  base_url: '{_yq(jira_url)}'\n"
        "  ssl_verify: true\n"
        "kb:\n"
        f"  root_dir: '{_yq(kb_root)}'\n"
        "llm:\n"
        "  default_provider_chain: ['zhipu', 'minimax']\n"
    )
    P.deployment_yaml_path(home).write_text(yaml_text, encoding="utf-8")
    print(f"  ✓ {P.deployment_yaml_path(home)}")
    # env.json（控制器/安装器跨平台共享：端口/路径快照）
    env_json = {
        "home": str(home),
        "port": port,
        "src": str(P.src_dir(home)),
        "venv_python": str(P.venv_python(home)),
        "auth_db": str(P.auth_db_path(home)),
        "kb_root": kb_root,
        "jira_base_url": jira_url,
    }
    P.env_json_path(home).write_text(json.dumps(env_json, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    print(f"  ✓ {P.env_json_path(home)}")


def init_db_and_admin(home: Path, admin_user: str, admin_password: str) -> None:
    _step("初始化数据库 + 管理员账号")
    vpy = P.venv_python(home)
    backend = P.backend_dir(home)
    data = P.data_dir(home)
    env = {**os.environ, **P.service_env(home, P.resolve_port(home))}
    _run([str(vpy), "-m", "bootstrap.init_db", "--data-dir", str(data)],
         cwd=backend, env=env)
    if admin_user and admin_password:
        _run([str(vpy), "-m", "bootstrap.seed_admin",
              "--data-dir", str(data),
              "--username", admin_user,
              "--password", admin_password,
              "--display-name", admin_user],
             cwd=backend, env=env)
        print(f"  ✓ 管理员 '{admin_user}' 就绪（auth.db: {P.auth_db_path(home)}）")
    else:
        print("  ⚠ 未提供 --admin-user/--admin-password，跳过种子管理员")
        print("    稍后可手动：")
        print(f"      {vpy} -m bootstrap.seed_admin --data-dir {data} --username admin --password '***'")


def generate_skill_token(home: Path, admin_user: str) -> None:
    """为 admin 生成 skill token 写入 config/env.json —— MCP server / 浏览器扩展开箱即用鉴权。
    （修复『安装承诺纯 MCP 委托但不生成 token，调用方开箱即 401』的契约缺口。）"""
    if not admin_user:
        print("  ⚠ 无管理员，跳过 skill token 生成（建后手动 make_skill_token.py）")
        return
    _step("生成 skill token（MCP / 浏览器扩展鉴权）")
    vpy = P.venv_python(home)
    tool = Path(__file__).resolve().parent / "make_skill_token.py"
    r = subprocess.run([str(vpy), str(tool), "--home", str(home), "--username", admin_user],
                       capture_output=True, text=True)
    if r.returncode == 0 and (r.stdout or "").strip():
        token = r.stdout.strip().splitlines()[-1]
        print(f"  ✓ 已写入 {P.env_json_path(home)}（token 前 8 位 {token[:8]}…）")
    else:
        print(f"  ⚠ 生成失败（可稍后手动 make_skill_token.py）：{(r.stderr or '').strip()[:160]}")


def register_and_start(home: Path, port: int, no_autostart: bool, no_start: bool) -> bool:
    if no_autostart:
        # 非持久启动：用 pidfile 后端，绝不写 launchd/systemd 持久单元
        # （否则 LaunchdManager/SystemdUserManager.start() 在单元缺失时会隐式 install_autostart）
        # 管理用：AITICKET_SERVICE_BACKEND=pidfile aiticket_ctl …
        mgr = SM.PidfileManager(home, port)
    else:
        mgr = SM.get_manager(home, port)
        _step(f"注册开机自启（后端={mgr.name}）")
        try:
            mgr.install_autostart()
            print("  ✓ 已注册")
        except Exception as e:
            print(f"  ⚠ 自启注册失败（不影响手动启动）：{e}")
    if no_start:
        return False
    _step(f"启动服务（后端={mgr.name}）")
    if _ping(port):
        print("  ✓ 服务已在运行")
        return True
    try:
        mgr.start()
    except Exception as e:
        print(f"  ✗ 启动失败：{e}")
        return False
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if _ping(port):
            print(f"  ✓ 启动成功 → {P.base_url(port)}")
            return True
        time.sleep(1.5)
    print("  ✗ 启动后 45s 内 /api/liveness 未通，请看日志：")
    print(f"      {P.venv_python(home)} {Path(__file__).parent / 'aiticket_ctl.py'} logs --err")
    return False


def print_summary(home: Path, port: int, healthy: bool) -> None:
    print("\n" + "=" * 56)
    print("  aiticket compact 安装完成" if healthy else "  aiticket compact 安装完成（服务未自动起，见上）")
    print("=" * 56)
    print(f"  访问看板 : {P.base_url(port)}")
    print(f"  HOME     : {home}")
    print(f"  KB 目录  : {P.kb_dir(home)}（/aiticket-config 可改）")
    ctl = Path(__file__).parent / "aiticket_ctl.py"
    vpy = P.venv_python(home)
    print(f"  控制     : {vpy} {ctl} [start|stop|restart|status|logs]")
    print("  下一步   : 填 Jira URL、装浏览器扩展抓 JSESSIONID、指定 KB 目录（/aiticket-config）")
    print("  说明     : 未填 LLM key = 纯 MCP 委托模式，由调用方 Agent 生成回复")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="install", description="aiticket compact 安装器")
    ap.add_argument("--home", default="", help="安装目录（默认 ~/.aiticket）")
    ap.add_argument("--port", type=int, default=None, help=f"端口（默认 {P.DEFAULT_PORT}）")
    ap.add_argument("--src", default="", help="从本地 checkout 安装（符号链接）")
    ap.add_argument("--repo", default=REPO_DEFAULT, help="git 仓库（无 --src 时克隆）")
    ap.add_argument("--branch", default=BRANCH_DEFAULT, help="git 分支")
    ap.add_argument("--full", action="store_true", help="装 requirements-full（含报表/pandas）")
    ap.add_argument("--admin-user", default="", help="种子管理员用户名")
    ap.add_argument("--admin-password", default="", help="种子管理员密码")
    ap.add_argument("--jira-url", default="", help="Jira base_url")
    ap.add_argument("--kb-dir", default="", help="KB 目录（默认 <home>/kb）")
    ap.add_argument("--no-autostart", action="store_true", help="不注册开机自启")
    ap.add_argument("--no-start", action="store_true", help="装完不自动启动")
    ap.add_argument("--force", action="store_true", help="重建 src/venv")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path(args.home).expanduser() if args.home else P.default_home()
    port = P.resolve_port(home, override=args.port)

    # 发布版：tools/ 同级若有 bundled 源码（src/APP/backend/main.py），默认用它免 clone
    if not args.src:
        _bundled = Path(__file__).resolve().parent.parent / "src"
        if (_bundled / "APP" / "backend" / "main.py").exists():
            args.src = str(_bundled)
            print(f"[install] 使用随 skill 打包的源码（免 clone）：{_bundled}")

    print(f"aiticket compact 安装 · HOME={home} · 端口={port}")
    make_layout(home)
    obtain_src(home, args.src or None, args.repo, args.branch, args.force)
    ensure_venv(home, args.full, args.force)
    write_config(home, port, args.jira_url, args.kb_dir or None)
    init_db_and_admin(home, args.admin_user, args.admin_password)
    generate_skill_token(home, args.admin_user)
    healthy = register_and_start(home, port, args.no_autostart, args.no_start)
    print_summary(home, port, healthy)
    return 0 if (healthy or args.no_start) else 1


if __name__ == "__main__":
    raise SystemExit(main())

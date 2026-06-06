#!/usr/bin/env python3
"""aiticket compact 控制器 — start / stop / restart / status / logs。

跨平台：经 service_manager 选 launchd/systemd/pidfile 后端起停服务；
服务"是否健康"以 GET /api/liveness 为权威。仅用 stdlib（可在系统 Python 下直接跑）。

用法：
    python aiticket_ctl.py start   [--home DIR] [--port N]
    python aiticket_ctl.py stop
    python aiticket_ctl.py restart
    python aiticket_ctl.py status
    python aiticket_ctl.py logs    [--lines 80] [--err]
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiticket_paths as P  # noqa: E402
import service_manager as SM  # noqa: E402


def _ping_liveness(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(P.liveness_url(port), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_healthy(port: int, total: float = 40.0, interval: float = 1.5) -> bool:
    deadline = time.monotonic() + total
    while time.monotonic() < deadline:
        if _ping_liveness(port):
            return True
        time.sleep(interval)
    return False


def _resolve(args) -> tuple[Path, int]:
    home = Path(args.home).expanduser() if args.home else P.default_home()
    port = P.resolve_port(home, override=args.port)
    return home, port


def cmd_start(args) -> int:
    home, port = _resolve(args)
    if _ping_liveness(port):
        print(f"✓ 服务已在运行 → {P.base_url(port)}")
        return 0
    mgr = SM.get_manager(home, port)
    print(f"[start] 后端={mgr.name} 端口={port}")
    mgr.start()
    if _wait_healthy(port):
        print(f"✓ 启动成功 → {P.base_url(port)}")
        return 0
    print(f"✗ 启动后 /api/liveness 未通，请查看日志：python {Path(__file__).name} logs --err")
    return 1


def cmd_stop(args) -> int:
    home, port = _resolve(args)
    mgr = SM.get_manager(home, port)
    print(f"[stop] 后端={mgr.name}")
    mgr.stop()
    # 等待端口释放
    for _ in range(10):
        if not _ping_liveness(port, timeout=1.0):
            print("✓ 已停止")
            return 0
        time.sleep(1.0)
    print("⚠ 停止命令已发，但 liveness 仍可达（可能有残留进程）")
    return 1


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(1.0)
    return cmd_start(args)


def cmd_status(args) -> int:
    home, port = _resolve(args)
    mgr = SM.get_manager(home, port)
    healthy = _ping_liveness(port)
    loaded = False
    try:
        loaded = mgr.is_loaded()
    except Exception:
        pass
    print("aiticket compact · 状态")
    print(f"  HOME    : {home}")
    print(f"  后端    : {mgr.name}")
    print(f"  端口    : {port}")
    print(f"  单元加载: {'是' if loaded else '否'}")
    print(f"  健康    : {'healthy ✓' if healthy else 'down ✗'}  ({P.liveness_url(port)})")
    if healthy:
        print(f"  访问    : {P.base_url(port)}")
    return 0 if healthy else 1


def cmd_logs(args) -> int:
    home, _ = _resolve(args)
    lf = P.err_log_file(home) if args.err else P.log_file(home)
    if not lf.exists():
        print(f"(无日志文件：{lf})")
        return 1
    try:
        lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        print(f"读取日志失败：{e}")
        return 1
    tail = lines[-args.lines:] if args.lines > 0 else lines
    print(f"# {lf}  (末 {len(tail)} 行)")
    print("\n".join(tail))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="aiticket_ctl", description="aiticket compact 控制器")
    ap.add_argument("--home", default="", help="AITICKET_HOME（默认 ~/.aiticket）")
    ap.add_argument("--port", type=int, default=None, help="端口（默认按 env.json / 18080）")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("start", "stop", "restart", "status"):
        sub.add_parser(name)
    lg = sub.add_parser("logs")
    lg.add_argument("--lines", type=int, default=80)
    lg.add_argument("--err", action="store_true", help="看 stderr 日志")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "logs": cmd_logs,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

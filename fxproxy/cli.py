"""fxproxy command-line interface."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import config as cfgmod
from .fxa_auth import bootstrap
from .guardian import GuardianClient, NotEnrolledError, TokenPool
from .proxyserver import NodePicker, ProxyServer


def _fmt_bytes(n) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PiB"


def _pool(args) -> TokenPool:
    cfg = cfgmod.load(args.config)
    tokens = cfg.get("refresh_tokens") or []
    if not tokens:
        print("no account; run `fxproxy login` first", file=sys.stderr)
        raise SystemExit(2)
    return TokenPool(
        tokens,
        strategy=cfg.get("pool_strategy", "round-robin"),
        cooldown=cfg.get("cooldown_seconds", 300),
        quota_cache_ttl=cfg.get("quota_cache_ttl", 60),
    )


def cmd_login(args):
    session_token = args.session_token
    email = args.email
    password = args.password
    if not session_token and email and not password:
        password = getpass.getpass("FxA password: ")
    if not session_token and not email:
        print("provide --session-token, or --email (+ --password)", file=sys.stderr)
        return 2

    print("[*] bootstrapping credentials ...", flush=True)
    res = bootstrap(session_token=session_token, email=email, password=password)
    cfg = cfgmod.load(args.config)
    rt = res["refresh_token"]
    tokens = cfg.get("refresh_tokens") or []
    if getattr(args, "append", False):
        if rt in tokens:
            print(f"[!] 账号已存在，未重复添加 (uid={res['quota']['uid']})")
            return 1
        tokens.append(rt)
        action = "已追加账号"
    else:
        if rt not in tokens:
            tokens.insert(0, rt)
        action = "已保存凭证"
    cfg["refresh_tokens"] = tokens
    if args.country:
        cfg["country"] = args.country.upper()
    path = cfgmod.save(cfg, args.config)
    q = res["quota"]
    print(f"[+] {action} uid={q['uid']}  quota={_fmt_bytes(q['max_bytes'])}"
          f"  subscribed={q['subscribed']}  账号数={len(tokens)}")
    print(f"[+] 凭证已保存到 {path}")
    print("    (keep this file secret; it grants proxy access to your accounts)")
    return 0


def cmd_accounts(args):
    """List every account with its quota and healthy/cooling status."""
    pool = _pool(args)
    now = __import__("time").monotonic()
    snaps = pool.quota_snapshot()
    print(f"策略: {pool.strategy}   账号数: {len(pool.clients)}   冷却: {pool.cooldown}s\n")
    total = 0
    total_known = True
    for i, (c, q) in enumerate(zip(pool.clients, snaps)):
        cooling = pool._cool_until.get(i, 0) > now
        tag = "冷却中" if cooling else "可用"
        rem = q.get("remaining_bytes")
        if q.get("unlimited"):
            rem_s, known = "无限", False
        elif rem is None:
            rem_s, known = q.get("error", "—")[:40], False
        else:
            rem_s, known = _fmt_bytes(rem), True
            total += rem
        total_known &= known
        print(f"  #{i + 1}  uid={str(q.get('uid') or '—'):<18} {tag:<6} 剩余 {rem_s}")
    print(f"\n总剩余: {'无限' if any(s.get('unlimited') for s in snaps) else (_fmt_bytes(total) if total_known else '部分未知')}")
    return 0


def cmd_token(args):
    cfg = cfgmod.load(args.config)
    tokens = cfg.get("refresh_tokens") or []
    if not tokens:
        print("no refresh_token; run `fxproxy login` first", file=sys.stderr)
        return 2
    quiet = getattr(args, "quiet", False)
    if quiet:
        print("\n".join(tokens))
        return 0
    print(f"refresh_token ×{len(tokens)} (顺序对应账号 #1..#N):\n")
    for i, t in enumerate(tokens):
        print(f"  #{i + 1}: {t}")
    print("\n纯脚本用 `fxproxy token -q`：每行一个 token，顺序不变。")
    return 0


def cmd_status(args):
    pool = _pool(args)
    s = pool.summary()
    ok = s["ok_accounts"]
    print(f"strategy         : {s['strategy']}")
    print(f"accounts         : {s['accounts']} ({ok} ok)")
    for i, q in enumerate(s["snapshots"]):
        rem = "unlimited" if q.get("unlimited") else _fmt_bytes(q.get("remaining_bytes"))
        print(f"  #{i + 1} uid={q.get('uid')}  remaining={rem}  ok={q.get('ok')}")
    return 0


def cmd_servers(args):
    pool = _pool(args)
    gc = pool.clients[0]  # 服务器列表是公开的，用任一账号拉取即可
    by = gc.nodes_by_country()
    for code in sorted(by):
        print(f"{code}  {by[code]['name']:20} {len(by[code]['nodes'])} node(s)")
    print(f"\n{len(by)} countries")
    return 0


def cmd_run(args):
    cfg = cfgmod.load(args.config)
    tokens = cfg.get("refresh_tokens") or []
    if not tokens:
        print("no account; run `fxproxy login` first", file=sys.stderr)
        return 2
    pool = TokenPool(
        tokens,
        strategy=cfg.get("pool_strategy", "round-robin"),
        cooldown=cfg.get("cooldown_seconds", 300),
        quota_cache_ttl=cfg.get("quota_cache_ttl", 60),
    )
    country = (args.country or cfg.get("country") or "").upper() or None
    picker = NodePicker(pool.clients[0], country)
    srv = ProxyServer(
        pool,
        picker,
        host=args.host or cfg["host"],
        http_port=args.http_port or cfg["http_port"],
        socks_port=args.socks_port or cfg["socks_port"],
        failover=getattr(args, "failover", None) or cfg.get("failover", 3),
    )
    try:
        asyncio.run(srv.serve())
    except KeyboardInterrupt:
        print("\n[fxproxy] 已停止")
    return 0


def cmd_web(args):
    from .webui import run_web
    run_web(
        args.config,
        web_host=getattr(args, "web_host", None) or "127.0.0.1",
        web_port=getattr(args, "web_port", None) or 8765,
        open_browser=not getattr(args, "no_open", False),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fxproxy",
        description="Standalone landing proxy over Firefox IP Protection (50GB/mo per account).",
    )
    p.add_argument("-c", "--config", help="config file path")
    sub = p.add_subparsers(dest="cmd", required=False)

    lp = sub.add_parser("login", help="bootstrap credentials (email+pass or sessionToken)")
    lp.add_argument("--email")
    lp.add_argument("--password")
    lp.add_argument("--session-token", help="pre-extracted FxA sessionToken (hex)")
    lp.add_argument("--country", help="default egress country ISO code")
    lp.add_argument("--append", action="store_true",
                    help="追加账号到池（默认覆盖第一个账号）")
    lp.set_defaults(func=cmd_login)

    ap = sub.add_parser("accounts", help="list all accounts with quota/status")
    ap.set_defaults(func=cmd_accounts)

    sp = sub.add_parser("status", help="show aggregate quota across accounts")
    sp.set_defaults(func=cmd_status)

    tp = sub.add_parser("token", help="print all refresh_tokens (copy to a server)")
    tp.add_argument("-q", "--quiet", action="store_true",
                    help="print only tokens, one per line")
    tp.set_defaults(func=cmd_token)

    vp = sub.add_parser("servers", help="list available egress countries")
    vp.set_defaults(func=cmd_servers)

    rp = sub.add_parser("run", help="start local SOCKS5 + HTTP proxy")
    rp.add_argument("--country", help="egress country ISO code (overrides config)")
    rp.add_argument("--host")
    rp.add_argument("--http-port", type=int)
    rp.add_argument("--socks-port", type=int)
    rp.add_argument("--failover", type=int,
                    help="number of nodes to try per account (default 3)")
    rp.set_defaults(func=cmd_run)

    wp = sub.add_parser("web", help="open the local web control panel")
    wp.add_argument("--web-host", default="127.0.0.1")
    wp.add_argument("--web-port", type=int, default=8765)
    wp.add_argument("--no-open", action="store_true", help="don't auto-open a browser")
    wp.set_defaults(func=cmd_web)
    return p


def _prompt(msg, default=""):
    v = input(f"{msg}" + (f" [{default}]" if default else "") + ": ").strip()
    return v or default


def interactive(config_path=None):
    ns = argparse.Namespace(config=config_path)
    while True:
        print("\n=== fxproxy — Firefox IP Protection 多账号落地代理 ===")
        print("  1) 登录/入组 (login)   —— 邮箱密码 或 sessionToken → 换凭证并入组")
        print("  2) 账号列表 (accounts) —— 各账号 uid / 配额 / 冷却状态")
        print("  3) 聚合配额 (status)")
        print("  4) 列出可用国家 (servers)")
        print("  5) 启动代理 (run)")
        print("  6) 可视化控制面板 (web)")
        print("  7) 显示 refresh_token (拷到服务器用)")
        print("  8) 退出")
        choice = input("选择指令 [1-8]: ").strip()
        try:
            if choice == "1":
                mode = _prompt("凭证方式: 1=邮箱密码  2=sessionToken", "1")
                a = argparse.Namespace(config=config_path, email=None, password=None,
                                       session_token=None, country=None, append=False)
                if mode == "2":
                    a.session_token = _prompt("粘贴 sessionToken(hex)")
                else:
                    a.email = _prompt("FxA 邮箱")
                    a.password = getpass.getpass("FxA 密码: ")
                a.country = _prompt("默认出口国家(留空=任意, 如 US/GB/DE)", "") or None
                a.append = _prompt("追加到账号池? 1=追加  0=覆盖", "1") == "1"
                cmd_login(a)
            elif choice == "2":
                cmd_accounts(ns)
            elif choice == "3":
                cmd_status(ns)
            elif choice == "4":
                cmd_servers(ns)
            elif choice == "5":
                a = argparse.Namespace(config=config_path, country=None,
                                       host=None, http_port=None, socks_port=None,
                                       failover=None)
                a.country = _prompt("出口国家(留空=用配置/任意)", "") or None
                print("(Ctrl+C 停止)")
                cmd_run(a)
            elif choice == "6":
                a = argparse.Namespace(config=config_path, web_host=None,
                                       web_port=None, no_open=False)
                print("(Ctrl+C 停止)")
                cmd_web(a)
            elif choice == "7":
                cmd_token(ns)
            elif choice in ("8", "q", "quit", "exit"):
                print("bye")
                return 0
            else:
                print("无效选择")
        except SystemExit:
            pass
        except KeyboardInterrupt:
            print("\n(已取消)")
        except Exception as e:
            print(f"[!] 出错: {e}")


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "cmd", None):
        return interactive(args.config)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

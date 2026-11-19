"""fxproxy command-line interface."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import config as cfgmod
from .guardian import GuardianClient, NotEnrolledError, TokenPool
from .proxyserver import NodePicker, ProxyServer

# fxa_auth 依赖 curl_cffi + pyfxa，只有首次登录/bootstrap 才用。
# 机房无头环境只跑代理时不需要这两个库，因此延迟到 cmd_login 内导入，
# 避免 `fxproxy run` 被重型登录依赖拖累。
def _bootstrap():
    from .fxa_auth import bootstrap
    return bootstrap


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


def _merge_tokens(old: list, incoming: list, append: bool) -> list:
    """--append 追加到池尾；默认覆盖第一个账号、保留其余并去重。"""
    if append:
        return old + [t for t in incoming if t not in old]
    return incoming + [t for t in old[1:] if t not in incoming]


def cmd_login(args):
    from . import cookieutil

    cfg = cfgmod.load(args.config)
    tokens = cfg.get("refresh_tokens") or []
    append = getattr(args, "append", False)

    # Cookie / 插件导出路径：可能一次导入多个账号
    cookie_raw = getattr(args, "cookie", None)
    cookie_file = getattr(args, "cookie_file", None)
    if cookie_file:
        try:
            with open(cookie_file, "r", encoding="utf-8") as fh:
                cookie_raw = fh.read()
        except OSError as e:
            print(f"[!] 读取 cookie 文件失败: {e}", file=sys.stderr)
            return 2
        if not (cookie_raw or "").strip():
            print(f"[!] cookie 文件为空: {cookie_file}", file=sys.stderr)
            return 2
    if cookie_raw:
        try:
            parsed = cookieutil.parse(cookie_raw)
        except ValueError as e:
            print(f"[!] cookie 解析失败: {e}", file=sys.stderr)
            return 2

        added, skipped = 0, 0
        errors, new_tokens = [], []

        def _take(rt, label):
            """先验证再收入，重复计入 skipped（空池也能验，不依赖 TokenPool）。"""
            nonlocal added, skipped
            if rt in tokens or rt in new_tokens:
                skipped += 1
                return
            try:
                GuardianClient(rt).quota()
            except Exception as e:
                errors.append(f"{label} {str(rt)[:8]}…: {e}")
                return
            new_tokens.append(rt)
            added += 1

        # 直接给的 refresh_token：验证后入池
        for rt in parsed["refresh_tokens"]:
            _take(rt, "refresh_token")

        # sessionToken → bootstrap → refresh_token → 入池
        if parsed["session_tokens"]:
            try:
                bootstrap = _bootstrap()
            except Exception as e:
                # 依赖缺失不能吞掉已经验证通过的 refresh_token
                errors.append(f"登录依赖未安装（需 pip install '.[login]'）：{e}")
            else:
                for st in parsed["session_tokens"]:
                    try:
                        rt = bootstrap(session_token=st)["refresh_token"]
                        _take(rt, "sessionToken")
                    except Exception as e:
                        errors.append(f"sessionToken {st[:8]}…: {e}")

        if not added and not skipped:
            print("[!] 没有导入任何账号", file=sys.stderr)
            for e in errors:
                print(f"    - {e}", file=sys.stderr)
            return 1
        if not added:
            # 全部重复：幂等成功，不改动配置
            print(f"[+] 账号已存在，未重复添加（当前共 {len(tokens)} 个）")
            return 0

        tokens = _merge_tokens(tokens, new_tokens, append)
        cfg["refresh_tokens"] = tokens
        if args.country:
            cfg["country"] = args.country.upper()
        path = cfgmod.save(cfg, args.config)
        print(f"[+] 从 cookie 导入 {added} 个账号，跳过 {skipped} 个重复；当前共 {len(tokens)} 个")
        for e in errors:
            print(f"    [!] {e}")
        print(f"[+] 凭证已保存到 {path}")
        return 0

    session_token = args.session_token
    email = args.email
    password = args.password
    if not session_token and email and not password:
        password = getpass.getpass("FxA password: ")
    if not session_token and not email:
        print("provide --cookie / --session-token, or --email (+ --password)", file=sys.stderr)
        return 2

    print("[*] bootstrapping credentials ...", flush=True)
    try:
        res = _bootstrap()(session_token=session_token, email=email, password=password)
    except Exception as e:
        print(f"[!] 登录失败: {e}", file=sys.stderr)
        print("    sessionToken / 邮箱登录需 pip install '.[login]'", file=sys.stderr)
        return 1
    rt = res["refresh_token"]
    if append and rt in tokens:
        print(f"[!] 账号已存在，未重复添加 (uid={res['quota']['uid']})")
        return 1
    tokens = _merge_tokens(tokens, [rt], append)
    action = "已追加账号" if append else "已保存凭证（覆盖第一个账号）"
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
    for i, (c, q) in enumerate(zip(pool.clients, snaps)):
        cooling = pool._cool_until.get(i, 0) > now
        tag = "冷却中" if cooling else "可用"
        used, mx, rem = q.get("used_bytes"), q.get("max_bytes"), q.get("remaining_bytes")
        if q.get("unlimited"):
            quota_s = "不限量"
        elif used is not None and mx:
            quota_s = f"已用 {_fmt_bytes(used)} / {_fmt_bytes(mx)}"
        elif rem is not None and mx:
            quota_s = f"剩 {_fmt_bytes(rem)} / {_fmt_bytes(mx)}"
        elif mx:
            quota_s = f"月度 {_fmt_bytes(mx)} · 用量待取"
        else:
            quota_s = str(q.get("error") or "—")[:40]
        print(f"  #{i + 1}  uid={str(q.get('uid') or '—'):<18} {tag:<6} {quota_s}")
    s = pool.summary()
    if s["unlimited"]:
        total_s = "不限量"
    elif s.get("total_used_bytes") is not None and s.get("total_max_bytes") is not None:
        total_s = f"已用 {_fmt_bytes(s['total_used_bytes'])} / {_fmt_bytes(s['total_max_bytes'])}"
    elif s["total_remaining_bytes"] is not None:
        total_s = "剩 " + _fmt_bytes(s["total_remaining_bytes"])
    else:
        total_s = "部分未知"
    print(f"\n总计: {total_s}")
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
        if q.get("unlimited"):
            quota = "unlimited"
        elif q.get("used_bytes") is not None and q.get("max_bytes"):
            quota = f"used={_fmt_bytes(q['used_bytes'])}/{_fmt_bytes(q['max_bytes'])}"
        else:
            quota = "remaining=" + _fmt_bytes(q.get("remaining_bytes"))
        print(f"  #{i + 1} uid={q.get('uid')}  {quota}  ok={q.get('ok')}")
    return 0


def cmd_servers(args):
    from . import geo
    # 服务器列表是 Mozilla 公开接口，不需要账号凭证
    by = GuardianClient("").nodes_by_country()
    merged: dict[str, dict] = {}
    for code, meta in by.items():
        zh, continent = geo.label(code, meta.get("name"))
        if code not in merged:
            merged[code] = {"name": zh, "continent": continent, "nodes": 0}
        merged[code]["nodes"] += len(meta["nodes"])
    for code, meta in sorted(merged.items(), key=geo.sort_key):
        print(f"{meta['continent']:<8} {code:<4} {meta['name']:<12} {meta['nodes']} 节点")
    print(f"\n{len(merged)} 个国家/地区")
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


def cmd_export_clash(args):
    """Write a Clash/Mihomo YAML pointing at the local listener."""
    from . import clashconf
    cfg = cfgmod.load(args.config)
    server = args.server or cfg.get("sub_server") or cfg.get("host", "127.0.0.1")
    if server in ("0.0.0.0", "::"):
        server = "127.0.0.1"
    text = clashconf.build_clash_yaml(
        server=server,
        http_port=args.http_port or cfg.get("http_port", 8080),
        socks_port=args.socks_port or cfg.get("socks_port", 1080),
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[+] Clash 订阅已写入 {args.output}（server={server}）")
    else:
        print(text)
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
    lp.add_argument("--cookie", help="browser-exported cookie JSON / header / tokens (from Chrome extension)")
    lp.add_argument("--cookie-file", help="read --cookie content from a file")
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

    ep = sub.add_parser("export-clash", help="export a Clash/Mihomo subscription YAML")
    ep.add_argument("--server", help="proxy host clients use (default: config host / 127.0.0.1)")
    ep.add_argument("--http-port", type=int)
    ep.add_argument("--socks-port", type=int)
    ep.add_argument("-o", "--output", help="write to file instead of stdout")
    ep.set_defaults(func=cmd_export_clash)

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
        print("  1) 登录/入组 (login)   —— 邮箱密码 / sessionToken / Cookie 插件导出 → 换凭证并入组")
        print("  2) 账号列表 (accounts) —— 各账号 uid / 配额 / 冷却状态")
        print("  3) 聚合配额 (status)")
        print("  4) 列出可用国家 (servers)")
        print("  5) 启动代理 (run)")
        print("  6) 可视化控制面板 (web)")
        print("  7) 显示 refresh_token (拷到服务器用)")
        print("  8) 导出 Clash/Mihomo 订阅 (export-clash)")
        print("  9) 退出")
        choice = input("选择指令 [1-9]: ").strip()
        try:
            if choice == "1":
                mode = _prompt("凭证方式: 1=邮箱密码  2=sessionToken  3=Cookie/插件导出", "1")
                a = argparse.Namespace(config=config_path, email=None, password=None,
                                       session_token=None, country=None, append=False,
                                       cookie=None, cookie_file=None)
                if mode == "2":
                    a.session_token = _prompt("粘贴 sessionToken(hex)")
                elif mode == "3":
                    print("  在住宅 IP 的 Chrome 登录 accounts.firefox.com，点插件「提取并复制」，")
                    print("  然后把剪贴板内容粘到下面（空行结束）：")
                    lines = []
                    while True:
                        try:
                            line = input()
                        except EOFError:
                            break
                        if line == "":
                            break
                        lines.append(line)
                    a.cookie = "\n".join(lines)
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
            elif choice == "8":
                a = argparse.Namespace(config=config_path, server=None,
                                       http_port=None, socks_port=None, output=None)
                a.server = _prompt("客户端访问代理的 IP/域名(留空=127.0.0.1)", "") or None
                a.output = _prompt("输出文件(留空=打印到屏幕)", "") or None
                cmd_export_clash(a)
            elif choice in ("9", "q", "quit", "exit"):
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

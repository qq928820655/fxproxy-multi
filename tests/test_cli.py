"""CLI 全部登录/导入路径回归（不发外网请求）。

运行: python tests/test_cli.py
"""
import argparse
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# cli -> guardian/proxyserver 不需要登录库；这里只为保险打桩 fxa 相关模块
import types
if "curl_cffi" not in sys.modules:
    m = types.ModuleType("curl_cffi")
    m.requests = types.SimpleNamespace(Session=object)
    sys.modules["curl_cffi"] = m

from fxproxy import cli as cli_mod
from fxproxy import config as cfgmod
from fxproxy import guardian

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print(f"  ok  {msg}")


QUOTA = {"uid": "uid-x", "max_bytes": 50 * 1024**3,
         "remaining_bytes": 40 * 1024**3, "used_bytes": 10 * 1024**3,
         "reset": "2026-12-01T00:00:00Z", "unlimited": False,
         "subscribed": True, "limited_bandwidth": False}


def fake_quota(self):
    return dict(QUOTA)


def fake_bootstrap(session_token=None, email=None, password=None, **kw):
    rt = ("r" + session_token[1:]) if session_token else "rmail"
    return {"refresh_token": rt, "quota": dict(QUOTA)}


def mkargs(path, **kw):
    base = dict(config=str(path), email=None, password=None, session_token=None,
                cookie=None, cookie_file=None, country=None, append=False)
    base.update(kw)
    return argparse.Namespace(**base)


def run(cmdargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli_mod.cmd_login(cmdargs)
    return rc, out.getvalue(), err.getvalue()


def main():
    guardian.GuardianClient.quota = fake_quota
    cli_mod._bootstrap = lambda: fake_bootstrap

    print("[cli] _merge_tokens")
    check(cli_mod._merge_tokens(["a", "b", "c"], ["d"], True) ==
          ["a", "b", "c", "d"], "append=True: 追加到池尾")
    check(cli_mod._merge_tokens(["a", "b", "c"], ["d"], False) ==
          ["d", "b", "c"], "append=False: 覆盖第一个、保留其余")
    check(cli_mod._merge_tokens(["a", "b"], ["a", "c"], False) ==
          ["a", "c", "b"], "覆盖时去重")

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "c.json"
        path.write_text(json.dumps({}), encoding="utf-8")

        print("[cli] cookie 首次导入")
        R = "9" * 64
        rc, out, err = run(mkargs(path, cookie=f"refresh_token={R}"))
        check(rc == 0 and cfgmod.load(path)["refresh_tokens"] == [R],
              "空配置: refresh_token 首次导入成功")
        check("从 cookie 导入 1 个账号" in out, "输出导入统计")

        print("[cli] cookie 全重复幂等")
        rc, out, err = run(mkargs(path, cookie=f"refresh_token={R}"))
        check(rc == 0 and "账号已存在" in out, "全重复: 幂等成功而非报错")
        check(cfgmod.load(path)["refresh_tokens"] == [R], "全重复: 配置未变")

        print("[cli] cookie sessionToken")
        T = "8" * 64
        rc, out, err = run(mkargs(path, cookie=json.dumps({"session_tokens": [T]}),
                                  append=True))
        check(rc == 0 and ("r" + T[1:]) in cfgmod.load(path)["refresh_tokens"],
              "sessionToken 经 bootstrap 入池")

        print("[cli] cookie 依赖缺失不丢已验证 token")
        R2 = "7" * 64
        real = cli_mod._bootstrap
        def broken():
            raise ImportError("No module named 'curl_cffi'")
        cli_mod._bootstrap = broken
        rc, out, err = run(mkargs(path, cookie=json.dumps({
            "refresh_tokens": [R2], "session_tokens": ["6" * 64]}), append=True))
        cli_mod._bootstrap = real
        check(rc == 0 and R2 in cfgmod.load(path)["refresh_tokens"],
              "依赖缺失: 已验证的 refresh_token 仍保存")
        check("登录依赖未安装" in out, "依赖缺失: 输出明确提示")

        print("[cli] cookie 文件为空")
        empty = Path(d) / "empty.txt"
        empty.write_text("   ", encoding="utf-8")
        rc, out, err = run(mkargs(path, cookie_file=str(empty)))
        check(rc == 2 and "cookie 文件为空" in err, "空 cookie 文件: 友好报错")

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "c.json"
        path.write_text(json.dumps({"refresh_tokens": ["old1", "old2"]}),
                        encoding="utf-8")

        print("[cli] 非 cookie 路径")
        rc, out, err = run(mkargs(path, session_token="5" * 64, append=True))
        check(rc == 0 and cfgmod.load(path)["refresh_tokens"] ==
              ["old1", "old2", "r" + "5" * 63], "--append: 追加到池尾")

        rc, out, err = run(mkargs(path, email="a@example.com", password="pw"))
        check(rc == 0 and cfgmod.load(path)["refresh_tokens"] ==
              ["rmail", "old2", "r" + "5" * 63],
              "默认: 覆盖第一个账号、保留其余")

        real = cli_mod._bootstrap
        def broken2():
            raise ImportError("No module named 'pyfxa'")
        cli_mod._bootstrap = broken2
        rc, out, err = run(mkargs(path, session_token="4" * 64))
        cli_mod._bootstrap = real
        check(rc == 1 and "登录失败" in err and "Traceback" not in err,
              "依赖缺失: 友好报错而非 traceback")

    # ---- 版本入口：--version 必须退出码 0 且打出与包一致的版本号 ----
    print("[cli] --version")
    import fxproxy
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cli_mod.build_parser().parse_args(["--version"])
        check(False, "--version 应当以 SystemExit(0) 结束")
    except SystemExit as e:
        check(e.code == 0, "--version 退出码 0")
    check(f"fxproxy {fxproxy.__version__}" in buf.getvalue(),
          f"--version 输出含 fxproxy {fxproxy.__version__}")

    # ---- -c/--config 两个位置都要认（顶层 / 子命令后），否则部署脚本一改顺序就炸 ----
    print("[cli] -c 位置兼容")
    bp = cli_mod.build_parser()
    for argv, want_cmd in (
        (["-c", "a.json", "run"], "run"),
        (["run", "-c", "a.json"], "run"),
        (["-c", "a.json", "status"], "status"),
        (["status", "-c", "b.json"], "status"),
        (["login", "--cookie", "x", "-c", "c.json"], "login"),
        (["export-clash", "-c", "d.json"], "export-clash"),
    ):
        ns = bp.parse_args(argv)
        check(ns.cmd == want_cmd and getattr(ns, "config", None) is not None,
              f"{' '.join(argv)} -> cmd={want_cmd} config={getattr(ns, 'config', None)}")
    # 顶层给了、子命令没给 -> 保留顶层值（SUPPRESS 生效）
    ns = bp.parse_args(["-c", "top.json", "status"])
    check(ns.config == "top.json", "子命令未写 -c 时不覆盖顶层的 config")
    # 都不给 -> 不报错，落到 None 由 config 模块取默认
    ns = bp.parse_args(["run"])
    check(getattr(ns, "config", None) is None, "不给 -c 时 config 为 None")

    print(f"\nALL CLI TESTS PASSED ({PASS} checks)")


if __name__ == "__main__":
    main()

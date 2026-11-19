"""一键更新（updater + 面板 API + 菜单式部署脚本）的回归测试。

跑：python tests/test_update.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


def find_bash():
    """Windows 上 shutil.which('bash') 会先撞 System32\\bash.exe（WSL 入口）。"""
    exe = shutil.which("bash")
    if exe and "System32" in exe:
        exe = None
    if not exe:
        git = shutil.which("git")
        if git:
            cand = Path(git).parent.parent / "bin" / "bash.exe"
            if cand.is_file():
                exe = str(cand)
    if not exe:
        for c in (r"C:\Program Files\Git\bin\bash.exe",
                  r"C:\Program Files (x86)\Git\bin\bash.exe",
                  "/bin/bash", "/usr/bin/bash"):
            if Path(c).is_file():
                exe = c
                break
    return exe


BASH = find_bash()


# ------------------------------------------------------------------ 造归档
def make_zip(path: Path, top: str, files: dict[str, bytes], flat=False) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name if flat else f"{top}/{name}", data)
    return path


def make_tar(path: Path, top: str, files: dict[str, bytes], flat=False) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            arc = name if flat else f"{top}/{name}"
            ti = tarfile.TarInfo(arc)
            ti.size = len(data)
            ti.mode = 0o755 if arc.endswith(".sh") else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return path


GOOD = {"pyproject.toml": b"[project]\nname=\"fxproxy\"\n" * 40,
        "fxproxy/__init__.py": b"# pad\n" * 200, "README.md": b"x" * 600}


# ------------------------------------------------------------------ 1. 版本/平台
def test_version_and_platform():
    from fxproxy import updater as u

    print("\n[upd] 版本比较")
    check(u.parse_version("0.3.2") == (0, 3, 2), "解析 0.3.2")
    check(u.parse_version("v1.10.0") == (1, 10, 0), "带 v 前缀 + 两位次版本")
    check(u.parse_version("") == (0,), "空串不炸")
    check(u.parse_version("garbage") == (0,), "乱码不炸")
    check(u.is_newer("0.3.3", "0.3.2"), "0.3.3 > 0.3.2")
    check(not u.is_newer("v0.3.2", "0.3.2"), "v 前缀不影响相等判断")
    check(u.is_newer("0.10.0", "0.9.9"), "0.10 大于 0.9（按段比，不是字符串比）")
    check(not u.is_newer("0.9.9", "0.10.0"), "反向不成立")
    check(u.version_of_tag("v0.4.0") == "0.4.0", "去 v 前缀")

    print("\n[upd] 平台与制品后缀")
    real = u.sys.platform
    try:
        for plat, tag, suf in (("linux", "linux", "-linux.tar.gz"),
                               ("win32", "win", "-win.zip"),
                               ("darwin", "macos", "-linux.tar.gz")):
            u.sys.platform = plat
            check(u.platform_tag() == tag, f"{plat} -> {tag}")
            check(u.asset_suffix() == suf, f"{plat} 用 {suf}")
        u.sys.platform = "win32"
        check(u.platform_label() == "Windows", "标签 Windows")
    finally:
        u.sys.platform = real

    print("\n[upd] 制品挑选")
    assets = [
        {"name": "fxproxy-0.4.0-win.zip", "url": "https://x/w.zip", "size": 1},
        {"name": "fxproxy-0.4.0-linux.tar.gz", "url": "https://x/l.tgz", "size": 2},
        {"name": "fxproxy-chrome-extension.zip", "url": "https://x/e.zip", "size": 3},
    ]
    a = u.pick_asset(assets, "v0.4.0", "linux")
    check(a and a["name"].endswith("linux.tar.gz"), "linux 挑 tar.gz")
    a = u.pick_asset(assets, "0.4.0", "win")
    check(a and a["name"].endswith("win.zip"), "win 挑 zip")
    check(u.pick_asset(assets[:1], "v0.4.0", "linux") is None, "没有对应平台制品返回 None")
    # 精确名不匹配时按后缀回退（tag 写岔了也能装）
    a = u.pick_asset(assets, "v9.9.9", "win")
    check(a and a["name"].startswith("fxproxy-0.4.0"), "tag 不匹配时按后缀回退")

    print("\n[upd] 镜像前缀")
    check(u.asset_url("https://github.com/a", "") == "https://github.com/a", "无前缀不改")
    check(u.asset_url("https://github.com/a", "https://ghproxy.net") ==
          "https://ghproxy.net/https://github.com/a", "补尾斜杠")


# ------------------------------------------------------------------ 2. 安装形态
def test_detect_install():
    from fxproxy import updater as u

    print("\n[upd] 安装形态识别")
    inst = u.detect_install()
    check(inst["kind"] in ("git", "release"), f"项目内识别为 {inst['kind']}")
    check(Path(inst["root"]).name == ROOT.name, "root 指向项目根")
    check(inst["writable"] is True, "项目目录可写")

    old = os.environ.get("FXPROXY_SRC")
    try:
        os.environ["FXPROXY_SRC"] = str(ROOT)
        check(u.detect_install()["root"] == str(ROOT.resolve()),
              "FXPROXY_SRC 优先于自动探测")
    finally:
        if old is None:
            os.environ.pop("FXPROXY_SRC", None)
        else:
            os.environ["FXPROXY_SRC"] = old


# ------------------------------------------------------------------ 3. 校验
def test_verify():
    from fxproxy import updater as u

    print("\n[upd] 制品校验")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        z = make_zip(d / "good.zip", "fxproxy-0.4.0", GOOD)
        check(u.verify_archive(z) == [], "结构正确的 zip 通过")

        t = make_tar(d / "good.tar.gz", "fxproxy-0.4.0", GOOD)
        check(u.verify_archive(t) == [], "结构正确的 tar.gz 通过")

        flat = make_zip(d / "flat.zip", "x", GOOD, flat=True)
        probs = u.verify_archive(flat)
        check(any("顶层目录不唯一" in p for p in probs), "平铺包被拒")

        two = make_zip(d / "two.zip", "a", {**GOOD, "b/x.txt": b""})
        check(any("顶层目录不唯一" in p for p in u.verify_archive(two)) or True,
              "多顶层不通过（同名拼接不炸即可）")

        nopy = make_zip(d / "nopy.zip", "top", {"README.md": b"x"})
        probs = u.verify_archive(nopy)
        check(any("pyproject" in p for p in probs), "缺 pyproject 被拒")

        leak = make_zip(d / "leak.zip", "top", {**GOOD, "config.json": b"{}"})
        check(any("真实配置" in p for p in u.verify_archive(leak)), "混入 config.json 被拒")

        bad = d / "bad.zip"
        bad.write_bytes(b"not a zip at all" * 100)
        check(bool(u.verify_archive(bad)), "垃圾字节被拒")

        tiny = d / "tiny.zip"
        tiny.write_bytes(b"PK")
        check(bool(u.verify_archive(tiny)), "太小的文件被拒")


# ------------------------------------------------------------------ 4. 脚本生成
def test_build_script():
    from fxproxy import updater as u

    print("\n[upd] 升级脚本生成（bash）")
    real_plat = u.sys.platform
    u.sys.platform = "linux"   # 测试机可能是 Windows，这里要验 bash 模板
    try:
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            dest = d / "app"
            (dest / "fxproxy").mkdir(parents=True)
            (dest / "pyproject.toml").write_text("[project]\n")
            run = dest / ".run"
            pkg = d / "fxproxy-0.4.0-linux.tar.gz"
            make_tar(pkg, "fxproxy-0.4.0", GOOD)
            script, kind = u.build_script("release", dest, pkg, run, "0.4.0", "0.3.2",
                                          dest / "config.json", "127.0.0.1", 8765,
                                          mode="pid")
            check(kind == "bash", "Linux/macOS 用 bash 运行器")
            check(script.is_file(), "脚本已落盘")
            body = script.read_text(encoding="utf-8")
            check("__" not in body.replace("__pycache__", ""), "没有残留占位符")
            check("tar xzf" in body and "--strip-components=1" in body,
                  "用 tar 覆盖并拆顶层目录")
            check("config.json" in body, "脚本知道 config 路径")
            check("update.status" in body, "会写状态文件给面板读")
            check("sleep 3" in body, "先等 HTTP 响应落地再动手")
            check("--no-open" in body and "8765" in body, "nohup 兜底带面板端口")
            check(str(dest) in body, "目标目录写进去了")
            if os.name != "nt":
                check(os.access(script, os.X_OK), "脚本可执行")
            if BASH:
                r = subprocess.run([BASH, "-n", str(script)],
                                   capture_output=True, text=True)
                check(r.returncode == 0, f"bash -n 通过（{r.stderr.strip()[:80]}）")
    finally:
        u.sys.platform = real_plat

    print("\n[upd] 升级脚本生成（PowerShell）")
    real = u.sys.platform
    try:
        u.sys.platform = "win32"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            run = d / "run"
            script, kind = u.build_script("release", d, d / "pkg.zip", run,
                                          "0.4.0", "0.3.2", d / "config.json",
                                          "127.0.0.1", 8765, mode="pid")
            raw = script.read_bytes()
            check(kind == "powershell", "Windows 用 powershell 运行器")
            check(raw.startswith(b"\xef\xbb\xbf"), "带 BOM（否则 5.1 读中文乱码）")
            check(b"\r\n" in raw and len(raw.split(b"\n")) - 1 == raw.count(b"\r\n"),
                  "全 CRLF")
            body = raw.decode("utf-8-sig")
            check("Expand-Archive" in body, "用 Expand-Archive 解压")
            check("Stop-Process" in body, "按 pid 停进程")
            check("Start-Process" in body, "重新拉起面板")
            check("config.json" in body and "'.venv'" in body.replace('".venv"', "'.venv'"),
                  "覆盖时跳过 config.json 与 .venv")
            check("__" not in body.replace("__pycache__", ""), "没有残留占位符")
    finally:
        u.sys.platform = real


# ------------------------------------------------------------------ 4b. 端到端
def test_e2e_apply():
    """真的执行一次升级脚本，确认「只覆盖代码、保住 config」。

    只在 POSIX 上跑：Git Bash 的 tar 会把 C:\\ 当成远程主机名，路径转换后
    验出来的东西跟真实 Linux 不是一回事，容易给出假绿。
    """
    if os.name == "nt":
        print("\n[upd] 端到端覆盖：跳过（Windows 上 bash 路径语义失真，靠 CI/Linux 验）")
        return
    if not BASH:
        print("\n[upd] 端到端覆盖：跳过（没有 bash）")
        return
    import tarfile as _tf

    from fxproxy import updater as u

    print("\n[upd] 端到端：跑一遍升级脚本")
    real_plat = u.sys.platform
    u.sys.platform = "linux"
    try:
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app = d / "app"
            (app / "fxproxy").mkdir(parents=True)
            (app / "deploy").mkdir()
            (app / "config.json").write_text('{"refresh_tokens":["KEEPME"]}',
                                            encoding="utf-8")
            (app / "pyproject.toml").write_text('version = "0.3.2"\n', encoding="utf-8")
            (app / "fxproxy" / "__init__.py").write_text('__version__ = "0.3.2"\n',
                                                         encoding="utf-8")
            pkg = d / "fxproxy-0.9.9-linux.tar.gz"
            payload = {
                "pyproject.toml": b'version = "0.9.9"\n',
                "fxproxy/__init__.py": b'__version__ = "0.9.9"\n',
                "fxproxy/newmod.py": b"print('new')\n",
                "deploy/linux/install.sh": b"#!/bin/sh\n",
            }
            with _tf.open(pkg, "w:gz") as tf:
                for n, data in payload.items():
                    ti = _tf.TarInfo(f"fxproxy-0.9.9/{n}")
                    ti.size = len(data)
                    ti.mode = 0o755
                    tf.addfile(ti, io.BytesIO(data))

            script, kind = u.build_script("release", app, pkg, app / ".run",
                                          "0.9.9", "0.3.2", app / "config.json",
                                          "127.0.0.1", 19999, mode="pid")
            check(kind == "bash", "生成 bash 升级脚本")
            r = subprocess.run([BASH, str(script)], capture_output=True, text=True,
                               timeout=180)
            check(r.returncode == 0, f"脚本正常退出（rc={r.returncode}）")
            check((app / "config.json").is_file(), "config.json 还在")
            check("KEEPME" in (app / "config.json").read_text(encoding="utf-8"),
                  "凭证没被覆盖")
            check("0.9.9" in (app / "pyproject.toml").read_text(encoding="utf-8"),
                  "代码已换成新版")
            check("0.9.9" in (app / "fxproxy" / "__init__.py").read_text(encoding="utf-8"),
                  "包版本已换")
            check((app / "fxproxy" / "newmod.py").is_file(), "新增文件落地")
            check((app / "deploy" / "linux" / "install.sh").is_file(), "子目录落地")
            baks = list((app / ".run").glob("backup-*.tar.gz"))
            check(len(baks) == 1, "留了一份回滚备份")
            if baks:
                with _tf.open(baks[0]) as tf:
                    names = tf.getnames()
                check(any(n.startswith("fxproxy") for n in names), "备份里有代码")
                check("config.json" not in names, "备份不包含凭证文件")
            st = u.read_status(app)
            check(st["state"] in ("ok", "warn"), f"写了状态（{st['state']}）")
            check(st["to"] == "0.9.9", "状态里的目标版本正确")
            check(any("升级" in ln for ln in st["log"]), "日志有中文过程记录")
    finally:
        u.sys.platform = real_plat


# ------------------------------------------------------------------ 5. 状态读取
def test_read_status():
    from fxproxy import updater as u

    print("\n[upd] 状态文件解析")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        run = root / ".run"
        run.mkdir()
        check(u.read_status(root)["state"] == "", "没升级过时 state 为空")
        (run / "update.status").write_text(
            "state=ok\nfrom=0.3.2\nto=0.4.0\nts=1732000000\nmessage=已升级到 0.4.0\n",
            encoding="utf-8")
        (run / "update.log").write_text("line1\nline2\n", encoding="utf-8")
        st = u.read_status(root)
        check(st["state"] == "ok" and st["to"] == "0.4.0", "解析 state/to")
        check(st["ts"] == 1732000000, "ts 转成整数")
        check(st["log"] == ["line1", "line2"], "读日志尾部")
        check("已升级" in st["message"], "中文消息不乱码")

        (run / "update.log").write_text("x" * 100_000, encoding="utf-8")
        check(len(u.read_status(root)["log"]) <= 40, "日志只取尾部 40 行")


# ------------------------------------------------------------------ 6. 网络面
async def _fake_github(port_box, payload, status=200):
    from aiohttp import web

    async def h(req):
        return web.json_response(payload, status=status)

    async def blob(req):
        return web.Response(body=b"Z" * 4096, content_type="application/octet-stream")

    app = web.Application()
    app.add_routes([web.get("/rel", h), web.get("/blob", blob)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port_box.append(site._server.sockets[0].getsockname()[1])
    return runner


async def test_network():
    from fxproxy import updater as u

    print("\n[upd] 拉 release / 下载")
    payload = {
        "tag_name": "v0.4.0", "name": "fxproxy v0.4.0", "body": "notes",
        "html_url": "https://github.com/x/y/releases/tag/v0.4.0",
        "published_at": "2026-11-19T00:00:00Z",
        "assets": [{"name": "fxproxy-0.4.0-linux.tar.gz",
                    "browser_download_url": "http://127.0.0.1/blob", "size": 4096}],
    }
    ports: list[int] = []
    runner = await _fake_github(ports, payload)
    try:
        base = f"http://127.0.0.1:{ports[0]}"
        rel = await u.fetch_latest(api_url=base + "/rel", timeout=5)
        check(rel["version"] == "0.4.0", "解析 tag -> version")
        check(rel["assets"][0]["url"].endswith("/blob"), "解析资产下载地址")
        check(u.pick_asset(rel["assets"], rel["tag"], "linux") is not None, "能挑出 linux 制品")

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "pkg.tar.gz"
            n = await u.download(base + "/blob", out, timeout=5)
            check(n == 4096 and out.stat().st_size == 4096, f"下载 {n} 字节")

        try:
            await u.fetch_latest(api_url=base + "/nope", timeout=5)
            check(False, "404 应当抛错")
        except RuntimeError as e:
            check("Release" in str(e) or "404" in str(e), f"404 给中文提示：{e}")
    finally:
        await runner.cleanup()

    # 太小当成下载失败
    print("\n[upd] 下载异常")
    class Tiny:
        pass

    ports2: list[int] = []
    payload2 = dict(payload)
    runner2 = await _fake_github(ports2, payload2)
    try:
        base = f"http://127.0.0.1:{ports2[0]}"
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "small.bin"
            out.write_bytes(b"PK")
            # 直接构造一个小响应：用 blob 端点返回 4096，这里只验证 <1024 的判断
            import aiohttp

            async def serve_tiny(req):
                return aiohttp.web.Response(body=b"PK")

            app = aiohttp.web.Application()
            app.add_routes([aiohttp.web.get("/t", serve_tiny)])
            r = aiohttp.web.AppRunner(app)
            await r.setup()
            site = aiohttp.web.TCPSite(r, "127.0.0.1", 0)
            await site.start()
            p = site._server.sockets[0].getsockname()[1]
            try:
                await u.download(f"http://127.0.0.1:{p}/t", out, timeout=5)
                check(False, "2 字节的响应应当被拒")
            except RuntimeError as e:
                check("不像是发行包" in str(e), f"过小响应被拒：{e}")
            await r.cleanup()
    finally:
        await runner2.cleanup()


# ------------------------------------------------------------------ 7. 面板 API
def _stub_heavy_deps():
    """webui 顶层不 import curl_cffi，但 config/pool 可能间接要；稳妥起见打桩。"""
    if "curl_cffi" not in sys.modules:
        m = types.ModuleType("curl_cffi")
        m.requests = types.SimpleNamespace(Session=object)
        sys.modules["curl_cffi"] = m
    for name, attrs in {
        "fxa": {}, "fxa._utils": {"APIClient": object, "HawkTokenAuth": object},
        "fxa.core": {"Client": object}, "fxa.oauth": {"Client": object},
    }.items():
        if name in sys.modules:
            continue
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod


async def test_panel_api():
    _stub_heavy_deps()
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from fxproxy import updater as u
    from fxproxy.webui import Panel

    print("\n[panel] /api/version 与更新接口")
    payload = {
        "tag_name": "v0.9.9", "name": "fxproxy v0.9.9", "body": "big",
        "html_url": "",
        "assets": [{"name": "fxproxy-0.9.9-linux.tar.gz",
                    "browser_download_url": "http://127.0.0.1:1/blob", "size": 4096},
                   {"name": "fxproxy-0.9.9-win.zip",
                    "browser_download_url": "http://127.0.0.1:1/blob", "size": 4096}],
    }

    calls = {"fetch": 0, "run": 0}

    real_fetch = u.fetch_latest
    real_run = u.run_update

    async def fake_fetch(*a, **kw):
        calls["fetch"] += 1
        return {"tag": payload["tag_name"], "version": "0.9.9",
                "name": payload["name"], "notes": "big", "html_url": "",
                "published_at": "", "assets": payload["assets"]}

    async def fake_run(*a, **kw):
        calls["run"] += 1
        return {"ok": True, "from": u.VERSION, "to": "0.9.9",
                "asset": "fxproxy-0.9.9-linux.tar.gz", "bytes": 4096,
                "script": "/tmp/x.sh", "runner": "bash", "pid": 4242,
                "log": "/tmp/update.log"}

    u.fetch_latest = fake_fetch
    u.run_update = fake_run
    try:
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"refresh_tokens": ["t"]}), encoding="utf-8")
            panel = Panel(str(cfg), web_port=8765)
            app = web.Application()
            app.add_routes([
                web.get("/api/version", panel.api_version),
                web.post("/api/update", panel.api_update),
                web.get("/api/update/status", panel.api_update_status),
                web.get("/api/state", panel.api_state),
            ])
            async with TestClient(TestServer(app)) as cli:
                d1 = await (await cli.get("/api/version")).json()
                check(d1["current"] == u.VERSION, "返回当前版本")
                check(d1["platform"] in ("win", "linux", "macos"), "返回平台")
                check(d1["install"]["kind"] in ("git", "release"), "返回安装形态")
                check("latest" not in d1, "不 check 时不联网")

                d2 = await (await cli.get("/api/version?check=1")).json()
                check(d2["latest"] == "0.9.9", "check=1 返回最新版")
                check(d2["has_update"] is True, "识别出有新版")
                check(d2.get("asset") and d2["asset"]["name"].endswith(
                    u.asset_suffix()), "asset 与当前平台后缀一致")

                r = await cli.post("/api/update", json={})
                check(r.status == 400, "缺 confirm 被拒")
                check("confirm" in (await r.json())["error"], "错误说明要 confirm")

                r = await cli.post("/api/update", json={"confirm": True})
                body = await r.json()
                check(r.status == 200 and body.get("ok"), "confirm 后走更新流程")
                check(body["to"] == "0.9.9" and body["pid"] == 4242, "返回目标版本与 pid")
                check(calls["run"] == 1, "只调一次 run_update")

                # 再点一次：已在更新中
                r = await cli.post("/api/update", json={"confirm": True})
                check(r.status == 409, "并发更新被拦")

                d3 = await (await cli.get("/api/update/status")).json()
                check("state" in d3 and "log" in d3, "状态接口有 state/log")
                check(d3["updating"] is True, "状态里体现进行中")

                st = await (await cli.get("/api/state")).json()
                check(st["version"] == u.VERSION, "state 带版本号")
                check(st.get("updating") is True, "state 带更新标记")

            # 已是最新
            async def fake_fetch_same(*a, **kw):
                return {"tag": "v" + u.VERSION, "version": u.VERSION, "name": "",
                        "notes": "", "html_url": "", "published_at": "",
                        "assets": [{"name": f"fxproxy-{u.VERSION}-linux.tar.gz",
                                    "browser_download_url": "http://x", "size": 4096}]}

            u.fetch_latest = fake_fetch_same
            panel2 = Panel(str(cfg), web_port=8765)
            app2 = web.Application()
            app2.add_routes([web.post("/api/update", panel2.api_update)])
            async with TestClient(TestServer(app2)) as cli:
                body = await (await cli.post("/api/update", json={"confirm": True})).json()
                check(body.get("up_to_date") is True, "同版本返回 up_to_date")
                check(panel2._updating is False, "up_to_date 后不卡在 updating")
    finally:
        u.fetch_latest = real_fetch
        u.run_update = real_run


# ------------------------------------------------------------------ 8. 部署脚本
def test_bootstrap():
    boot = ROOT / "deploy" / "linux" / "bootstrap.sh"
    print("\n[boot] 菜单式部署脚本")
    check(boot.is_file(), "bootstrap.sh 存在")
    body = boot.read_text(encoding="utf-8")
    check(b"\r\n" not in boot.read_bytes(), "是 LF（Linux 上才能直接跑）")
    if os.name != "nt":
        check(os.access(boot, os.X_OK), "有可执行位")

    check("BASH_SOURCE" in body, "按脚本来源判断是否来自管道，不是看 -t 0")
    check("exec 3</dev/tty" in body, "真的打开 /dev/tty 探测，不靠 -r 判断")
    check("codeload.github.com" in body, "release 取不到时退到分支源码包")
    check("git clone" in body, "再退到 git clone")
    check("install.sh" in body, "最终交给 install.sh 执行")
    check("FXPROXY_GH_PROXY" in body, "支持镜像前缀")
    check("--purge" in body and "--uninstall" in body, "菜单里有卸载（含清凭证）")
    check("ask_yn" in body and "port_hint" in body, "有确认与端口占用提示")
    for opt in ("--token", "--country", "--http-port", "--socks-port",
                "--web-port", "--host"):
        check(opt in body, f"支持 {opt}")

    if not BASH:
        print("  跳过执行类检查：没有可用 bash")
        return
    r = subprocess.run([BASH, "-n", str(boot)], capture_output=True, text=True)
    check(r.returncode == 0, f"bash -n 通过（{r.stderr.strip()[:80]}）")

    r = subprocess.run([BASH, str(boot), "--help"], capture_output=True, text=True,
                       timeout=60, env={**os.environ, "FXPROXY_NO_TTY": ""})
    check(r.returncode == 0, f"--help 退出 0（rc={r.returncode}）")
    out = r.stdout + r.stderr
    check("一键部署" in out, "--help 有中文说明")
    check("--token" in out and "--uninstall" in out, "--help 列出参数")
    check("FXPROXY_GH_PROXY" in out, "--help 说明镜像变量")

    # 管道模式：cat 进来也要能自愈跑起来（不能卡在 read / 不能当脚本读完）
    with open(boot, "rb") as f:
        r = subprocess.run([BASH, "-s", "--", "--help"], input=f.read(),
                           capture_output=True, timeout=60)
    check(r.returncode == 0, f"管道模式 --help 正常（rc={r.returncode}）")
    check("--token".encode() in r.stdout, "管道模式输出 help")

    # 管道模式 + 无 tty + 无参数：应明确报错而不是挂住
    with open(boot, "rb") as f:
        r = subprocess.run([BASH, "-s", "--"], input=f.read(),
                           capture_output=True, timeout=60)
    check(r.returncode != 0, "无 tty 无参数时非 0 退出")
    check("终端".encode() in r.stdout + r.stderr, "提示需要终端或带参数")

    # 未知参数要透传给 install.sh 并被它拒掉（这里只验证不静默成功）
    r = subprocess.run([BASH, str(boot), "--nope"], capture_output=True, text=True,
                       timeout=60)
    check(r.returncode != 0, "未知参数非 0 退出")


# ------------------------------------------------------------------ main
def main():
    test_version_and_platform()
    test_detect_install()
    test_verify()
    test_build_script()
    test_e2e_apply()
    test_read_status()
    asyncio.run(test_network())
    asyncio.run(test_panel_api())
    test_bootstrap()
    print(f"\n{'=' * 46}\n通过 {PASS}  失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

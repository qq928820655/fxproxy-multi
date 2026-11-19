"""面板 HTTP API 端到端冒烟（用 aiohttp test client，不发外网请求）。

运行: python tests/test_webapi.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# webui -> fxa_auth 需要 curl_cffi / pyfxa；本测试不打真实登录路径，
# 用桩模块让 import 通过即可，避免依赖外网安装。
if "curl_cffi" not in sys.modules:
    m = types.ModuleType("curl_cffi")
    m.requests = types.SimpleNamespace(Session=object)
    sys.modules["curl_cffi"] = m
for name, attrs in {
    "fxa": {},
    "fxa._utils": {"APIClient": object, "HawkTokenAuth": object},
    "fxa.core": {"Client": object},
    "fxa.oauth": {"Client": object},
}.items():
    if name not in sys.modules:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod

from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

from fxproxy import config as cfgmod
from fxproxy import guardian
from fxproxy import webui as webui_mod
from fxproxy.webui import Panel

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print(f"  ok  {msg}")


def fake_quota(self):
    return {"uid": "uid-" + self.refresh_token[-1], "max_bytes": 50 * 1024**3,
            "remaining_bytes": 40 * 1024**3, "used_bytes": 10 * 1024**3,
            "reset": "2026-12-01T00:00:00Z", "unlimited": False,
            "subscribed": True, "limited_bandwidth": False}


async def main():
    guardian.GuardianClient.quota = fake_quota  # 打桩，避免外网
    # 服务器列表也打桩：api_servers 用公开 client，不能让它真的上网
    guardian.GuardianClient.nodes_by_country = lambda self: {
        "US": {"name": "United States", "nodes": [("a", 2499), ("b", 2499)]},
        "US-2": {"name": "United States", "nodes": [("c", 2499)]},
        "CN": {"name": "China", "nodes": [("d", 2499)]},
        "ZZ": {"name": "Zedland", "nodes": [("e", 2499)]},
    }

    # cookie 模式的 sessionToken 分支要调 bootstrap，用桩替换掉重型依赖
    def fake_fxa():
        def bootstrap(session_token=None, **kw):
            return {"refresh_token": "r" + session_token[1:]}
        return types.SimpleNamespace(bootstrap=bootstrap)
    webui_mod._fxa = fake_fxa
    with tempfile.TemporaryDirectory() as d:
        cfgpath = Path(d) / "config.json"
        cfgpath.write_text(json.dumps({
            "refresh_tokens": ["tokA", "tokB"], "pool_strategy": "round-robin",
            "country": "", "host": "127.0.0.1", "http_port": 8080,
            "socks_port": 1080, "failover": 3,
        }), encoding="utf-8")
        panel = Panel(str(cfgpath))
        app = web.Application()
        app.add_routes([
            web.get("/", panel.index),
            web.get("/api/state", panel.api_state),
            web.get("/api/accounts", panel.api_accounts),
            web.post("/api/accounts/refresh", panel.api_accounts_refresh),
            web.post("/api/account/remove", panel.api_account_remove),
            web.post("/api/strategy", panel.api_strategy),
            web.post("/api/login", panel.api_login),
            web.get("/api/config", panel.api_config),
            web.get("/api/servers", panel.api_servers),
            web.get("/clash.yaml", panel.api_clash),
        ])
        async with TestClient(TestServer(app)) as cli:
            print("[api] state")
            r = await cli.get("/api/state")
            s = await r.json()
            check(s["authed"] and s["account_count"] == 2, "state 返回 2 个账号")
            check(s["strategy"] == "round-robin", "state 返回策略")

            print("[api] accounts")
            r = await cli.get("/api/accounts")
            a = await r.json()
            check(len(a["accounts"]) == 2, "账号列表 2 条")
            check(a["total_remaining_bytes"] == 80 * 1024**3, "配额汇总 80GiB")
            check(all(x["ok"] for x in a["accounts"]), "账号健康状态")

            print("[api] servers 中文/洲域")
            r = await cli.get("/api/servers")
            sv = await r.json()
            bycode = {c["code"]: c for c in sv["countries"]}
            check("US" in bycode and bycode["US"]["name"] == "美国", "servers 返回中文国名")
            check(bycode["US"]["continent"] == "北美洲", "servers 返回洲域")
            check("CN" in bycode and bycode["CN"]["name"] == "中国", "CN 映射中国")
            check("ZZ" in bycode and bycode["ZZ"]["continent"] == "其他", "未映射代码回退到其他")
            check("已用 / 月度" in (await (await cli.get("/")).text()), "面板表头改为已用/月度")

            print("[api] strategy")
            r = await cli.post("/api/strategy", json={"strategy": "quota"})
            d1 = await r.json()
            check(d1.get("ok") and d1["strategy"] == "quota", "策略切换到 quota")
            check(panel.pool.strategy == "quota", "池策略实际改变")
            check(cfgmod.load(cfgpath)["pool_strategy"] == "quota", "策略已落盘")

            print("[api] login --append 语义")
            r = await cli.post("/api/login", json={"mode": "token", "refresh_token": "tokC"})
            d2 = await r.json()
            check(d2.get("ok") and d2["account_count"] == 3, "追加后 3 个账号")
            check("tokC" in panel.pool.tokens, "新 token 入池")

            r = await cli.post("/api/login", json={"mode": "token", "refresh_token": "tokC"})
            d3 = await r.json()
            check(d3["account_count"] == 3, "重复账号幂等")

            print("[api] remove")
            r = await cli.post("/api/account/remove", json={"index": 0})
            d4 = await r.json()
            check(d4.get("ok") and d4["account_count"] == 2, "删除后 2 个账号")
            check(panel.pool.tokens == ["tokB", "tokC"], "删除索引 0 后顺序正确")
            check(cfgmod.load(cfgpath)["refresh_tokens"] == ["tokB", "tokC"], "删除已落盘")

            print("[api] config 导出")
            r = await cli.get("/api/config")
            d5 = await r.json()
            c = json.loads(d5["config"])
            check(c["refresh_tokens"] == ["tokB", "tokC"], "导出含全部 token")
            check(c["refresh_token"] == "tokB", "导出含兼容字段")

            print("[api] cookie 模式导入")
            T1 = "a" * 64
            R2 = "e" * 64
            cookie_payload = {"session_tokens": [T1], "accounts": []}
            r = await cli.post("/api/login", json={"mode": "cookie", "cookie": json.dumps(cookie_payload)})
            d6 = await r.json()
            check(d6.get("ok") and d6["added"] == 1, "cookie 模式: sessionToken 换 token 入池")
            check(("r" + T1[1:]) in panel.pool.tokens, "cookie 模式: bootstrap 出的 token 已入池")
            check(cfgmod.load(cfgpath)["refresh_tokens"] and ("r" + T1[1:]) in cfgmod.load(cfgpath)["refresh_tokens"], "cookie 模式: 已落盘")

            # 重复导入幂等
            r = await cli.post("/api/login", json={"mode": "cookie", "cookie": json.dumps(cookie_payload)})
            d7 = await r.json()
            check(d7.get("ok") and d7.get("skipped") == 1 and d7["added"] == 0, "cookie 模式: 重复 token 幂等跳过")

            # Cookie 头形式
            r = await cli.post("/api/login", json={"mode": "cookie", "cookie": f"refresh_token={R2}; x=1"})
            d8 = await r.json()
            check(d8.get("ok") and d8["added"] == 1, "cookie 模式: Cookie 头格式导入")
            check(R2 in panel.pool.tokens, "cookie 模式: refresh token 直接入池")

            print("[api] 面板 HTML")
            r = await cli.get("/")
            html = await r.text()
            check("调度策略" in html and "账号池" in html, "面板含多账号 UI")
            check("cookieData" in html and "Cookie / 插件导出" in html, "面板含 cookie 导入 UI")
            check("subUrl" in html and "/clash.yaml" in html, "面板含订阅地址 UI")

            print("[api] clash.yaml 订阅")
            r = await cli.get("/clash.yaml")
            y = await r.text()
            check(r.status == 200 and "proxies:" in y, "订阅返回 YAML")
            check("type: http" in y and "type: socks5" in y, "订阅含 HTTP+SOCKS5 节点")
            check("127.0.0.1" in y and "port: 8080" in y, "订阅节点指向本地代理端口")
            check(r.headers.get("Content-Type", "").startswith("text/yaml"), "订阅 Content-Type 正确")

        # 容器部署：env 注入公网入口后，订阅必须指向公网地址与映射端口。
        print("[api] clash.yaml 走 env 公网入口")
        os.environ.update({
            "FXPROXY_SUB_SERVER": "198.51.100.7",
            "FXPROXY_SUB_HTTP_PORT": "18080",
            "FXPROXY_SUB_SOCKS_PORT": "11080",
        })
        try:
            with tempfile.TemporaryDirectory() as d:
                cfgpath = Path(d) / "sub.json"
                cfgpath.write_text(json.dumps({"refresh_tokens": ["tokA"]}), encoding="utf-8")
                pp = Panel(str(cfgpath))
                app2 = web.Application()
                app2.router.add_get("/clash.yaml", pp.api_clash)
                async with TestClient(TestServer(app2)) as c2:
                    y2 = await (await c2.get("/clash.yaml")).text()
                check('server: "198.51.100.7"' in y2, "env 订阅 server 覆盖 Host 头")
                check("port: 18080" in y2 and "port: 11080" in y2, "env 订阅端口覆盖")
                check("127.0.0.1" not in y2, "env 订阅不含本地地址")
        finally:
            for k in ("FXPROXY_SUB_SERVER", "FXPROXY_SUB_HTTP_PORT", "FXPROXY_SUB_SOCKS_PORT"):
                os.environ.pop(k, None)

    # 回归：全新安装（账号池为空）时 cookie 导入不能抛 TokenPool 空列表错误
    print("[api] 空账号池首次 cookie 导入（回归）")
    with tempfile.TemporaryDirectory() as d:
        cfgpath = Path(d) / "empty.json"
        cfgpath.write_text(json.dumps({}), encoding="utf-8")
        panel = Panel(str(cfgpath))
        check(panel.pool is None, "空配置: 初始账号池为 None")
        app = web.Application()
        app.add_routes([
            web.post("/api/login", panel.api_login),
            web.get("/api/state", panel.api_state),
        ])
        async with TestClient(TestServer(app)) as cli:
            r = await cli.post("/api/login", json={"mode": "cookie", "cookie": "   "})
            d0 = await r.json()
            check("请粘贴凭证文本" in d0.get("error", ""), "空凭证: 友好错误而非崩溃")

            R = "f" * 64
            r = await cli.post("/api/login", json={
                "mode": "cookie", "cookie": f"refresh_token={R}"})
            d1 = await r.json()
            check(d1.get("ok") and d1.get("added") == 1 and d1["account_count"] == 1,
                  "空池首次导入: refresh_token 成功建池")
            check(panel.pool is not None and R in panel.pool.tokens,
                  "空池首次导入: token 已在池中")
            check(cfgmod.load(cfgpath)["refresh_tokens"] == [R],
                  "空池首次导入: 已落盘")
            r = await cli.get("/api/state")
            s = await r.json()
            check(s["authed"] and s["account_count"] == 1,
                  "空池首次导入: 状态变为已登录")

    print("[api] 空账号池首次 sessionToken 导入（回归）")
    with tempfile.TemporaryDirectory() as d:
        cfgpath = Path(d) / "empty2.json"
        cfgpath.write_text(json.dumps({}), encoding="utf-8")
        panel = Panel(str(cfgpath))
        app = web.Application()
        app.add_routes([web.post("/api/login", panel.api_login)])
        async with TestClient(TestServer(app)) as cli:
            T = "c" * 64
            r = await cli.post("/api/login", json={
                "mode": "cookie",
                "cookie": json.dumps({"session_tokens": [T], "accounts": []})})
            d2 = await r.json()
            check(d2.get("ok") and d2.get("added") == 1,
                  "空池首次导入: sessionToken 经 bootstrap 成功")
            check(panel.pool is not None and ("r" + T[1:]) in panel.pool.tokens,
                  "空池首次导入: bootstrap 出的 token 已入池")

    print(f"\nALL PASS ({PASS} checks)")


if __name__ == "__main__":
    asyncio.run(main())

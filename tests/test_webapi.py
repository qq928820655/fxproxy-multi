"""面板 HTTP API 端到端冒烟（用 aiohttp test client，不发外网请求）。

运行: python tests/test_webapi.py
"""
import asyncio
import json
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

    print(f"\nALL PASS ({PASS} checks)")


if __name__ == "__main__":
    asyncio.run(main())

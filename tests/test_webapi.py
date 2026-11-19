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
    self._uid = "uid-" + self.refresh_token[-1]
    return {"uid": self._uid, "max_bytes": 50 * 1024**3,
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
            web.post("/api/accounts/export", panel.api_accounts_export),
            web.post("/api/account/remove", panel.api_account_remove),
            web.post("/api/strategy", panel.api_strategy),
            web.post("/api/force-ipv4", panel.api_force_ipv4),
            web.post("/api/runtime-ua", panel.api_runtime_ua),
            web.post("/api/login", panel.api_login),
            web.get("/api/config", panel.api_config),
            web.get("/api/servers", panel.api_servers),
            web.get("/api/nodes", panel.api_nodes),
            web.post("/api/nodes/disable", panel.api_node_disable),
            web.post("/api/start", panel.api_start),
            web.post("/api/ports", panel.api_ports),
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

            print("[api] nodes 列表")
            r = await cli.get("/api/nodes")
            nd = await r.json()
            bycode_nodes = {n["code"]: n for n in nd["nodes"]}
            check(len(nd["nodes"]) == 5, "nodes 返回全部 5 个节点(展开)")
            check(bycode_nodes["US"]["name"] == "美国", "nodes 用中文国名")
            check(bycode_nodes["US"]["host"] in ("a", "b") and
                  bycode_nodes["US"]["port"] == 2499, "nodes 含 host/port")
            check("latency_ms" not in nd["nodes"][0] and "ok" not in nd["nodes"][0],
                  "无参 nodes 不探测、无延迟字段")

            print("[api] nodes probe=1")
            real_conn = webui_mod.socket.create_connection
            calls = {"n": 0}
            class _Sock:
                def __enter__(self): return self
                def __exit__(self, *a): return False
            def fake_conn(addr, timeout=None):
                calls["n"] += 1
                host = addr[0]
                if host == "b": raise OSError("refused")
                return _Sock()  # b 失败，其余立即成功
            webui_mod.socket.create_connection = fake_conn
            try:
                r = await cli.get("/api/nodes?probe=1")
                pr = await r.json()
            finally:
                webui_mod.socket.create_connection = real_conn
            pnodes = {n["host"]: n for n in pr["nodes"]}
            check(calls["n"] == 5, "probe 对每个节点各发一次 TCP connect")
            check(pnodes["a"]["ok"] is True and isinstance(pnodes["a"]["latency_ms"], int),
                  "probe 成功节点 ok=True + 整数延迟")
            check(pnodes["b"]["ok"] is False and pnodes["b"]["latency_ms"] is None,
                  "probe 失败节点 ok=False + 延迟 None")
            html2 = await (await cli.get("/")).text()
            check(all(k in html2 for k in ("probeBtn", "lat-fast", "lat-mid", "lat-slow", "lat-fail")),
                  "面板含节点卡片与四档延迟配色")

            print("[api] strategy")
            r = await cli.post("/api/strategy", json={"strategy": "quota"})
            d1 = await r.json()
            check(d1.get("ok") and d1["strategy"] == "quota", "策略切换到 quota")
            check(panel.pool.strategy == "quota", "池策略实际改变")
            check(cfgmod.load(cfgpath)["pool_strategy"] == "quota", "策略已落盘")

            print("[api] force-ipv4 开关")
            check(cfgmod.load(cfgpath).get("force_ipv4_egress") is False,
                  "默认出口强制 IPv4 关闭")
            r = await cli.post("/api/force-ipv4", json={"enabled": True})
            df = await r.json()
            check(df.get("ok") and df.get("force_ipv4_egress") is True,
                  "开启出口强制 IPv4")
            check(cfgmod.load(cfgpath).get("force_ipv4_egress") is True,
                  "开关已落盘")
            stf = await (await cli.get("/api/state")).json()
            check(stf.get("force_ipv4_egress") is True, "state 暴露开关状态")
            r = await cli.post("/api/force-ipv4", json={"enabled": False})
            check((await r.json()).get("force_ipv4_egress") is False, "可再次关闭")

            print("[api] runtime UA 开关")
            r = await cli.post("/api/runtime-ua", json={"enabled": True})
            dua = await r.json()
            check(dua.get("ok") and dua.get("runtime_user_agent") == "firefox",
                  "开启 Firefox UA")
            check(cfgmod.load(cfgpath).get("runtime_user_agent") == "firefox",
                  "UA 开关已落盘")
            check(panel.pool._session.headers.get("User-Agent") == guardian.FIREFOX_UA,
                  "共享会话 UA 已热更新")
            stu = await (await cli.get("/api/state")).json()
            check(stu.get("runtime_user_agent") == "firefox", "state 暴露 UA 设置")
            r = await cli.post("/api/runtime-ua", json={"enabled": False})
            check((await r.json()).get("runtime_user_agent") == "", "可再次关闭")

            print("[api] 节点禁用")
            check(cfgmod.load(cfgpath).get("disabled_nodes") == [], "默认无禁用节点")
            r = await cli.post("/api/nodes/disable",
                               json={"host": "a", "disabled": True})
            dd = await r.json()
            check(dd.get("ok") and dd.get("disabled") is True, "禁用节点返回 ok")
            check("a" in cfgmod.load(cfgpath).get("disabled_nodes", []), "禁用已落盘")
            nodes = (await (await cli.get("/api/nodes")).json())["nodes"]
            na = next(n for n in nodes if n["host"] == "a")
            check(na.get("disabled") is True, "api_nodes 标记禁用节点")
            r = await cli.post("/api/nodes/disable",
                               json={"host": "a", "disabled": False})
            check("a" not in cfgmod.load(cfgpath).get("disabled_nodes", []),
                  "可再次启用节点")

            print("[api] login --append 语义")
            r = await cli.post("/api/login", json={"mode": "token", "refresh_token": "tokC"})
            d2 = await r.json()
            check(d2.get("ok") and d2["account_count"] == 3, "追加后 3 个账号")
            check("tokC" in panel.pool.tokens, "新 token 入池")

            r = await cli.post("/api/login", json={"mode": "token", "refresh_token": "tokC"})
            d3 = await r.json()
            check(d3["account_count"] == 3, "重复账号幂等")

            print("[api] 同账号（同 uid）新凭证 → 更新而非新增")
            # fake_quota 的 uid = "uid-"+token 末位，故 newA 与 tokA 同 uid
            r = await cli.post("/api/login", json={"mode": "token", "refresh_token": "newA"})
            du = await r.json()
            check(du.get("action") == "updated" and du["account_count"] == 3,
                  f"同 uid 原地更新不增账号（{du}）")
            check("newA" in panel.pool.tokens and "tokA" not in panel.pool.tokens,
                  "旧 token 被新 token 替换")
            r = await cli.post("/api/login", json={"mode": "token", "refresh_token": "newD"})
            da = await r.json()
            check(da.get("action") == "added" and da["account_count"] == 4,
                  f"不同 uid 才新增账号（{da}）")
            # 还原现场，避免影响后续 remove/config 用例
            panel.pool.replace_token(0, "tokA")
            panel.pool.remove(3)
            await asyncio.to_thread(panel._persist)
            check(panel.pool.tokens == ["tokA", "tokB", "tokC"], "测试后还原账号池")

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

            print("[api] 账号导出（多选）")
            r = await cli.post("/api/accounts/export", json={"indices": [0]})
            ex = await r.json()
            check(ex.get("format") == "fxproxy-accounts"
                  and ex["accounts"][0]["refresh_token"] == "tokB",
                  "按索引导出单个账号")
            r = await cli.post("/api/accounts/export", json={"indices": [0, 1]})
            ex2 = await r.json()
            check([a["refresh_token"] for a in ex2["accounts"]] == ["tokB", "tokC"],
                  "多选导出顺序正确")
            # 导出格式应能被导入解析器读回（refresh_token 归入 refresh 桶）
            from fxproxy import cookieutil
            hexrt = "a" * 64
            sample = json.dumps({"accounts": [
                {"uid": 1, "email": "x@y.z", "refresh_token": hexrt}]})
            parsed = cookieutil.parse(sample)
            check(parsed["refresh_tokens"] == [hexrt] and parsed["session_tokens"] == [],
                  "导出 JSON 可被导入解析为 refresh_token")

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
            check("provUrl" in html and "provider=1" in html,
                  "面板含 mihomo provider 地址 UI")
            check("copyProvBtn" in html, "面板含 provider 复制按钮")

            print("[api] clash.yaml 订阅")
            r = await cli.get("/clash.yaml")
            y = await r.text()
            check(r.status == 200 and "proxies:" in y, "订阅返回 YAML")
            check("type: http" in y and "type: socks5" in y, "订阅含 HTTP+SOCKS5 节点")
            check("127.0.0.1" in y and "port: 8080" in y, "订阅节点指向本地代理端口")
            check(r.headers.get("Content-Type", "").startswith("text/yaml"), "订阅 Content-Type 正确")
            ui = r.headers.get("subscription-userinfo", "")
            sm = panel.pool.summary()
            exp_total = int(sm.get("total_max_bytes") or 0)
            exp_used = int(sm.get("total_used_bytes") or 0)
            check(f"download={exp_used}" in ui and f"total={exp_total}" in ui
                  and "upload=0" in ui and "expire=0" in ui,
                  f"订阅带配额头 subscription-userinfo（实得 {ui!r}）")
            # Firefox 报真实用量时先采纳为新基线
            panel._add_traffic(1111, 2222)
            ui2 = (await cli.get("/clash.yaml")).headers.get("subscription-userinfo", "")
            check(f"download={exp_used + 3333}" in ui2 and "upload=0" in ui2,
                  f"Firefox 基线上继续叠加本地新增（实得 {ui2!r}）")
            # Firefox 不计量（used=0，免费账号常态）时才回落到本地观测
            orig_summary = panel.pool.summary
            panel.pool.summary = lambda: {
                "total_used_bytes": 0, "total_max_bytes": 50 * 1024**3,
                "total_remaining_bytes": 50 * 1024**3, "unlimited": False}
            try:
                ui3 = await panel._subscription_userinfo()
            finally:
                panel.pool.summary = orig_summary
            check("upload=1111" in ui3 and "download=2222" in ui3,
                  f"Firefox 不计量时用本地兜底（实得 {ui3!r}）")
            # 直接复现用户场景：0 → 本地 10M → Firefox 20M → 再加 2M = 22M
            saved = dict(panel._traffic)
            panel._traffic.update({"up": 0, "down": 0, "ff_seen": 0, "offset": 0})
            panel._add_traffic(0, 10 * 1024**2)
            check(panel._apply_usage_baseline(0) == 10 * 1024**2,
                  "Firefox=0 时显示本地 10M")
            check(panel._apply_usage_baseline(20 * 1024**2) == 20 * 1024**2,
                  "Firefox 刷新到 20M 时采纳为新基线")
            panel._add_traffic(0, 2 * 1024**2)
            check(panel._apply_usage_baseline(20 * 1024**2) == 22 * 1024**2,
                  "20M 基础上继续累加本地新增到 22M")
            panel._traffic.clear(); panel._traffic.update(saved); panel._save_traffic()

            print("[api] clash.yaml?provider=1 —— mihomo proxy-provider 形态")
            rp = await cli.get("/clash.yaml?provider=1")
            yp = await rp.text()
            check(rp.status == 200 and yp.startswith("proxies:"), "provider 以 proxies: 开头")
            only_proxies = [ln for ln in yp.splitlines() if ln and not ln[0].isspace()]
            check(only_proxies == ["proxies:"],
                  f"provider 只有 proxies 一个顶层键，实得 {only_proxies}")
            # 完整配置里的这些字段会把客户端设置顶掉，provider 里绝不能出现
            for bad in ("port: 7890", "socks-port:", "mode: rule", "proxy-groups:",
                        "rules:", "log-level:", "allow-lan:"):
                check(bad not in yp, f"provider 不含 {bad!r}")
            check("type: http" in yp and "type: socks5" in yp, "provider 仍含两个节点")
            check("fxproxy-provider.yaml" in rp.headers.get("Content-Disposition", ""),
                  "provider 下载文件名区分")

            print("[api] 端口可配置（8080 被占时改端口）")
            s0 = await (await cli.get("/api/state")).json()
            check(s0.get("web_port") == 8765, "state 暴露面板端口，供排除冲突")

            r = await cli.post("/api/ports", json={"http_port": 19081, "socks_port": 19082})
            dp = await r.json()
            check(dp.get("ok") and dp["http_port"] == 19081 and dp["socks_port"] == 19082,
                  "改端口返回新端口")
            saved = cfgmod.load(cfgpath)
            check(saved["http_port"] == 19081, "新 HTTP 端口已落盘")
            check(saved["socks_port"] == 19082, "新 SOCKS5 端口已落盘")

            r = await cli.post("/api/ports", json={"http_port": 19081, "socks_port": 19081})
            check("同一个端口" in (await r.json()).get("error", ""),
                  "HTTP/SOCKS5 不许同端口")
            r = await cli.post("/api/ports", json={"http_port": 70000})
            check("1-65535" in (await r.json()).get("error", ""), "越界端口被拒")
            r = await cli.post("/api/ports", json={"http_port": "abc"})
            check("不是数字" in (await r.json()).get("error", ""), "非数字端口被拒")
            r = await cli.post("/api/ports", json={"http_port": 8765})
            check("面板" in (await r.json()).get("error", ""), "不许占用面板端口")
            r = await cli.post("/api/ports", json={"http_port": 19081, "socks_port": 19082})
            check((await r.json()).get("unchanged") is True, "端口未变时幂等返回")

            yp2 = await (await cli.get("/clash.yaml")).text()
            check("port: 19081" in yp2 and "port: 19082" in yp2,
                  "订阅端口跟随新配置")

            check("portBtn" in (await (await cli.get("/")).text()), "面板含端口设置 UI")

            print("[api] 运行中改端口 → 按新端口自动重启")
            # 桩掉 ProxyServer，只验证「先停、再按新端口起」的接线，不打真外网
            class _Picker:
                country = "US"
                node = "node-a"

            class _OldSrv:
                picker = _Picker()
                stopped = False

                async def stop(self):
                    self.stopped = True

            old_srv = _OldSrv()
            panel.srv = old_srv
            orig_ps = webui_mod.ProxyServer
            calls = {}

            class _NewSrv:
                def __init__(self, pool, picker, host=None, http_port=None,
                             socks_port=None, failover=3,
                             force_ipv4_egress=False):
                    calls.update(host=host, http_port=http_port,
                                 socks_port=socks_port, country=picker.country,
                                 node=picker.node,
                                 force_ipv4_egress=force_ipv4_egress)

                async def start(self):
                    calls["started"] = True

            webui_mod.ProxyServer = _NewSrv
            try:
                r = await cli.post("/api/ports", json={"http_port": 19083,
                                                       "socks_port": 19084})
                dr = await r.json()
            finally:
                webui_mod.ProxyServer = orig_ps
                panel.srv = None
            check(dr.get("restarted") is True, "运行中改端口返回 restarted")
            check(old_srv.stopped, "旧实例先被停掉")
            check(calls.get("http_port") == 19083 and calls.get("socks_port") == 19084,
                  "新实例用新端口启动")
            check(calls.get("country") == "US" and calls.get("node") == "node-a",
                  "重启保留原出口国家/节点")

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
                # vless 节点是否导出取决于本机 bridge/uuid.txt（gitignore 文件）。
                # 干净环境没有它，这里桩一个，避免测试结果随开发机状态漂移。
                pp._bridge_uuid = lambda: "11111111-2222-3333-4444-555555555555"
                app2 = web.Application()
                app2.router.add_get("/clash.yaml", pp.api_clash)
                async with TestClient(TestServer(app2)) as c2:
                    y2 = await (await c2.get("/clash.yaml")).text()
                check('server: "198.51.100.7"' in y2, "env 订阅 server 覆盖 Host 头")
                check("port: 443" in y2, "per-node vless 走 443")
                check("fxproxy-http" not in y2 and "fxproxy-socks5" not in y2,
                      "有 vless 节点时不导出网关裸端口代理")
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

    print("[api] 空池 api_start 防御（回归）")
    with tempfile.TemporaryDirectory() as d:
        cfgpath = Path(d) / "emptypool.json"
        cfgpath.write_text(json.dumps({
            "refresh_tokens": [], "pool_strategy": "round-robin", "country": "US",
        }), encoding="utf-8")
        panel = Panel(str(cfgpath))
        check(panel.pool is None, "refresh_tokens=[] → _make_pool 返回 None")
        # 模拟异常路径下的"空池非 None"：clients 为空，tokens 也为空
        panel.pool = guardian.TokenPool(["x"])
        panel.pool.clients = []
        app = web.Application()
        app.add_routes([web.post("/api/start", panel.api_start)])
        async with TestClient(TestServer(app)) as cli:
            r = await cli.post("/api/start", json={"country": "US"})
            d = await r.json()
            check("无账号" in d.get("error", ""), "空池非 None 启动: 友好错误而非 IndexError")

    print(f"\nALL PASS ({PASS} checks)")


if __name__ == "__main__":
    asyncio.run(main())

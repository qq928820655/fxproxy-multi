"""TokenPool / config 单元测试（不发网络请求）。

运行: python -m pytest tests/test_pool.py -q
或直接: python tests/test_pool.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fxproxy import config as cfgmod
from fxproxy.guardian import TokenPool, GuardianClient

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print(f"  ok  {msg}")


def test_config_migration():
    print("[config] 旧单 token 迁移 + 环境变量")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.json"
        p.write_text('{"refresh_token":"OLD_TOKEN","country":"US"}', encoding="utf-8")
        cfg = cfgmod.load(p)
        check(cfg["refresh_tokens"] == ["OLD_TOKEN"], "单 token 转成列表")
        check(cfg["refresh_token"] == "OLD_TOKEN", "兼容字段同步")

        cfg["refresh_tokens"] = ["A", "B", "A"]
        saved = cfgmod.save(cfg, p)
        cfg2 = cfgmod.load(saved)
        check(cfg2["refresh_tokens"] == ["A", "B"], "保存时去重保序")
        check(cfg2["refresh_token"] == "A", "兼容字段=首个")

        os.environ["FXPROXY_REFRESH_TOKENS"] = "X,Y,Z"
        cfg3 = cfgmod.load(p)
        check(cfg3["refresh_tokens"] == ["X", "Y", "Z"], "多 token 环境变量覆盖")
        os.environ["FXPROXY_REFRESH_TOKEN"] = "Y"
        cfg4 = cfgmod.load(p)
        check(cfg4["refresh_tokens"][0] == "Y", "单 token 环境变量置顶")
        del os.environ["FXPROXY_REFRESH_TOKENS"]
        del os.environ["FXPROXY_REFRESH_TOKEN"]


def test_pool_round_robin():
    print("[pool] round-robin 均匀轮转")
    pool = TokenPool(["t1", "t2", "t3"], strategy="round-robin")
    picked = [pool.pick()[0] for _ in range(9)]
    check(picked == [0, 1, 2, 0, 1, 2, 0, 1, 2], f"顺序轮转 {picked}")


def test_pool_failover():
    print("[pool] failover 主备")
    pool = TokenPool(["t1", "t2", "t3"], strategy="failover")
    check(all(pool.pick()[0] == 0 for _ in range(5)), "默认全走 #1")
    pool.report_failure(pool.clients[0])
    check(pool.pick()[0] == 1, "#1 冷却后切到 #2")


def test_pool_cooldown_all():
    print("[pool] 全部冷却时降级为全可用")
    pool = TokenPool(["t1", "t2"], cooldown=1)
    for c in pool.clients:
        pool.report_failure(c)
    check(len(pool.pick_many(2)) == 2, "全冷却仍返回全部账号（避免服务中断）")


def test_pool_add_remove():
    print("[pool] 账号增删")
    pool = TokenPool(["t1"])
    idx = pool.add("t2")
    check(idx == 1 and pool.tokens == ["t1", "t2"], "add 追加")
    try:
        pool.add("t2")
        check(False, "重复 add 应报错")
    except ValueError:
        check(True, "重复 add 被拒绝")
    pool.remove(0)
    check(pool.tokens == ["t2"], "remove 后索引平移")
    try:
        pool.remove(0)
        check(False, "删最后一个应报错")
    except ValueError:
        check(True, "至少保留一个账号")


def test_upsert_by_uid():
    print("[pool] 同 uid 更新而非新增")
    pool = TokenPool(["tokA", "tokB"])
    pool.clients[0]._uid = 111
    pool.clients[1]._uid = 222
    check(pool.uid_index() == {"111": 0, "222": 1}, "uid_index 映射正确")
    check(pool.upsert("tokNew", 111) == "updated", "同 uid 返回 updated")
    check(pool.tokens == ["tokNew", "tokB"], "同 uid 原地替换，顺序不变")
    check(len(pool.clients) == 2, "同 uid 不增加账号数")
    check(pool.upsert("tokC", 333) == "added", "新 uid 返回 added")
    check(len(pool.clients) == 3, "新 uid 增加账号数")
    check(pool.upsert("tokNew", 111) == "skipped", "重复 token 返回 skipped")


def test_pool_quota_weight():
    print("[pool] quota 按剩余加权")
    pool = TokenPool(["t1", "t2"], strategy="quota")
    import time
    # 注入缓存：#1 剩余 big，#2 剩余 small
    now = __import__("time").monotonic()
    pool._quota_cache[0] = (now + 999, {"remaining_bytes": 10**9})
    pool._quota_cache[1] = (now + 999, {"remaining_bytes": 1})
    wins = [pool.pick()[0] for _ in range(500)]
    check(wins.count(0) > 450, f"高配额账号被选中 {wins.count(0)}/500")


def test_account_auth_failure_is_distinct():
    print("[pool] 账号鉴权失败异常类型")
    from fxproxy.proxyserver import AccountAuthFailure, ProxyServer
    check(issubclass(AccountAuthFailure, ConnectionError), "AccountAuthFailure 可捕获")
    # 单 client 向后兼容包装
    gc = GuardianClient("t1")
    srv = ProxyServer(gc, None)
    check(len(srv.pool.clients) == 1, "传 GuardianClient 自动包成单账号池")


def test_quota_not_falsely_unlimited():
    """status.limited_bandwidth 是权威：50GB 账号不能被 header 误判为无限。"""
    print("[pool] 配额 unlimited 判定")
    gc = GuardianClient("qt")
    # 模拟 status: 50GB 限量账号
    gc.status = lambda: {"uid": 42, "maxBytes": "53687091200",
                         "subscribed": True, "limited_bandwidth": True}
    gc.proxy_pass = lambda: "tok"  # 不真的取 header，_quota_hdr 保持空
    q = gc.quota()
    check(q["unlimited"] is False, "status 限量 + 无 header 时 not unlimited")
    check(q["max_bytes"] == 53687091200, "max_bytes 回退到 status.maxBytes")
    check(q["used_bytes"] is None, "无 header 时 used 为 None 而非编造")

    # 模拟 status 说不限量 + header 确认 unlimited
    gc.status = lambda: {"uid": 42, "maxBytes": "0",
                         "subscribed": True, "limited_bandwidth": False}
    gc._quota_hdr = {"unlimited": True, "limit": None, "remaining": None}
    q2 = gc.quota()
    check(q2["unlimited"] is True, "status 不限量 + header 确认 → unlimited")

    print("[pool] summary 总量字段")
    pool = TokenPool(["a", "b"])
    pool.quota_snapshot = lambda: [
        {"ok": True, "unlimited": False, "used_bytes": 100, "max_bytes": 1000,
         "remaining_bytes": 900},
        {"ok": True, "unlimited": False, "used_bytes": 50, "max_bytes": 500,
         "remaining_bytes": 450},
    ]
    s = pool.summary()
    check(s["total_used_bytes"] == 150, "summary 汇总已用")
    check(s["total_max_bytes"] == 1500, "summary 汇总总额")
    check(s["unlimited"] is False, "summary unlimited 全真才真")


def test_env_deployment_overrides():
    """容器/VPS 部署：全靠 FXPROXY_* 环境变量，无 config.json 也要能跑。"""
    print("[config] 容器环境变量覆盖（无配置文件）")
    keys = ["FXPROXY_REFRESH_TOKENS", "FXPROXY_REFRESH_TOKEN", "FXPROXY_HOST",
            "FXPROXY_HTTP_PORT", "FXPROXY_SOCKS_PORT", "FXPROXY_COUNTRY",
            "FXPROXY_POOL_STRATEGY", "FXPROXY_SUB_SERVER",
            "FXPROXY_SUB_HTTP_PORT", "FXPROXY_SUB_SOCKS_PORT", "FXPROXY_FAILOVER"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update({
            "FXPROXY_REFRESH_TOKENS": "t1,t2",
            "FXPROXY_HOST": "0.0.0.0",
            "FXPROXY_HTTP_PORT": "18080",
            "FXPROXY_SOCKS_PORT": "11080",
            "FXPROXY_COUNTRY": "de",
            "FXPROXY_POOL_STRATEGY": "quota",
            "FXPROXY_SUB_SERVER": "203.0.113.9",
            "FXPROXY_SUB_HTTP_PORT": "18080",
            "FXPROXY_SUB_SOCKS_PORT": "11080",
            "FXPROXY_FAILOVER": "5",
        })
        cfg = cfgmod.load("/nonexistent/path/config.json")
        check(cfg["refresh_tokens"] == ["t1", "t2"], "env token 列表")
        check(cfg["host"] == "0.0.0.0", "env 监听地址")
        check(cfg["http_port"] == 18080 and cfg["socks_port"] == 11080, "env 监听端口")
        check(cfg["country"] == "DE", "env 出口国家大写归一")
        check(cfg["pool_strategy"] == "quota", "env 调度策略")
        check(cfg["failover"] == 5, "env failover")
        check(cfg["sub_server"] == "203.0.113.9", "env 订阅公网地址")
        check(cfg["sub_http_port"] == 18080 and cfg["sub_socks_port"] == 11080,
              "env 订阅公网端口")

        # 端口未显式给时「跟随」监听端口。这里刻意保留 0 而不回填具体值，
        # 否则之后改监听端口订阅会停在旧端口上（消费端用 `or` 回退）。
        os.environ.pop("FXPROXY_SUB_HTTP_PORT", None)
        os.environ.pop("FXPROXY_SUB_SOCKS_PORT", None)
        cfg2 = cfgmod.load("/nonexistent/path/config.json")
        check(not cfg2["sub_http_port"], "未配置时 sub 端口保持 0=跟随")
        check((cfg2["sub_http_port"] or cfg2["http_port"]) == cfg2["http_port"],
              "订阅端口回退到监听端口")

        # 非法 env 端口忽略，不把程序带崩
        os.environ["FXPROXY_SUB_HTTP_PORT"] = "not-a-port"
        cfg3 = cfgmod.load("/nonexistent/path/config.json")
        check(not cfg3["sub_http_port"], "非法 env 端口被忽略")
        check((cfg3["sub_http_port"] or cfg3["http_port"]) == cfg3["http_port"],
              "非法值仍回退到监听端口")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_subscription_uses_public_entry():
    """订阅 YAML 的 server/port 必须取公网入口，而非监听地址。"""
    print("[clashconf] 订阅使用公网入口")
    from fxproxy import clashconf
    yaml = clashconf.build_clash_yaml(
        server="203.0.113.9", http_port=18080, socks_port=11080)
    check('server: "203.0.113.9"' in yaml, "订阅 server = 公网地址")
    check("port: 18080" in yaml and "port: 11080" in yaml, "订阅端口 = 公网映射端口")
    check("127.0.0.1" not in yaml, "订阅里不出现 127.0.0.1")


def test_port_helpers():
    """端口占用检测与 bind 失败提示——8080 冲突是最高频的启动失败原因。"""
    print("[ports] 端口占用检测 + 中文提示")
    import errno
    import socket
    from fxproxy.proxyserver import _bind_error
    from fxproxy.webui import _pick_free_ports, _port_free

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    busy = srv.getsockname()[1]
    try:
        check(not _port_free("127.0.0.1", busy), "已监听端口判定为占用")
    finally:
        srv.close()
    check(_port_free("127.0.0.1", busy), "释放后端口可用")

    e = OSError(errno.EADDRINUSE, "in use")
    msg = _bind_error("HTTP", "--http-port", "127.0.0.1", 8080, e)
    check("8080" in msg and "--http-port" in msg, "占用提示含端口与改法")
    check("面板" in msg, "占用提示指向面板入口")
    e2 = OSError(errno.EACCES, "denied")
    check("1024" in _bind_error("HTTP", "--http-port", "127.0.0.1", 80, e2),
          "低端口无权限时给出高位端口建议")

    p1, p2 = _pick_free_ports(start=19100, span=60)
    check(p2 == p1 + 1, "测速端口成对相邻")
    check(_port_free("127.0.0.1", p1) and _port_free("127.0.0.1", p2),
          "测速端口确实空闲（不再写死 8095/8096）")


def test_force_ipv4_egress():
    print("[ipv4] 出口强制 IPv4")
    import asyncio
    from fxproxy.proxyserver import ProxyServer
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.json"
        p.write_text('{"refresh_token":"T"}', encoding="utf-8")
        check(cfgmod.load(p).get("force_ipv4_egress") is False, "默认关闭")
        os.environ["FXPROXY_FORCE_IPV4_EGRESS"] = "true"
        try:
            check(cfgmod.load(p).get("force_ipv4_egress") is True, "环境变量可开启")
        finally:
            os.environ.pop("FXPROXY_FORCE_IPV4_EGRESS", None)
    check(ProxyServer._is_ip_literal("1.2.3.4"), "识别 IPv4 字面量")
    check(ProxyServer._is_ip_literal("::1"), "识别 IPv6 字面量")
    check(not ProxyServer._is_ip_literal("example.com"), "域名不是字面量")
    inst = object.__new__(ProxyServer)
    ip = asyncio.run(inst._resolve_ipv4("localhost"))
    check(ip.count(".") == 3, f"localhost 解析为 IPv4（{ip}）")


if __name__ == "__main__":
    test_config_migration()
    test_env_deployment_overrides()
    test_subscription_uses_public_entry()
    test_port_helpers()
    test_force_ipv4_egress()
    test_pool_round_robin()
    test_pool_failover()
    test_pool_cooldown_all()
    test_pool_add_remove()
    test_upsert_by_uid()
    test_pool_quota_weight()
    test_quota_not_falsely_unlimited()
    test_account_auth_failure_is_distinct()
    print(f"\nALL PASS ({PASS} checks)")

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


def test_version_and_changelog():
    print("[version] 版本号与 CHANGELOG 一致")
    import re
    from fxproxy import __version__
    root = Path(__file__).resolve().parents[1]
    pv = re.search(r'^version\s*=\s*"([^"]+)"',
                   (root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    check(pv and pv.group(1) == __version__,
          f"pyproject 与 __version__ 一致 ({__version__})")
    cl = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    check(f"## [{__version__}]" in cl, "CHANGELOG 含当前版本条目")


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


def test_update_flow():
    print("[update] 一键更新不会卡死在 409")
    import subprocess
    import tempfile
    import time as _t
    from fxproxy import updater
    from fxproxy.webui import Panel

    p = Panel.__new__(Panel)
    p._updating = False
    p._update_pid = None
    p._update_until = 0.0
    check(p._update_busy() is False, "空闲时不忙")
    p._updating = True
    check(p._update_busy() is True, "请求进行中时忙")
    p._updating = False
    p._update_pid = 2_000_000_000
    p._update_until = _t.monotonic() + 600
    check(p._update_busy() is False, "升级脚本已退出时自动放开（不再 409）")
    check(p._update_pid is None, "死 pid 被清掉")
    p._update_pid = os.getpid()
    p._update_until = _t.monotonic() + 600
    check(p._update_busy() is True, "脚本存活时仍拦截")
    p._update_until = _t.monotonic() - 1
    check(p._update_busy() is False, "TTL 到期后放开")
    check(p._update_pid is None, "过期 pid 被清掉")

    d = Path(tempfile.mkdtemp())
    # build_script 首个参数现在是 kind，模板按平台选。这里固定按 linux 生成
    # bash 脚本来校验模板内容，避免 Windows 上落到 ps1 分支。
    _platform = updater.sys.platform
    updater.sys.platform = "linux"
    try:
        script, kind = updater.build_script(
            "bash", d, d / "pkg.tar.gz", d / ".run", "9.9.9", "0.5.6",
            d / "cfg.json", "0.0.0.0", 8765, mode="pid")
    finally:
        updater.sys.platform = _platform
    body = script.read_text(encoding="utf-8")
    check(kind == "bash", "生成 bash 脚本")
    check("port_busy()" in body and "fuser -k" in body,
          "stop_all 校验端口释放并按端口兜底击杀")
    check("PROBE=127.0.0.1" in body, "绑定 0.0.0.0 时探测走回环")
    check("/api/version" in body and "current" in body,
          "健康检查校验运行版本而非仅端口应答")
    check("runtime.env" in body and "command -v python3" in body,
          "start_all 有解释器回退链")
    # Windows 上的 bash 可能是 WSL 转发 shim，连探测调用都会被安全策略拦掉
    # 整个进程，所以直接不发起；脚本内容断言在上面已经覆盖。
    if sys.platform.startswith("win"):
        print("  skip  Windows 上不跑 bash -n（bash 可能是 WSL shim）")
    else:
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True)
        check(r.returncode == 0, "生成的脚本 bash 语法通过")


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


def test_pool_disabled_accounts():
    print("[pool] 手动禁用账号不参与选择")
    pool = TokenPool(["t1", "t2", "t3"], strategy="round-robin")
    pool.set_disabled({TokenPool.acct_key("t2")})
    picked = [pool.pick()[0] for _ in range(6)]
    check(1 not in picked, f"禁用 #2 后不再被选中 {picked}")
    check(set(picked) == {0, 2}, "只轮转未禁用账号")
    pool.set_disabled({TokenPool.acct_key(t) for t in ("t1", "t2", "t3")})
    check(pool.pick_many(3) == [], "全部禁用时返回空（不兜底）")
    try:
        pool.pick()
        check(False, "全部禁用 pick 应报错")
    except RuntimeError:
        check(True, "全部禁用 pick 报错")
    pool.set_disabled(set())
    check(len(pool.pick_many(3)) == 3, "恢复后重新参与")


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


def test_runtime_user_agent():
    print("[ua] 控制面 UA 可开关")
    import requests
    from fxproxy import guardian
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.json"
        p.write_text('{"refresh_token":"T"}', encoding="utf-8")
        check(cfgmod.load(p).get("runtime_user_agent") == "", "默认不伪装 UA")
        os.environ["FXPROXY_RUNTIME_USER_AGENT"] = "firefox"
        try:
            check(cfgmod.load(p).get("runtime_user_agent") == "firefox",
                  "环境变量可开启 UA")
        finally:
            os.environ.pop("FXPROXY_RUNTIME_USER_AGENT", None)
    s = requests.Session()
    guardian.set_default_user_agent("firefox")
    guardian.apply_user_agent(s)
    check(s.headers.get("User-Agent") == guardian.FIREFOX_UA, "firefox 映射到 FIREFOX_UA")
    guardian.set_default_user_agent("")
    guardian.apply_user_agent(s)
    check(s.headers.get("User-Agent") == requests.utils.default_user_agent(),
          "关闭后恢复 requests 默认 UA")


def test_fpn_401_selfheal_and_ua():
    print("[fpn] 401 自愈 + 浏览器 UA 不发给 fpn")
    from fxproxy.guardian import GuardianClient, FIREFOX_UA

    class Resp:
        def __init__(self, code, body=None):
            self.status_code = code
            self._body = body or {}
            self.text = str(self._body)
            self.headers = {}
        def json(self):
            return self._body

    class Sess:
        def __init__(self):
            self.headers = {"User-Agent": FIREFOX_UA}
            self.gets = []
            self._n = 0
            self.posts = 0
        def post(self, url, json=None, timeout=None):
            self.posts += 1
            return Resp(200, {"access_token": "at%d" % self.posts, "expires_in": 3600})
        def get(self, url, headers=None, timeout=None):
            self.gets.append(dict(headers or {}))
            self._n += 1
            if self._n == 1:
                return Resp(401, {"error": "Unauthorized"})
            return Resp(200, {"uid": 42, "maxBytes": "0"})

    s = Sess()
    gc = GuardianClient("rt", session=s)
    st = gc.status()
    check(st.get("uid") == 42, "401 后换新 token 重试成功")
    check(len(s.gets) == 2 and s.posts >= 2, "发生一次重试与重新取 token")
    check(all(g.get("User-Agent") == "fxproxy/1.0" for g in s.gets),
          "fpn 请求改用非浏览器 UA（否则 406）")
    check(s.gets[0].get("Authorization") != s.gets[1].get("Authorization"),
          "重试携带刷新后的 bearer")


def test_proxy_splice_traffic():
    print("[traffic] 8080/1080 代理流量回调计数")
    import asyncio
    from fxproxy.proxyserver import ProxyServer, NodePicker

    class _C:
        def nodes_by_country(self):
            return {"US": {"name": "US", "nodes": [("a", 1)]}}

    srv = ProxyServer(GuardianClient("t"), NodePicker(_C()))
    got = []
    srv._on_traffic = lambda up, down, gc=None: got.append((up, down))

    class _Block:
        def __init__(self, first):
            self.first = first
            self.done = False
        async def read(self, n):
            if not self.done:
                self.done = True
                return self.first
            await asyncio.Event().wait()  # 永不返回，靠 sibling 结束被 cancel

    class _EOF:
        def __init__(self, first):
            self.first = first
            self.done = False
        async def read(self, n):
            if not self.done:
                self.done = True
                return self.first
            return b""

    class _W:
        def write(self, b):
            pass
        async def drain(self):
            pass
        def close(self):
            pass

    async def run():
        await srv._splice(_Block(b"a" * 100), _W(), _EOF(b"b" * 300), _W(),
                          leftover=b"x" * 10)

    asyncio.run(run())
    check(got == [(110, 300)], "up=leftover+客户端字节, down=服务端字节")


def test_node_picker_exclude():
    print("[nodes] 禁用节点从选择池排除")
    from fxproxy.proxyserver import NodePicker

    class _C:
        def nodes_by_country(self):
            return {"US": {"name": "US", "nodes": [("a", 1), ("b", 2)]}}

    p = NodePicker(_C(), exclude={"a"})
    pool = p._pool()
    check([h for h, _ in pool] == ["b"], "禁用节点被排除，只剩 b")
    p2 = NodePicker(_C(), exclude={"a", "b"})
    try:
        p2._pool()
        check(False, "全部禁用应报错")
    except RuntimeError:
        check(True, "全部禁用时报错")
    # 显式 pin 的节点优先于禁用（用户手动指定）
    p3 = NodePicker(_C(), node="a", exclude={"a"})
    check(p3._pool() == [("a", 1)], "显式 pin 覆盖禁用")


if __name__ == "__main__":
    test_version_and_changelog()
    test_config_migration()
    test_env_deployment_overrides()
    test_subscription_uses_public_entry()
    test_port_helpers()
    test_force_ipv4_egress()
    test_runtime_user_agent()
    test_fpn_401_selfheal_and_ua()
    test_proxy_splice_traffic()
    test_node_picker_exclude()
    test_update_flow()
    test_pool_round_robin()
    test_pool_failover()
    test_pool_cooldown_all()
    test_pool_disabled_accounts()
    test_pool_add_remove()
    test_upsert_by_uid()
    test_pool_quota_weight()
    test_quota_not_falsely_unlimited()
    test_account_auth_failure_is_distinct()
    print(f"\nALL PASS ({PASS} checks)")

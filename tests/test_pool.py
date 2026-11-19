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


if __name__ == "__main__":
    test_config_migration()
    test_pool_round_robin()
    test_pool_failover()
    test_pool_cooldown_all()
    test_pool_add_remove()
    test_pool_quota_weight()
    test_account_auth_failure_is_distinct()
    print(f"\nALL PASS ({PASS} checks)")

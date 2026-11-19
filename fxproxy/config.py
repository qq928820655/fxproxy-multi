"""Config file handling (JSON).

多账号配置用 ``refresh_tokens`` 列表；旧版的单 ``refresh_token`` 字段
仍然识别，load 时自动迁移成列表，save 时两个字段都写，保证老客户端能读。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path(
    os.environ.get("FXPROXY_CONFIG", Path.home() / ".config" / "fxproxy" / "config.json")
)

DEFAULTS = {
    "refresh_token": "",          # 兼容字段：始终等于 refresh_tokens[0]
    "refresh_tokens": [],         # 多账号凭证列表
    "pool_strategy": "round-robin",   # round-robin | quota | failover
    "quota_cache_ttl": 60,        # 配额快照缓存秒数
    "cooldown_seconds": 300,      # 账号失败后的冷却秒数
    "country": "",                # "" = any; else ISO code like "US", "GB", "DE"
    "host": "127.0.0.1",
    "http_port": 8080,
    "socks_port": 1080,
    "failover": 3,                # nodes to try per connection before giving up
    # 订阅里给客户端用的地址/端口。容器或 VPS 部署时客户端在公网，
    # 不能填 127.0.0.1；这三项由 FXPROXY_SUB_* 环境变量注入。
    "sub_server": "",             # 空 = 用请求 Host 推导（仅本地/局域网直连用）
    "sub_http_port": 0,           # 0 = 跟随 http_port
    "sub_socks_port": 0,          # 0 = 跟随 socks_port
    # 出口强制 IPv4：CONNECT 前把目标域名在本机解析成 IPv4，再让 Fastly
    # 用 IPv4 连接目标，从而让目标站看到的出口 IP 是 IPv4。代价是本机要
    # 做 DNS 解析，且纯 IPv6 目标会连不上。
    "force_ipv4_egress": False,
    # 运行时控制面请求的 User-Agent："" = requests 默认；"firefox" = 伪装
    # 成 Firefox UA（降低 python-requests 指纹），也可填自定义字符串。
    "runtime_user_agent": "",
}

_VALID_STRATEGIES = ("round-robin", "quota", "failover")


def _split_tokens(raw: str) -> list[str]:
    """环境变量可写逗号/换行/分号分隔的多个 token。"""
    parts = [p.strip() for p in raw.replace(";", ",").replace("\n", ",").split(",")]
    return [p for p in parts if p]


def load(path: Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_PATH
    cfg = dict(DEFAULTS)
    cfg["refresh_tokens"] = []
    if path.exists():
        cfg.update(json.loads(path.read_text()))

    # ---- 归一化 token 列表 ----
    tokens = cfg.get("refresh_tokens") or []
    if isinstance(tokens, str):           # 手工写错成字符串时兜底
        tokens = _split_tokens(tokens)
    tokens = [t.strip() for t in tokens if isinstance(t, str) and t.strip()]
    single = (cfg.get("refresh_token") or "").strip()
    if single and single not in tokens:
        # 老配置只有 refresh_token：迁移成列表第一项
        tokens.insert(0, single)
    if not tokens and single:
        tokens = [single]
    # 去重保序
    cfg["refresh_tokens"] = list(dict.fromkeys(tokens))
    cfg["refresh_token"] = cfg["refresh_tokens"][0] if cfg["refresh_tokens"] else ""

    # ---- 环境变量覆盖 ----
    env_many = os.environ.get("FXPROXY_REFRESH_TOKENS")
    if env_many:
        cfg["refresh_tokens"] = list(dict.fromkeys(_split_tokens(env_many)))
    env_one = os.environ.get("FXPROXY_REFRESH_TOKEN")
    if env_one:
        if env_one in cfg["refresh_tokens"]:
            cfg["refresh_tokens"].remove(env_one)
        cfg["refresh_tokens"].insert(0, env_one)
    cfg["refresh_token"] = cfg["refresh_tokens"][0] if cfg["refresh_tokens"] else ""

    # ---- 监听参数 / 出口国家环境变量覆盖（无配置文件部署）----
    env_host = os.environ.get("FXPROXY_HOST")
    if env_host and env_host.strip():
        cfg["host"] = env_host.strip()
    for key, envkey in (("http_port", "FXPROXY_HTTP_PORT"),
                        ("socks_port", "FXPROXY_SOCKS_PORT"),
                        ("failover", "FXPROXY_FAILOVER")):
        raw = os.environ.get(envkey)
        if raw and raw.strip():
            try:
                cfg[key] = int(raw.strip())
            except (TypeError, ValueError):
                pass
    env_country = os.environ.get("FXPROXY_COUNTRY")
    if env_country and env_country.strip():
        cfg["country"] = env_country.strip().upper()
    env_strategy = os.environ.get("FXPROXY_POOL_STRATEGY")
    if env_strategy and env_strategy.strip() in _VALID_STRATEGIES:
        cfg["pool_strategy"] = env_strategy.strip()
    env_f4 = os.environ.get("FXPROXY_FORCE_IPV4_EGRESS")
    if env_f4 is not None and env_f4.strip():
        cfg["force_ipv4_egress"] = env_f4.strip().lower() in (
            "1", "true", "yes", "on")
    env_ua = os.environ.get("FXPROXY_RUNTIME_USER_AGENT")
    if env_ua is not None:
        cfg["runtime_user_agent"] = env_ua.strip()

    # ---- 订阅公网入口（容器/VPS 部署关键）----
    # 优先级：环境变量 > 配置文件 > 默认值；端口回退到实际监听端口。
    env_sub = os.environ.get("FXPROXY_SUB_SERVER")
    if env_sub and env_sub.strip():
        cfg["sub_server"] = env_sub.strip()
    for key, envkey, fallback in (
        ("sub_http_port", "FXPROXY_SUB_HTTP_PORT", "http_port"),
        ("sub_socks_port", "FXPROXY_SUB_SOCKS_PORT", "socks_port"),
    ):
        raw = os.environ.get(envkey)
        if raw and raw.strip():
            try:
                cfg[key] = int(raw.strip())
            except (TypeError, ValueError):
                pass  # 非法值忽略，走配置文件/回退
        # 0 或缺失 = 跟随监听端口（消费端用 `or` 回退）。
        # 这里刻意不回填具体端口：回填会把「跟随」固化成当时的值，
        # 之后在面板里改监听端口，订阅就再也不跟着变了。

    if cfg.get("pool_strategy") not in _VALID_STRATEGIES:
        cfg["pool_strategy"] = "round-robin"
    try:
        cfg["quota_cache_ttl"] = max(0, int(cfg.get("quota_cache_ttl", 60)))
    except (TypeError, ValueError):
        cfg["quota_cache_ttl"] = 60
    try:
        cfg["cooldown_seconds"] = max(1, int(cfg.get("cooldown_seconds", 300)))
    except (TypeError, ValueError):
        cfg["cooldown_seconds"] = 300
    return cfg


def save(cfg: dict, path: Path | None = None) -> Path:
    path = Path(path) if path else DEFAULT_PATH
    out = dict(cfg)
    tokens = [t.strip() for t in (out.get("refresh_tokens") or []) if t and t.strip()]
    out["refresh_tokens"] = list(dict.fromkeys(tokens))
    # 同时写兼容字段
    out["refresh_token"] = out["refresh_tokens"][0] if out["refresh_tokens"] else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 无 POSIX 权限位，忽略
    return path

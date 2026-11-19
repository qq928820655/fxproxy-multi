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

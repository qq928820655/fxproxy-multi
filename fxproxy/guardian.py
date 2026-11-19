"""Guardian (Firefox IP Protection) API client.

Handles the runtime credential chain:

    refresh_token  --(FxA /oauth/token)-->  access_token (~24h)
    access_token   --(GET /api/v1/fpn/token)-->  proxy pass JWT (~10min)

and exposes the public Fastly server list. The proxy pass JWT is what the
local proxy presents to a Fastly node via ``Proxy-Authorization: Bearer``.
"""
from __future__ import annotations

import base64
import json
import random
import threading
import time
import itertools
from typing import Optional

import requests

# Firefox desktop public (PKCE-capable) client. The VPN client
# (e6eb0d1e856335fc) is confidential and cannot be used for token refresh.
FIREFOX_CLIENT_ID = "5882386c6d801776"
VPN_SCOPE = "profile https://identity.mozilla.com/apps/vpn"

FXA_OAUTH_TOKEN_URL = "https://oauth.accounts.firefox.com/v1/token"
GUARDIAN_BASE = "https://vpn.mozilla.org"
SERVERLIST_URL = (
    "https://firefox.settings.services.mozilla.com"
    "/v1/buckets/main/collections/vpn-serverlist/records"
)

# Refresh the short-lived proxy pass this many seconds before it expires.
PASS_REFRESH_MARGIN = 60
# Refresh the access token this many seconds before it expires.
ACCESS_REFRESH_MARGIN = 300


def _b64url_json(seg: str) -> dict:
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def jwt_exp(token: str) -> int:
    """Return the ``exp`` (unix seconds) of a JWT, or 0 if absent."""
    try:
        payload = _b64url_json(token.split(".")[1])
        return int(payload.get("exp", 0))
    except Exception:
        return 0


class GuardianError(RuntimeError):
    pass


class NotEnrolledError(GuardianError):
    """Raised when the account is valid but not enrolled (HTTP 403).

    Run the one-time ``login``/``enroll`` bootstrap to enroll the account.
    """


class GuardianClient:
    def __init__(
        self,
        refresh_token: str,
        client_id: str = FIREFOX_CLIENT_ID,
        scope: str = VPN_SCOPE,
        session: Optional[requests.Session] = None,
    ):
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.scope = scope
        self._s = session or requests.Session()
        self._lock = threading.Lock()

        self._access_token: Optional[str] = None
        self._access_exp: float = 0.0

        self._pass: Optional[str] = None
        self._pass_exp: float = 0.0
        self._quota_hdr: dict = {}

    # ---- access token -------------------------------------------------
    def _refresh_access_token(self) -> str:
        r = self._s.post(
            FXA_OAUTH_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
                "scope": self.scope,
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise GuardianError(
                f"oauth refresh failed: {r.status_code} {r.text[:200]}"
            )
        d = r.json()
        self._access_token = d["access_token"]
        self._access_exp = time.time() + int(d.get("expires_in", 86400))
        return self._access_token

    @property
    def access_token(self) -> str:
        if not self._access_token or time.time() >= self._access_exp - ACCESS_REFRESH_MARGIN:
            self._refresh_access_token()
        return self._access_token  # type: ignore[return-value]

    def _auth_header(self) -> dict:
        return {"Authorization": "Bearer " + self.access_token}

    # ---- account status / quota --------------------------------------
    def status(self) -> dict:
        r = self._s.get(
            GUARDIAN_BASE + "/api/v1/fpn/status",
            headers=self._auth_header(),
            timeout=20,
        )
        if r.status_code == 403:
            raise NotEnrolledError(
                "account not enrolled (HTTP 403) — run the `login` bootstrap"
            )
        if r.status_code != 200:
            raise GuardianError(f"status failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def quota(self) -> dict:
        """Return account status merged with live quota usage.

        Keys: uid, max_bytes, subscribed, limited_bandwidth,
              remaining_bytes, used_bytes, reset (ISO), unlimited.

        ``limited_bandwidth`` from the status endpoint is authoritative for
        whether the account has a cap; the x-quota-unlimited header only tells
        us the header set is absent. A 50GB account that has not minted a pass
        yet still has maxBytes set — never report it as unlimited in that case.
        """
        s = self.status()
        max_bytes = int(s.get("maxBytes", 0) or 0)
        limited = bool(s.get("limited_bandwidth", False))
        subscribed = bool(s.get("subscribed", False))
        # populate quota headers by minting a proxy pass
        try:
            self.proxy_pass()
        except GuardianError:
            pass
        q = self._quota_hdr
        limit = q.get("limit") or max_bytes
        remaining = q.get("remaining")
        used = (limit - remaining) if (limit and remaining is not None) else None
        # 真实额度判定：status 明确限量 → 有限；只有 status 说不限量且 header
        # 也确认 unlimited 时才算无限。避免把 50GB 账号误显示成"无限"。
        unlimited = (not limited) and bool(q.get("unlimited", False))
        return {
            "uid": s.get("uid"),
            "max_bytes": limit,
            "subscribed": subscribed,
            "limited_bandwidth": limited,
            "remaining_bytes": remaining,
            "used_bytes": used,
            "reset": q.get("reset"),
            "unlimited": unlimited,
        }

    # ---- proxy pass ---------------------------------------------------
    def _fetch_pass(self) -> str:
        r = self._s.get(
            GUARDIAN_BASE + "/api/v1/fpn/token",
            headers=self._auth_header(),
            timeout=20,
        )
        if r.status_code == 403:
            raise NotEnrolledError(
                "account not enrolled (HTTP 403) — run the `login` bootstrap"
            )
        if r.status_code != 200:
            raise GuardianError(
                f"proxy pass fetch failed: {r.status_code} {r.text[:200]}"
            )
        token = r.json().get("token")
        if not token:
            raise GuardianError("proxy pass response missing 'token'")
        self._pass = token
        self._pass_exp = jwt_exp(token) or (time.time() + 600)
        h = r.headers
        self._quota_hdr = {
            "limit": _int_or_none(h.get("x-quota-limit")),
            "remaining": _int_or_none(h.get("x-quota-remaining")),
            "reset": h.get("x-quota-reset"),
            "unlimited": (h.get("x-quota-unlimited", "").lower() == "true"),
        }
        return token

    def proxy_pass(self) -> str:
        """Return a currently-valid proxy pass JWT, refreshing as needed."""
        with self._lock:
            if not self._pass or time.time() >= self._pass_exp - PASS_REFRESH_MARGIN:
                self._fetch_pass()
            return self._pass  # type: ignore[return-value]

    # ---- server list --------------------------------------------------
    def servers(self) -> list[dict]:
        r = self._s.get(SERVERLIST_URL, timeout=20)
        if r.status_code != 200:
            raise GuardianError(f"serverlist failed: {r.status_code}")
        return r.json()["data"]

    def nodes_by_country(self) -> dict[str, dict]:
        """Return {country_code: {'name', 'nodes': [(host, port), ...]}}."""
        out: dict[str, dict] = {}
        for rec in self.servers():
            nodes = []
            for city in rec.get("cities", []):
                for srv in city.get("servers", []):
                    nodes.append((srv["hostname"], int(srv["port"])))
            if nodes:
                out[rec["code"]] = {"name": rec.get("name", rec["code"]), "nodes": nodes}
        return out


# ---------------------------------------------------------------------------
# Multi-account scheduling
# ---------------------------------------------------------------------------

class TokenPool:
    """A pool of GuardianClients with pluggable per-connection selection.

    Strategies:
      * ``round-robin`` — cycle through accounts (default, even spread).
      * ``quota``       — weight by remaining bytes, refreshed on a TTL cache.
      * ``failover``    — stick to account #0, move down only on failure.

    A failing account is put in cooldown for ``cooldown`` seconds; if every
    account is cooling down we ignore the cooldown and use them all (better a
    retry than a hard outage). All state is guarded by ``_lock`` because the
    proxy serves connections from many asyncio tasks via ``to_thread``.
    """

    STRATEGIES = ("round-robin", "quota", "failover")

    def __init__(
        self,
        tokens: list[str],
        strategy: str = "round-robin",
        cooldown: int = 300,
        quota_cache_ttl: int = 60,
        session: Optional[requests.Session] = None,
    ):
        if not tokens:
            raise ValueError("TokenPool needs at least one refresh_token")
        if strategy not in self.STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}; choose {self.STRATEGIES}")
        self.strategy = strategy
        self.cooldown = max(1, int(cooldown))
        self.quota_cache_ttl = max(0, int(quota_cache_ttl))
        self._lock = threading.RLock()
        self._rr = itertools.count()
        self._cool_until: dict[int, float] = {}     # client index -> monotonic deadline
        self._quota_cache: dict[int, tuple[float, dict]] = {}
        # One shared Session for the whole pool: fewer TCP/TLS handshakes.
        self._session = session or requests.Session()
        self.clients: list[GuardianClient] = [
            GuardianClient(t, session=self._session) for t in tokens
        ]

    # ---- account mutation ---------------------------------------------
    @property
    def tokens(self) -> list[str]:
        return [c.refresh_token for c in self.clients]

    def add(self, token: str) -> int:
        token = (token or "").strip()
        if not token:
            raise ValueError("empty refresh_token")
        with self._lock:
            if token in self.tokens:
                raise ValueError("该账号已在池中")
            self.clients.append(GuardianClient(token, session=self._session))
            return len(self.clients) - 1

    def remove(self, index: int) -> None:
        with self._lock:
            if not 0 <= index < len(self.clients):
                raise IndexError(f"账号序号 {index} 不存在")
            if len(self.clients) == 1:
                raise ValueError("至少保留一个账号")
            self.clients.pop(index)
            # 索引平移：重建冷却表和配额缓存
            self._cool_until = {}
            self._quota_cache = {}

    def set_strategy(self, strategy: str) -> None:
        if strategy not in self.STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}; choose {self.STRATEGIES}")
        with self._lock:
            self.strategy = strategy

    # ---- cooldown / selection -----------------------------------------
    def report_failure(self, client: GuardianClient) -> None:
        """Put an account in cooldown after a connect/auth failure."""
        with self._lock:
            try:
                idx = self.clients.index(client)
            except ValueError:
                return
            self._cool_until[idx] = time.monotonic() + self.cooldown
            # stale quota makes the weight unreliable; drop it
            self._quota_cache.pop(idx, None)
            _pool_log(f"账号 #{idx + 1} 失败，冷却 {self.cooldown}s")

    def _healthy(self) -> list[int]:
        now = time.monotonic()
        with self._lock:
            out = [i for i in range(len(self.clients))
                   if self._cool_until.get(i, 0) <= now]
            # All cooling -> use everyone rather than refusing service.
            return out or list(range(len(self.clients)))

    def pick(self) -> tuple[int, GuardianClient]:
        """Choose one account. Returns ``(index, client)``."""
        with self._lock:
            healthy = self._healthy()
            if self.strategy == "failover":
                idx = healthy[0]
            elif self.strategy == "quota":
                idx = self._pick_by_quota(healthy)
            else:  # round-robin
                # next() is atomic under GIL, but keep it under the lock for clarity.
                idx = next(self._rr) % len(healthy)
                idx = healthy[idx]
            return idx, self.clients[idx]

    def pick_many(self, n: int) -> list[tuple[int, GuardianClient]]:
        """Up to n distinct accounts for cross-account failover."""
        with self._lock:
            healthy = self._healthy()
            order: list[int]
            if self.strategy == "quota":
                # Best first, then the rest in current pool order.
                first = self._pick_by_quota(healthy)
                order = [first] + [i for i in healthy if i != first]
            elif self.strategy == "failover":
                order = list(healthy)
            else:
                start = next(self._rr) % len(healthy)
                order = healthy[start:] + healthy[:start]
            return [(i, self.clients[i]) for i in order[:max(1, n)]]

    def _pick_by_quota(self, healthy: list[int]) -> int:
        """Weighted pick by remaining bytes; equal weight when cache is cold."""
        now = time.monotonic()
        weights: list[float] = []
        for i in healthy:
            cached = self._quota_cache.get(i)
            if cached and cached[0] >= now:
                rem = cached[1].get("remaining_bytes")
                # +1 keeps an exhausted (0) account in the draw, just unlikely.
                weights.append(max(1.0, float(rem or 0)))
            else:
                weights.append(1.0)
        total = sum(weights)
        r = random.random() * total
        acc = 0.0
        for i, w in zip(healthy, weights):
            acc += w
            if r <= acc:
                return i
        return healthy[-1]

    # ---- quota ---------------------------------------------------------
    def refresh_quotas(self) -> list[dict]:
        """Fetch every account's quota, cache it, return snapshots."""
        out: list[dict] = []
        now = time.monotonic()
        with self._lock:
            clients = list(self.clients)
        for i, c in enumerate(clients):
            try:
                q = c.quota()
                q["ok"] = True
            except Exception as e:
                q = {"uid": None, "remaining_bytes": None, "max_bytes": 0,
                     "reset": None, "unlimited": False, "ok": False, "error": str(e)}
            with self._lock:
                self._quota_cache[i] = (now + self.quota_cache_ttl, q)
            out.append(q)
        return out

    def quota_snapshot(self) -> list[dict]:
        """Return cached quota snapshots; refresh only stale entries."""
        now = time.monotonic()
        with self._lock:
            clients = list(self.clients)
            stale = [i for i in range(len(clients))
                     if not (self._quota_cache.get(i) and self._quota_cache[i][0] >= now)]
        for i in stale:
            c = clients[i]
            try:
                q = c.quota()
                q["ok"] = True
            except Exception as e:
                q = {"uid": None, "remaining_bytes": None, "max_bytes": 0,
                     "reset": None, "unlimited": False, "ok": False, "error": str(e)}
            with self._lock:
                self._quota_cache[i] = (now + self.quota_cache_ttl, q)
        with self._lock:
            return [dict(self._quota_cache[i][1]) if i in self._quota_cache
                    else {"uid": None, "remaining_bytes": None, "max_bytes": 0,
                          "ok": False, "error": "no snapshot yet"}
                    for i in range(len(clients))]

    def summary(self) -> dict:
        """Aggregate quota across accounts for the CLI/panel."""
        snaps = self.quota_snapshot()
        good = [s for s in snaps if s.get("ok")]
        totals = [s["remaining_bytes"] for s in good
                  if s.get("remaining_bytes") is not None]
        useds = [s["used_bytes"] for s in good if s.get("used_bytes") is not None]
        maxes = [s["max_bytes"] for s in good if s.get("max_bytes")]
        unlimited = all(s.get("unlimited") for s in snaps) and bool(snaps)
        return {
            "accounts": len(snaps),
            "ok_accounts": len(good),
            "snapshots": snaps,
            "total_remaining_bytes": (None if not totals else sum(totals)),
            "total_used_bytes": (sum(useds) if useds else None),
            "total_max_bytes": (sum(maxes) if maxes else None),
            "unlimited": unlimited,
            "strategy": self.strategy,
        }


def _pool_log(msg: str):
    print(f"[fxproxy:pool] {msg}", flush=True)


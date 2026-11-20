"""Parse browser-exported Firefox Accounts credentials.

Accepts, in order of preference:
  1. A JSON object emitted by chrome-extension/ (has ``session_tokens`` /
     ``accounts`` arrays, and/or a ``refresh_token``).
  2. A JSON array of token strings.
  3. A raw Netscape-style cookie header / cookie file, e.g.
     ``sessionToken=abcd...; other=1`` or a ``#HttpOnly_`` line.
  4. One or more bare tokens separated by whitespace/commas/newlines.

Returns a dict::

    {"session_tokens": [...], "refresh_tokens": [...],
     "accounts": [{"uid", "email", "session_token"}, ...], "detected": [...]}

Every token is deduplicated, first-seen order preserved. The two token kinds
(64-hex sessions vs refresh tokens) are indistinguishable by shape, so a bare
64-hex token is reported as a *session* token by default; callers that know it
is a refresh token should pass ``bare_as="refresh_token"``.
"""
from __future__ import annotations

import base64
import json
import re

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
FXS = re.compile(r"^fxs_[A-Za-z0-9_\-]+$")


def _is_token(s: str) -> bool:
    return bool(HEX64.match(s) or FXS.match(s))


def _norm(s: str) -> str:
    s = s.strip()
    if HEX64.match(s):
        return s.lower()
    if s.startswith("fxs_"):
        # Bearer-style session token: base64url of the 32 raw bytes -> hex.
        b64 = s[4:].replace("-", "+").replace("_", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            raw = base64.urlsafe_b64decode(b64)
            hexs = raw.hex()
            if HEX64.match(hexs):
                return hexs
        except Exception:
            pass
        return s
    return s


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _from_cookie_text(text: str, sessions: list, refresh: list, detected: list):
    """Pull `name=value` pairs whose name hints at a session/refresh token."""
    # Netscape cookie file lines:  domain  flag  path  secure  expiry  name  value
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            name, value = parts[5], parts[6]
            if _is_token(value):
                detected.append(f"cookie:{name}")
                (refresh if "refresh" in name.lower() else sessions).append(_norm(value))
    # header style: a=b; c=d
    for pair in re.split(r"[;\n\r]", text):
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip().strip('"')
        if _is_token(value):
            detected.append(f"cookie:{name}")
            (refresh if "refresh" in name.lower() else sessions).append(_norm(value))


def _walk_json(obj, sessions, refresh, detected, depth=0):
    if depth > 8 or obj is None:
        return
    if isinstance(obj, str):
        if _is_token(obj):
            sessions.append(_norm(obj))
            detected.append("json:string")
        return
    if isinstance(obj, list):
        for x in obj:
            _walk_json(x, sessions, refresh, detected, depth + 1)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, str) and _is_token(v):
                if "refresh" in kl:
                    refresh.append(_norm(v))
                    detected.append(f"json:{k}")
                elif "session" in kl or "token" in kl:
                    sessions.append(_norm(v))
                    detected.append(f"json:{k}")
                else:
                    _walk_json(v, sessions, refresh, detected, depth + 1)
            else:
                _walk_json(v, sessions, refresh, detected, depth + 1)


def parse(raw: str, *, bare_as: str = "session_token") -> dict:
    """Parse credential text into token buckets.

    ``bare_as`` decides where a bare 64-hex token goes when there is no field
    name to classify it: "session_token" (default) or "refresh_token".

    ``accounts`` entries keep uid/email paired with their token, whichever kind
    they carry: ``{"uid", "email", "session_token"}`` for plugin exports,
    ``{"uid", "email", "refresh_token"}`` for the panel's own export.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("凭证文本为空")

    sessions: list = []
    refresh: list = []
    accounts: list = []
    detected: list = []

    # Try JSON first (object or array from the Chrome extension).
    parsed = None
    if raw[0] in "[{":
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None

    if isinstance(parsed, dict):
        # accounts 数组：插件导出带 session_token，面板「导出账号」带 refresh_token。
        # 两种都要连 uid/email 一起收 —— 只收 token 的话导出再导入邮箱就丢了，
        # 面板列表里那列会是空的。
        for rec in parsed.get("accounts") or []:
            if not isinstance(rec, dict):
                continue
            uid = rec.get("uid") or ""
            email = rec.get("email") or ""
            st = rec.get("session_token") or rec.get("sessionToken")
            rt = rec.get("refresh_token") or rec.get("refreshToken")
            if st and _is_token(st):
                accounts.append({
                    "uid": uid, "email": email,
                    "session_token": _norm(st),
                })
            elif rt and _is_token(rt):
                accounts.append({
                    "uid": uid, "email": email,
                    "refresh_token": _norm(rt),
                })
                refresh.append(_norm(rt))
                detected.append("json:accounts[].refresh_token")
        # explicit fields first, then a generic sweep for renamed keys
        for key in ("session_token", "sessionToken"):
            v = parsed.get(key)
            if isinstance(v, str) and _is_token(v):
                sessions.append(_norm(v))
                detected.append(f"json:{key}")
        for key in ("refresh_token", "refreshToken"):
            v = parsed.get(key)
            if isinstance(v, str) and _is_token(v):
                refresh.append(_norm(v))
                detected.append(f"json:{key}")
        for key in ("session_tokens", "sessionTokens"):
            for v in parsed.get(key) or []:
                if isinstance(v, str) and _is_token(v):
                    sessions.append(_norm(v))
                    detected.append(f"json:{key}")
        for key in ("refresh_tokens", "refreshTokens"):
            for v in parsed.get(key) or []:
                if isinstance(v, str) and _is_token(v):
                    refresh.append(_norm(v))
                    detected.append(f"json:{key}")
        _walk_json(parsed, sessions, refresh, detected)
    elif isinstance(parsed, list):
        for v in parsed:
            if isinstance(v, str) and _is_token(v):
                sessions.append(_norm(v))
                detected.append("json:array")
            else:
                _walk_json(v, sessions, refresh, detected)
    elif raw.lower().lstrip().startswith("#httponly_") or "=" in raw:
        _from_cookie_text(raw, sessions, refresh, detected)
    else:
        # bare tokens: whitespace/comma separated. Without a field name the
        # kind is ambiguous; honor the caller's ``bare_as`` hint.
        target = refresh if bare_as == "refresh_token" else sessions
        for piece in re.split(r"[\s,]+", raw):
            if _is_token(piece):
                target.append(_norm(piece))
                detected.append("bare")
        if not sessions and not refresh:
            raise ValueError("没有识别到 sessionToken / refresh_token / cookie")

    sessions = _dedup(sessions)
    refresh = _dedup(refresh)

    # Keep account tokens consistent with the de-duplicated session list.
    # accounts 里也可能只有 refresh_token（面板导出），那种不进 sessions。
    account_tokens = {a["session_token"] for a in accounts if a.get("session_token")}
    sessions = _dedup(sessions + sorted(account_tokens - set(sessions)))

    if not sessions and not refresh:
        raise ValueError("没有识别到 sessionToken / refresh_token / cookie")

    return {
        "session_tokens": sessions,
        "refresh_tokens": refresh,
        "accounts": accounts,
        "detected": _dedup(detected),
    }

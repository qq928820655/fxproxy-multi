/*
 * fxproxy FxA Token Extractor — content script.
 * Runs only on accounts.firefox.com(.cn). Reads the page's own storage —
 * the same data the FxA web app uses to keep you signed in — and returns a
 * structured payload. It never talks to a server; the popup copies the result
 * to the clipboard and the human pastes it into the fxproxy panel.
 *
 * Primary source:  __fxa_storage.accounts  -> { uid: { email, sessionToken, ... } }
 * Fallbacks:       any localStorage/sessionStorage/cookie value that looks like
 *                  a sessionToken, in case Mozilla renames the key.
 */
(function () {
  "use strict";

  var HEX64 = /^[0-9a-fA-F]{64}$/;
  var FXS_PREFIXED = /^fxs_[A-Za-z0-9_\-]+$/; // newer Bearer-style session token

  function isSessionToken(v) {
    return typeof v === "string" && (HEX64.test(v) || FXS_PREFIXED.test(v));
  }

  // Strip a possible "fxs_" bearer prefix -> raw 32-byte hex when possible.
  // fxs_ tokens are base64url of the 32 raw bytes; decode back to hex so the
  // bootstrap code (which expects hex) can consume them.
  function normalizeToken(v) {
    if (!v) return v;
    if (HEX64.test(v)) return v.toLowerCase();
    if (v.startsWith("fxs_")) {
      var b64 = v.slice(4).replace(/-/g, "+").replace(/_/g, "/");
      while (b64.length % 4) b64 += "=";
      try {
        var bin = atob(b64);
        var out = "";
        for (var i = 0; i < bin.length; i++) {
          out += ("0" + bin.charCodeAt(i).toString(16)).slice(-2);
        }
        if (HEX64.test(out)) return out;
      } catch (e) { /* not decodable; hand back verbatim */ }
      return v;
    }
    return v;
  }

  function emailOf(uid, rec) {
    if (rec && rec.email) return rec.email;
    // some builds keep the email under a separate prefix key
    try {
      var alt = rawGet("__fxa_storage.email." + uid) || rawGet("_fxa_storage.email." + uid);
      if (alt) return JSON.parse(alt);
    } catch (e) { /* ignore */ }
    return null;
  }

  function rawGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }

  // Deep-walk any JSON-ish value hunting for sessionToken fields.
  // Kept generic so a key rename in fxa-content-server doesn't break us.
  function deepScan(value, found, depth) {
    if (depth > 6 || value == null) return;
    if (typeof value === "string") {
      if (isSessionToken(value)) found.push(normalizeToken(value));
      return;
    }
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i++) deepScan(value[i], found, depth + 1);
      return;
    }
    if (typeof value === "object") {
      var keys = Object.keys(value);
      for (var k = 0; k < keys.length; k++) {
        var key = keys[k], val = value[key];
        if (/sessiontoken|session_token/i.test(key) && typeof val === "string") {
          found.push(normalizeToken(val));
        } else {
          deepScan(val, found, depth + 1);
        }
      }
    }
  }

  function scanStorage(store, found) {
    try {
      for (var i = 0; i < store.length; i++) {
        var key = store.key(i);
        var raw = store.getItem(key);
        if (!raw) continue;
        // direct hit: the value itself is a token
        if (isSessionToken(raw)) { found.push(normalizeToken(raw)); continue; }
        var parsed = null;
        try { parsed = JSON.parse(raw); } catch (e) { continue; }
        deepScan(parsed, found, 0);
      }
    } catch (e) { /* storage may be blocked */ }
  }

  function scanCookies(found) {
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i].split("=");
      if (p.length < 2) continue;
      var val = decodeURIComponent(p.slice(1).join("=").trim());
      if (isSessionToken(val)) found.push(normalizeToken(val));
    }
  }

  function collect() {
    var accounts = [];
    var found = [];

    // 1) documented structure — gives us email + uid alongside the token
    var acctRaw = rawGet("__fxa_storage.accounts") || rawGet("_fxa_storage.accounts");
    if (acctRaw) {
      try {
        var map = JSON.parse(acctRaw);
        Object.keys(map || {}).forEach(function (uid) {
          var rec = map[uid] || {};
          var st = rec.sessionToken || rec.session_token;
          if (isSessionToken(st)) {
            accounts.push({
              uid: uid,
              email: emailOf(uid, rec),
              session_token: normalizeToken(st)
            });
            found.push(normalizeToken(st));
          }
        });
      } catch (e) { /* malformed, fall through to generic scan */ }
    }

    // 2) 兜底扫描：只在上面一个都没拿到时才做。
    //
    // 以前这两步是叠加的，结果一个账号导出 4 个 token：第 1 个来自 accounts
    // （当前登录，确定有效），后 3 个是 localStorage / sessionStorage / cookie
    // 里的历史残留。实测这 3 个送进 Mozilla 一律返回
    // "Invalid authentication token: Missing authentication"。
    // 混在一起只会让人以为"4 个总有一个能用"，实际是在拿废 token 撞墙。
    if (!found.length) {
      scanStorage(window.localStorage, found);
      scanStorage(window.sessionStorage, found);
      scanCookies(found);
    }

    // de-dup tokens, keep first-seen order
    var seen = {};
    var tokens = found.filter(function (t) {
      if (!t || seen[t]) return false;
      seen[t] = 1;
      return true;
    });

    // make sure account list has no duplicates by token
    var acctSeen = {};
    accounts = accounts.filter(function (a) {
      if (acctSeen[a.session_token]) return false;
      acctSeen[a.session_token] = 1;
      return true;
    });

    return {
      version: 1,
      source: "fxproxy-chrome-extension",
      origin: location.origin,
      extracted_at: new Date().toISOString(),
      accounts: accounts,
      session_tokens: tokens
    };
  }

  // Expose for the popup's explicit "extract now" call, and auto-cache on load
  // so the token is available even if the user signs in after opening the tab.
  window.__fxproxyExtract = collect;
  try {
    window.__fxproxyLastPayload = collect();
  } catch (e) {
    window.__fxproxyLastPayload = { version: 1, error: String(e) };
  }
})();

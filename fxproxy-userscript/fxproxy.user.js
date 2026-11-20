// ==UserScript==
// @name         fxproxy 凭证提取（Firefox IP Protection）
// @namespace    fxproxy
// @version      1.2.0
// @description  在 accounts.firefox.com 上提取 sessionToken，按三种格式导出或一键推送到本地 fxproxy 面板
// @author       fxproxy
// @match        https://accounts.firefox.com/*
// @match        https://accounts.firefox.com.cn/*
// @run-at       document-idle
// @noframes
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_addStyle
// @grant        GM_setClipboard
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

/*
 * 为什么油猴版能拿到 token：
 * Chrome 插件读的是 chrome.cookies（能读 HttpOnly），但这个脚本根本不需要
 * cookie —— FxA 网页端把登录态放在 localStorage 的 __fxa_storage.accounts
 * 里（{ uid: { email, sessionToken } }），httpOnly 的 sessionToken 只是另一份
 * 副本。同源脚本直接读 localStorage 就够，所以油猴版和插件版拿到的数据一致。
 *
 * 与插件的差别：这里多了「直接推送到面板」——GM_xmlhttpRequest 不受页面 CSP
 * 和 CORS 限制，可以把整段凭证 POST 给 127.0.0.1 上的面板，省掉复制粘贴。
 */
(function () {
  "use strict";

  var HEX64 = /^[0-9a-fA-F]{64}$/;
  var FXS = /^fxs_[A-Za-z0-9_\-]+$/;
  var PAGE = (typeof unsafeWindow !== "undefined" && unsafeWindow) ? unsafeWindow : window;

  var DEFAULT_PANEL = "http://127.0.0.1:5566";

  /* ---------------------------------------------------------------- 提取 */

  function isToken(v) {
    return typeof v === "string" && (HEX64.test(v) || FXS.test(v));
  }

  // fxs_ 开头的是 32 字节的 base64url，解码回 hex —— 面板的 bootstrap 只吃 hex。
  function norm(v) {
    if (!v) return v;
    if (HEX64.test(v)) return v.toLowerCase();
    if (v.indexOf("fxs_") === 0) {
      var b64 = v.slice(4).replace(/-/g, "+").replace(/_/g, "/");
      while (b64.length % 4) b64 += "=";
      try {
        var bin = atob(b64), out = "";
        for (var i = 0; i < bin.length; i++) {
          out += ("0" + bin.charCodeAt(i).toString(16)).slice(-2);
        }
        if (HEX64.test(out)) return out;
      } catch (e) { /* 解不开就原样返回 */ }
    }
    return v;
  }

  function lsGet(key) {
    try { return PAGE.localStorage.getItem(key); } catch (e) { return null; }
  }

  function emailOf(uid, rec) {
    if (rec && rec.email) return rec.email;
    try {
      var alt = lsGet("__fxa_storage.email." + uid) || lsGet("_fxa_storage.email." + uid);
      if (alt) return JSON.parse(alt);
    } catch (e) { /* ignore */ }
    return null;
  }

  // 深挖任意 JSON 结构找 sessionToken 字段，Mozilla 改 key 名也不会瞎。
  function deepScan(value, found, depth) {
    if (depth > 6 || value == null) return;
    if (typeof value === "string") {
      if (isToken(value)) found.push(norm(value));
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
          found.push(norm(val));
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
        if (isToken(raw)) { found.push(norm(raw)); continue; }
        var parsed = null;
        try { parsed = JSON.parse(raw); } catch (e) { continue; }
        deepScan(parsed, found, 0);
      }
    } catch (e) { /* storage 可能被策略挡住 */ }
  }

  function scanCookies(found) {
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i].split("=");
      if (p.length < 2) continue;
      var val;
      try { val = decodeURIComponent(p.slice(1).join("=").trim()); }
      catch (e) { val = p.slice(1).join("=").trim(); }
      if (isToken(val)) found.push(norm(val));
    }
  }

  function collect() {
    var accounts = [], found = [];

    var acctRaw = lsGet("__fxa_storage.accounts") || lsGet("_fxa_storage.accounts");
    if (acctRaw) {
      try {
        var map = JSON.parse(acctRaw);
        Object.keys(map || {}).forEach(function (uid) {
          var rec = map[uid] || {};
          var st = rec.sessionToken || rec.session_token;
          if (isToken(st)) {
            accounts.push({
              uid: uid,
              email: emailOf(uid, rec),
              session_token: norm(st)
            });
            found.push(norm(st));
          }
        });
      } catch (e) { /* 结构坏了就走下面的通用扫描 */ }
    }

    // 兜底扫描只在 accounts 一个都没解析出来时才做。
    // 两路叠加的后果：1 个当前会话 + localStorage/sessionStorage/cookie 里的
    // 历史残留，一个账号导出 4 个 token，后 3 个送 Mozilla 全是
    // "Invalid authentication token: Missing authentication"。
    if (!found.length) {
      scanStorage(PAGE.localStorage, found);
      try { scanStorage(PAGE.sessionStorage, found); } catch (e) { /* ignore */ }
      scanCookies(found);
    }

    var seen = {}, tokens = [];
    for (var i = 0; i < found.length; i++) {
      var t = found[i];
      if (!t || seen[t]) continue;
      seen[t] = 1;
      tokens.push(t);
    }

    var aseen = {};
    accounts = accounts.filter(function (a) {
      if (aseen[a.session_token]) return false;
      aseen[a.session_token] = 1;
      return true;
    });

    return {
      version: 1,
      source: "fxproxy-userscript",
      origin: location.origin,
      extracted_at: new Date().toISOString(),
      accounts: accounts,
      session_tokens: tokens
    };
  }

  /* ---------------------------------------------------------------- 格式 */

  // 导出格式只列两种真正不同的东西。
  // 「Cookie 头」和裸 sessionToken 是同一串字符，只差 `sessionToken=` 前缀，
  // 面板两个入口吃的也是同一段文本 —— 摆两个选项等于让人做一次没有区别的
  // 选择，删掉。
  //
  // 没有 refresh_token：FxA 的 /oauth/token 要 Hawk 签名，浏览器端换不出来。
  var FORMATS = {
    // 与 chrome-extension/formats.js 的 FORMATS 逐项对应，改一处要同步另一处
    cookie: {
      label: "Cookie 头（默认，最省事）",
      ext: "txt",
      fn: function (p) {
        return p.session_tokens.map(function (t) { return "sessionToken=" + t; }).join("; ") + "\n";
      }
    },
    session: {
      label: "sessionToken（每行一个）",
      ext: "txt",
      fn: function (p) { return p.session_tokens.join("\n") + "\n"; }
    },
    email: {
      label: "邮箱（面板还需填密码）",
      ext: "txt",
      fn: function (p) {
        var out = [], seen = {};
        for (var i = 0; i < p.accounts.length; i++) {
          var m = p.accounts[i].email;
          if (m && !seen[m]) { seen[m] = 1; out.push(m); }
        }
        return out.join("\n") + (out.length ? "\n" : "");
      }
    }
  };

  /* ---------------------------------------------------------------- 界面 */

  GM_addStyle([
    "#fxp-ball{position:fixed;right:18px;bottom:18px;z-index:2147483000;width:46px;height:46px;",
    "border-radius:50%;background:#7c5cff;color:#fff;font:600 14px/46px system-ui,-apple-system,'Segoe UI',sans-serif;",
    "text-align:center;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.35);user-select:none;transition:transform .12s}",
    "#fxp-ball:hover{transform:scale(1.06)}",
    "#fxp-card{position:fixed;right:18px;bottom:76px;z-index:2147483001;width:380px;max-height:74vh;",
    "background:#1b1d23;color:#e8e8ee;border:1px solid #33363f;border-radius:14px;",
    "box-shadow:0 18px 50px rgba(0,0,0,.55);font:13px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif;",
    "display:none;flex-direction:column;overflow:hidden}",
    "#fxp-card.on{display:flex}",
    ".fxp-hd{padding:12px 14px;border-bottom:1px solid #2b2e36;display:flex;align-items:center;justify-content:space-between}",
    ".fxp-hd b{font-size:14px}",
    ".fxp-x{cursor:pointer;color:#8a8f9c;font-size:16px;line-height:1;padding:2px 4px}",
    ".fxp-x:hover{color:#fff}",
    ".fxp-bd{padding:12px 14px;overflow:auto}",
    ".fxp-row{display:flex;align-items:center;gap:8px;padding:6px 8px;background:#22252c;border-radius:8px;margin-bottom:6px}",
    ".fxp-row .i{color:#8a8f9c;width:16px;text-align:right;flex:none}",
    ".fxp-row .e{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".fxp-row .t{color:#8a8f9c;font:11px ui-monospace,Consolas,monospace;flex:none}",
    ".fxp-empty{color:#8a8f9c;padding:10px 0;text-align:center}",
    ".fxp-lab{color:#8a8f9c;font-size:11px;margin:10px 0 5px;letter-spacing:.3px}",
    ".fxp-sel,.fxp-inp{width:100%;box-sizing:border-box;background:#22252c;border:1px solid #343843;color:#e8e8ee;",
    "border-radius:8px;padding:7px 9px;font:12px system-ui,sans-serif;outline:none}",
    ".fxp-sel:focus,.fxp-inp:focus{border-color:#7c5cff}",
    ".fxp-btns{display:flex;gap:8px;margin-top:10px}",
    ".fxp-btn{flex:1;padding:8px 10px;border-radius:8px;border:1px solid #3a3f4c;background:#262a33;color:#e8e8ee;",
    "font:12px system-ui,sans-serif;cursor:pointer}",
    ".fxp-btn:hover{background:#2f3440}",
    ".fxp-btn.p{background:#7c5cff;border-color:#7c5cff;color:#fff}",
    ".fxp-btn.p:hover{background:#8b6fff}",
    ".fxp-btn:disabled{opacity:.5;cursor:default}",
    ".fxp-msg{margin-top:9px;font-size:11px;color:#8a8f9c;min-height:15px;word-break:break-all}",
    ".fxp-msg.ok{color:#59c98a}",
    ".fxp-msg.err{color:#ef6b6b}",
    ".fxp-ft{padding:9px 14px;border-top:1px solid #2b2e36;color:#6f7482;font-size:11px}"
  ].join(""));

  var panelUrl = GM_getValue("panelUrl", DEFAULT_PANEL) || DEFAULT_PANEL;

  var ball = document.createElement("div");
  ball.id = "fxp-ball";
  ball.title = "fxproxy 凭证提取";
  ball.textContent = "FX";

  var card = document.createElement("div");
  card.id = "fxp-card";
  card.innerHTML = [
    '<div class="fxp-hd"><b>fxproxy 凭证提取</b><span class="fxp-x" data-x>×</span></div>',
    '<div class="fxp-bd">',
    '  <div class="fxp-lab">Firefox 账号</div>',
    '  <div data-list></div>',
    '  <div class="fxp-lab">导出格式</div>',
    '  <select class="fxp-sel" data-fmt></select>',
    '  <div class="fxp-btns">',
    '    <button class="fxp-btn" data-copy>复制给面板</button>',
    '    <button class="fxp-btn" data-dl>下载</button>',
    '  </div>',
    '  <div class="fxp-lab">面板地址</div>',
    '  <input class="fxp-inp" data-panel spellcheck="false">',
    '  <div class="fxp-btns">',
    '    <button class="fxp-btn p" data-push>直接推送到面板</button>',
    '  </div>',
    '  <div class="fxp-msg" data-msg></div>',
    '</div>',
    '<div class="fxp-ft">凭证只在本机流转，脚本不上传任何数据</div>'
  ].join("");

  var $list = card.querySelector("[data-list]");
  var $fmt = card.querySelector("[data-fmt]");
  var $panel = card.querySelector("[data-panel]");
  var $msg = card.querySelector("[data-msg]");
  var $copy = card.querySelector("[data-copy]");
  var $dl = card.querySelector("[data-dl]");
  var $push = card.querySelector("[data-push]");

  Object.keys(FORMATS).forEach(function (k) {
    var o = document.createElement("option");
    o.value = k;
    o.textContent = FORMATS[k].label;
    $fmt.appendChild(o);
  });
  // 旧版本存过 json / cookie / lines 这些已删掉的 key，读回来要落回默认
  var savedFmt = GM_getValue("fmt", "cookie");
  $fmt.value = FORMATS[savedFmt] ? savedFmt : "session";
  $push.disabled = ($fmt.value === "email");
  $panel.value = panelUrl;

  function say(text, kind) {
    $msg.className = "fxp-msg" + (kind ? " " + kind : "");
    $msg.textContent = text || "";
  }

  var lastPayload = null;

  function refresh() {
    var p;
    try {
      p = collect();
    } catch (e) {
      say("提取失败：" + e, "err");
      return null;
    }
    lastPayload = p;

    $list.textContent = "";
    if (!p.accounts.length && !p.session_tokens.length) {
      var d = document.createElement("div");
      d.className = "fxp-empty";
      d.textContent = "没找到登录态 —— 先在 accounts.firefox.com 登录";
      $list.appendChild(d);
    } else {
      var rows = p.accounts.length ? p.accounts : p.session_tokens.map(function (t) {
        return { uid: "", email: "", session_token: t };
      });
      rows.forEach(function (a, i) {
        var row = document.createElement("div");
        row.className = "fxp-row";
        var n = document.createElement("span");
        n.className = "i";
        n.textContent = String(i + 1);
        var e = document.createElement("span");
        e.className = "e";
        e.textContent = a.email || (a.uid ? a.uid.slice(0, 12) : "（未知账号）");
        var t = document.createElement("span");
        t.className = "t";
        t.textContent = a.session_token.slice(0, 10) + "…";
        row.appendChild(n); row.appendChild(e); row.appendChild(t);
        $list.appendChild(row);
      });
    }
    return p;
  }

  function textOf(kind) {
    var p = lastPayload || collect();
    var f = FORMATS[kind] || FORMATS.cookie;
    return { text: f.fn(p), fmt: f };
  }

  function currentText() {
    return textOf($fmt.value);
  }

  function copyText(text) {
    try {
      GM_setClipboard(text, "text");
      return true;
    } catch (e) { /* 退回 DOM 方案 */ }
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  // 复制跟随下拉：选中哪一项就复制哪一项，粘贴时在面板选同一个方式。
  $copy.onclick = function () {
    var p = refresh();
    if (!p) return;
    var c = currentText();
    if (!c.text.trim()) { say("没有可复制的凭证", "err"); return; }
    var ok = copyText(c.text);
    say(ok ? "已复制（" + c.fmt.label + "）—— 面板选同一项粘贴"
           : "复制被浏览器拦了，改用「下载」",
        ok ? "ok" : "err");
  };

  $dl.onclick = function () {
    var p = refresh();
    if (!p) return;
    var c = currentText();
    if (!c.text.trim()) { say("没有可导出的内容", "err"); return; }
    var d = new Date();
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var name = "fxproxy-accounts-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) +
               "-" + pad(d.getHours()) + pad(d.getMinutes()) + "." + c.fmt.ext;
    var blob = new Blob([c.text], { type: "text/plain;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
      a.remove();
    }, 1000);
    say("已下载 " + name, "ok");
  };

  $panel.onchange = function () {
    panelUrl = ($panel.value || "").trim().replace(/\/+$/, "") || DEFAULT_PANEL;
    $panel.value = panelUrl;
    GM_setValue("panelUrl", panelUrl);
  };

  // GM_xmlhttpRequest 不走页面的 CORS/CSP，这是插件版做不到的：
  // 选中格式的文本直接 POST 给本地面板，面板按对应入口解析并入池。
  //
  // 面板 /api/login 只有两条文本入口：cookie（任意凭证文本，含 JSON）和
  // session（每行一个）。裸 token 两项都认，所以统一走 session 入口。
  // email 只有邮箱没有 token，面板没法单独拿它入池。
  function pushBody(kind) {
    var c = textOf(kind);
    if (kind === "session") return { mode: "session", session_token: c.text };
    if (kind === "cookie") return { mode: "cookie", cookie: c.text };
    return null;
  }

  $push.onclick = function () {
    var p = refresh();
    if (!p) return;
    var body = pushBody($fmt.value);
    if (!body) { say("邮箱格式里没有 token，面板无法单独用它入池", "err"); return; }
    $panel.onchange();
    $push.disabled = true;
    say("正在推送到 " + panelUrl + " …");
    GM_xmlhttpRequest({
      method: "POST",
      url: panelUrl + "/api/login",
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify(body),
      timeout: 240000,  // 每个 sessionToken 都要 bootstrap + 查额度，慢
      onload: function (r) {
        $push.disabled = false;
        var d = null;
        try { d = JSON.parse(r.responseText); } catch (e) { /* 非 JSON */ }
        if (!d) {
          say("面板返回异常（HTTP " + r.status + "）：" + (r.responseText || "").slice(0, 120), "err");
          return;
        }
        if (d.error) { say("失败：" + d.error, "err"); return; }
        var parts = [];
        if (d.added) parts.push("新增 " + d.added);
        if (d.updated) parts.push("更新 " + d.updated);
        if (d.skipped) parts.push("跳过 " + d.skipped);
        var tail = d.account_count != null ? "，现有 " + d.account_count + " 个账号" : "";
        say("成功" + (parts.length ? "（" + parts.join("，") + "）" : "") + tail,
            (d.warnings && d.warnings.length) ? "" : "ok");
      },
      onerror: function () {
        $push.disabled = false;
        say("连不上面板 —— 面板没启动？地址填错了？", "err");
      },
      ontimeout: function () {
        $push.disabled = false;
        say("推送超时（面板还在逐个验证账号），去面板看账号池", "err");
      }
    });
  };

  $fmt.onchange = function () {
    GM_setValue("fmt", $fmt.value);
    // 邮箱格式推送不了，按钮直接灰掉，省得点了才知道
    $push.disabled = ($fmt.value === "email");
    say($fmt.value === "email" ? "邮箱格式只能复制/下载，入池还需要密码" : "");
  };

  card.querySelector("[data-x]").onclick = function () {
    card.classList.remove("on");
  };

  ball.onclick = function () {
    if (card.classList.contains("on")) {
      card.classList.remove("on");
      return;
    }
    card.classList.add("on");
    refresh();
    say("");
  };

  // FxA 是单页应用，登录完成后不刷新页面；卡片开着时定时重扫，不然还是空列表。
  setInterval(function () {
    if (card.classList.contains("on")) refresh();
  }, 8000);

  document.body.appendChild(ball);
  document.body.appendChild(card);
})();

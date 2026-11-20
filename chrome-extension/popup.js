"use strict";

// 导出格式只包含插件真能生成的东西：Cookie 头、sessionToken、邮箱。
// refresh_token 不在这里 —— FxA 的 /oauth/token 要 Hawk 签名，
// 浏览器端用 Bearer 调只会拿到 401 "Invalid authentication token in
// request signature"。refresh_token 由面板拿 sessionToken 换，插件不重复这条路。

var msg = document.getElementById("msg");
var out = document.getElementById("out");
var btn = document.getElementById("extract");
var fmtSel = document.getElementById("format");
var lastPayload = null;

// 版本号从 manifest 读，改版本只改一处。判断装没装新版就看这行。
try {
  document.getElementById("ver").textContent = "v" + chrome.runtime.getManifest().version;
} catch (e) { /* 非扩展环境（直接打开 html 调试）忽略 */ }

function say(text, cls) {
  msg.textContent = text;
  msg.className = cls || "";
}

async function grab() {
  var tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  var tab = tabs[0];
  if (!tab || !/^https:\/\/accounts\.firefox\.com(\.cn)?\//.test(tab.url || "")) {
    throw new Error("请先打开并登录 accounts.firefox.com 再点提取");
  }
  // 兜底：有些环境（指纹浏览器 / 打开很久的标签页）不会自动注入
  // manifest 里声明的 content script，这里手动补一次。content.js 是 IIFE，
  // 重复执行只是覆盖同名函数，无害。
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  } catch (e) { /* 注入失败就继续，下面会返回 extractor not ready */ }

  var results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: function () {
      return (window.__fxproxyExtract || function () {
        return window.__fxproxyLastPayload || { version: 1, error: "extractor not ready" };
      })();
    }
  });
  var payload = results && results[0] && results[0].result;
  if (!payload || payload.error) throw new Error((payload && payload.error) || "未知错误");
  if (!FxproxyFormats.tokensOf(payload).length) {
    throw new Error("没抓到凭证。确认已登录，然后刷新页面再试一次。");
  }
  return payload;
}

btn.addEventListener("click", async function () {
  btn.disabled = true;
  say("读取中 …");
  try {
    var payload = await grab();
    lastPayload = payload;
    out.value = FxproxyFormats.format(payload, fmtSel.value);
    var n = FxproxyFormats.tokensOf(payload).length;
    try {
      await navigator.clipboard.writeText(out.value);
      say("已复制 " + n + " 个凭证，到面板选同一项粘贴", "ok");
    } catch (e) {
      out.focus(); out.select();
      say("自动复制被拦：文本框已全选，按 Ctrl+C", "err");
    }
  } catch (e) {
    say(e.message || String(e), "err");
  } finally {
    btn.disabled = false;
  }
});

// 换选项时用缓存重算，不必再点一次
fmtSel.addEventListener("change", function () {
  if (!lastPayload) return;
  out.value = FxproxyFormats.format(lastPayload, fmtSel.value);
});

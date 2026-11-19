"use strict";

var msg = document.getElementById("msg");
var out = document.getElementById("out");
var btn = document.getElementById("extract");
var dlBtn = document.getElementById("download");
var fmtSel = document.getElementById("format");
var lastPayload = null;

function say(text, cls) {
  msg.textContent = text;
  msg.className = cls || "";
}

// Re-render the textbox when the chosen format changes (no need to re-extract).
fmtSel.addEventListener("change", function () {
  if (lastPayload) out.value = FxproxyFormats.format(lastPayload, fmtSel.value);
});

async function grab() {
  var tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  var tab = tabs[0];
  if (!tab || !/^https:\/\/accounts\.firefox\.com(\.cn)?\//.test(tab.url || "")) {
    throw new Error("请先打开并登录 accounts.firefox.com 再点提取");
  }
  // 兜底：有些环境（指纹浏览器 / 已打开很久的标签页）不会自动注入
  // manifest 里声明的 content script，这里手动补一次。重复注入无害——
  // content.js 是 IIFE，重复执行只是覆盖同名函数。
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  } catch (e) {
    // 注入失败就继续，下面 executeScript 会返回 "extractor not ready"
  }
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

function render(payload) {
  lastPayload = payload;
  out.value = FxproxyFormats.format(payload, fmtSel.value);
}

btn.addEventListener("click", async function () {
  btn.disabled = true; dlBtn.disabled = true;
  say("读取中 …");
  try {
    var payload = await grab();
    render(payload);
    var nTok = FxproxyFormats.tokensOf(payload).length;
    var nAcct = (payload.accounts || []).length;
    try {
      await navigator.clipboard.writeText(out.value);
      say("已复制 " + nTok + " 个 token（" + nAcct + " 个账号），格式：" + fmtSel.selectedOptions[0].text, "ok");
    } catch (e) {
      out.focus(); out.select();
      say("已提取，但自动复制失败——请手动全选文本框内容复制", "err");
    }
  } catch (e) {
    say(e.message || String(e), "err");
  } finally {
    btn.disabled = false; dlBtn.disabled = false;
  }
});

dlBtn.addEventListener("click", async function () {
  btn.disabled = true; dlBtn.disabled = true;
  say("读取中 …");
  try {
    var payload = lastPayload || await grab();
    render(payload);
    var f = FxproxyFormats.FORMATS[fmtSel.value] || FxproxyFormats.FORMATS.json;
    var blob = new Blob([out.value], { type: "text/plain;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "fxproxy-credentials." + f.ext;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    say("已下载 " + a.download + "，可用 `fxproxy login --cookie-file` 导入", "ok");
  } catch (e) {
    say(e.message || String(e), "err");
  } finally {
    btn.disabled = false; dlBtn.disabled = false;
  }
});

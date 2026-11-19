"use strict";

var msg = document.getElementById("msg");
var out = document.getElementById("out");
var btn = document.getElementById("extract");

function say(text, cls) {
  msg.textContent = text;
  msg.className = cls || "";
}

btn.addEventListener("click", async function () {
  btn.disabled = true;
  say("读取中 …");
  try {
    var tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    var tab = tabs[0];
    if (!tab || !/^https:\/\/accounts\.firefox\.com(\.cn)?\//.test(tab.url || "")) {
      say("请先打开并登录 accounts.firefox.com 再点提取", "err");
      btn.disabled = false;
      return;
    }

    var results;
    try {
      // Prefer the live extractor (handles sign-in after page load).
      results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: function () {
          return (window.__fxproxyExtract || function () {
            return window.__fxproxyLastPayload || { version: 1, error: "extractor not ready" };
          })();
        }
      });
    } catch (injectErr) {
      say("无法注入页面（刷新账号页后重试）: " + injectErr.message, "err");
      btn.disabled = false;
      return;
    }

    var payload = results && results[0] && results[0].result;
    if (!payload || payload.error) {
      say("提取失败: " + ((payload && payload.error) || "未知错误"), "err");
      btn.disabled = false;
      return;
    }

    var nTok = (payload.session_tokens || []).length;
    var nAcct = (payload.accounts || []).length;
    if (!nTok) {
      say("没抓到凭证。确认已登录，然后刷新页面再试一次。", "err");
      btn.disabled = false;
      return;
    }

    var text = JSON.stringify(payload, null, 2);
    out.value = text;
    try {
      await navigator.clipboard.writeText(text);
      say("已复制 " + nTok + " 个 token（" + nAcct + " 个账号），去面板粘贴", "ok");
    } catch (clipErr) {
      // clipboardWrite permission should cover this; fall back to select-all
      out.focus();
      out.select();
      say("已提取，但自动复制失败——请手动全选文本框内容复制", "err");
    }
  } catch (e) {
    say("出错: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
});

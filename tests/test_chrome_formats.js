/* Node test for chrome-extension/formats.js — run: node tests/test_chrome_formats.js */
const F = require("../chrome-extension/formats.js");

const T1 = "a".repeat(64), T2 = "b".repeat(64);
const payload = {
  version: 1,
  source: "fxproxy-chrome-extension",
  origin: "https://accounts.firefox.com",
  accounts: [
    { uid: "u1", email: "a@example.com", session_token: T1 },
    { uid: "u2", email: "b@example.com", session_token: T2 }
  ],
  session_tokens: [T1, T2]
};

function check(cond, label) {
  if (!cond) throw new Error("FAIL: " + label);
  console.log("  ok  " + label);
}

check(F.tokensOf(payload).length === 2, "tokensOf 2 tokens");
check(F.toJSON(payload).includes('"session_token"'), "JSON includes accounts");

// 下拉里只留两种真正不同的东西：cookie 头和裸 token 内容一致，合并掉了
const fmtKeys = Object.keys(F.FORMATS);
check(fmtKeys.length === 2 && fmtKeys.join(",") === "session,email",
      "FORMATS = session,email（cookie 已合并）");
check(F.format(payload, "cookie") === F.toLines(payload),
      "已删掉的 cookie 选项回落到 session");

const lines = F.toLines(payload);
check(lines.trim().split("\n").length === 2, "lines = 2 rows");

const cookie = F.toCookieHeader(payload);
check((cookie.match(/sessionToken=/g) || []).length === 2, "cookie header 2 pairs");

const netscape = F.toNetscape(payload);
check(netscape.split("\n").filter(l => l.includes("sessionToken")).length === 2, "netscape 2 rows");
check(netscape.includes("accounts.firefox.com"), "netscape host");

const csv = F.toCsv(payload);
check(csv.includes(", "), "csv comma separated");

const accounts = F.toAccountLines(payload);
check(accounts.includes("a@example.com") && accounts.includes(T1), "account lines have email + token");

// Cyclic reference: a fxs_-prefixed token in accounts should still be counted
const fxs = "fxs_" + Buffer.alloc(32).toString("base64url");
const p2 = { accounts: [{ email: "x@y.z", sessionToken: fxs }], session_tokens: [] };
check(F.tokensOf(p2)[0] === fxs, "accounts fallback with sessionToken key");

console.log("\nALL CHROME FORMATS TESTS PASSED");

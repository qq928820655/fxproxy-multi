"""油猴脚本静态校验：格式集合、采集互斥、推送映射。

跑: python tests/test_userscript.py
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "fxproxy-userscript" / "fxproxy.user.js"

FAIL = []


def check(cond, label):
    print(("  ok  " if cond else "  FAIL ") + label)
    if not cond:
        FAIL.append(label)


src = JS.read_text(encoding="utf-8")

print("[userscript] 元数据")
m = re.search(r"// @version\s+(\S+)", src)
check(m is not None, "有 @version")
ver = m.group(1) if m else "0"
check(tuple(int(x) for x in ver.split(".")) >= (1, 2, 0), f"版本 >= 1.2.0（当前 {ver}）")

print("\n[userscript] 导出格式与面板对齐")
block = re.search(r"var FORMATS = \{(.+?)\n  \};", src, re.S)
check(block is not None, "找到 FORMATS 定义")
keys = re.findall(r"^\s{4}(\w+): \{", block.group(1), re.M) if block else []
check(keys == ["cookie", "session", "email"],
      f"恰好 cookie/session/email 三项，与扩展一致（实际 {keys}）")
check("FORMATS.json" not in src and "FORMATS.lines" not in src,
      "没有指向已删格式的引用")
check('"sessionToken=" +' in src, "Cookie 头拼 sessionToken= 前缀")

print("\n[userscript] 采集不与兜底叠加")
collect = re.search(r"function collect\(\) \{(.+?)\n  \}", src, re.S)
body = collect.group(1) if collect else ""
check("if (!found.length) {" in body, "兜底扫描被 accounts 命中挡住")
scan_calls = [ln for ln in body.splitlines() if "scanStorage(" in ln or "scanCookies(" in ln]
check(len(scan_calls) >= 3, "兜底三路仍在（localStorage/sessionStorage/cookie）")
# 兜底必须在 if 块内部：出现的行号要晚于 if 行
if body:
    idx_if = body.find("if (!found.length) {")
    idx_scan = min([body.find("scanStorage(PAGE.localStorage")] +
                   [i for i in [body.find("scanStorage(PAGE.sessionStorage")] if i >= 0] or [10**9])
    check(idx_scan > idx_if > -1, "扫描语句位于 if 块内")

print("\n[userscript] 推送走面板对应入口")
check('mode: "session"' in src and "session_token:" in src, "session 格式推 mode=session")
check('mode: "cookie"' in src, "cookie 格式推 mode=cookie")
check("function pushBody" in src, "推送按格式分派")
check('return null;' in src and "没有 token" in src, "email 格式拒绝推送并说明原因")

print("\n[userscript] UI 残留")
for bad in ("data-morebox", "data-copy2", "fxp-more"):
    check(bad not in src, f"已移除 {bad}")

print("\n[userscript] 语法")
node = shutil.which("node")
if node:
    r = subprocess.run([node, "--check", str(JS)], capture_output=True, text=True)
    check(r.returncode == 0, "node --check 通过" + ("" if r.returncode == 0 else f"：{r.stderr.strip()[:200]}"))
else:
    print("  skip 未装 node")

print()
if FAIL:
    print(f"FAILED ({len(FAIL)}): " + "; ".join(FAIL))
    sys.exit(1)
print("ALL USERSCRIPT TESTS PASSED")

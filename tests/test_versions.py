"""四个部件的版本号一致性校验。

项目不是单部件：fxproxy 包 / Chrome 扩展 / 油猴 / muxserver 各自打号。
本测试盯的是「改了代码没改号」和「文档里的号跟不上代码」两类事故。

跑: python tests/test_versions.py
"""
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAIL = []


def check(cond, label):
    print(("  ok  " if cond else "  FAIL ") + label)
    if not cond:
        FAIL.append(label)


def read(p):
    return (ROOT / p).read_text(encoding="utf-8")


# ---------- 1. 各处真实版本号 ----------
print("[版本] 读取各部件真实版本")

m = re.search(r'__version__\s*=\s*"([^"]+)"', read("fxproxy/__init__.py"))
pkg_ver = m.group(1) if m else ""
check(bool(pkg_ver), f"fxproxy/__init__.py __version__ = {pkg_ver}")

# Python 3.10 没有 tomllib，tomli 也不是主依赖；pyproject 只有一行 version，正则够用
m = re.search(r'^version\s*=\s*"([^"]+)"', read("pyproject.toml"), re.M)
toml_ver = m.group(1) if m else ""
check(toml_ver == pkg_ver, f"pyproject.toml 与 __version__ 一致（{toml_ver}）")

ext_ver = json.loads(read("chrome-extension/manifest.json")).get("version", "")
check(bool(ext_ver), f"chrome-extension/manifest.json version = {ext_ver}")

m = re.search(r"// @version\s+(\S+)", read("fxproxy-userscript/fxproxy.user.js"))
userscript_ver = m.group(1) if m else ""
check(bool(userscript_ver), f"油猴 @version = {userscript_ver}")

for name, ver in (("fxproxy", pkg_ver), ("Chrome 扩展", ext_ver), ("油猴", userscript_ver)):
    check(bool(re.fullmatch(r"\d+\.\d+\.\d+", ver)), f"{name} 版本号是 x.y.z 形态（{ver}）")

# ---------- 2. README 顶部表 ----------
print("\n[版本] README 组件表")

readme = read("README.md")
rows = dict(re.findall(r"^\| ([^|]+?) \| `([\d.]+)` \|", readme, re.M))
# 表里第一行是 fxproxy，第二行 Chrome 扩展，第三行油猴
check(rows.get("fxproxy（面板 + 代理 + CLI）") == pkg_ver,
      f"README fxproxy = {pkg_ver}（实际 {rows.get('fxproxy（面板 + 代理 + CLI）')}）")
check(rows.get("Chrome 扩展") == ext_ver,
      f"README 扩展 = {ext_ver}（实际 {rows.get('Chrome 扩展')}）")
check(rows.get("油猴脚本") == userscript_ver,
      f"README 油猴 = {userscript_ver}（实际 {rows.get('油猴脚本')}）")
check("muxserver" in readme, "README 列了 muxserver 一行")

# README 正文里不该再出现写死的旧版本号
stale = re.findall(r"fxproxy\s+`?(\d+\.\d+\.\d+)`?", readme)
check(all(v == pkg_ver for v in stale),
      f"README 无残留旧版本号（出现 {sorted(set(stale))}）")

# ---------- 3. CHANGELOG ----------
print("\n[版本] CHANGELOG")

cl = read("CHANGELOG.md")
first = re.search(r"^## \[([\d.]+)\]", cl, re.M)
check(first is not None and first.group(1) == pkg_ver,
      f"CHANGELOG 最新条目 = {pkg_ver}（实际 {first.group(1) if first else '无'}）")

head = cl[: first.start()] if first else cl
line = re.search(r"组件版本：fxproxy `([\d.]+)` · Chrome 扩展 `([\d.]+)` · 油猴 `([\d.]+)`",
                 cl[first.start():] if first else cl)
check(line is not None, "最新条目有「组件版本」行")
if line:
    check(line.group(1) == pkg_ver, f"组件版本行 fxproxy = {pkg_ver}")
    check(line.group(2) == ext_ver, f"组件版本行 扩展 = {ext_ver}")
    check(line.group(3) == userscript_ver, f"组件版本行 油猴 = {userscript_ver}")

check(line is not None and line.start() < (cl.find("### ") - first.start() if first else 10**9),
      "组件版本行位于条目的 ### 小节之前")

# ---------- 4. 打包产物与源码同步 ----------
print("\n[版本] 打包产物")

zpath = ROOT / "fxproxy-chrome-extension.zip"
if zpath.exists():
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if n.endswith("manifest.json")]
        packed = json.loads(z.read(names[0])) if names else {}
    check(packed.get("version") == ext_ver,
          f"zip 内 manifest = {ext_ver}（实际 {packed.get('version')}）")
else:
    print("  skip 没有 zip")

# crx：版本写在 CRX3 头部的 JSON 里，直接扫字节
cpath = ROOT / "fxproxy-chrome-extension.crx"
if cpath.exists():
    blob = cpath.read_bytes()[:4096]
    m = re.search(rb'"version"\s*:\s*"([\d.]+)"', blob) or re.search(rb'"version":\s*(\d+)', blob)
    if m:
        got = m.group(1).decode()
        check(got == ext_ver, f"crx 头部 version = {ext_ver}（实际 {got}）")
    else:
        print("  skip crx 头部无版本字段（CRX2）")
else:
    print("  skip 没有 crx")

# ---------- 5. 内部一致性 ----------
print("\n[版本] 部件间一致性")

# 扩展与油猴是同一件事的两个壳，导出项集合必须一样
ext_formats = re.findall(r"^    (\w+): \{[^\n]*label:", read("chrome-extension/manifest.json"), re.M)
src_ext = read("chrome-extension/formats.js")
ext_keys = re.findall(r"^\s{4}(\w+):\s*\{ label:", src_ext, re.M)
src_usr = read("fxproxy-userscript/fxproxy.user.js")
blk = re.search(r"var FORMATS = \{(.+?)\n  \};", src_usr, re.S)
usr_keys = re.findall(r"^\s{4}(\w+): \{", blk.group(1), re.M) if blk else []
check(ext_keys == usr_keys,
      f"扩展与油猴导出项一致（扩展 {ext_keys} / 油猴 {usr_keys}）")

print()
if FAIL:
    print(f"FAILED ({len(FAIL)}): " + "; ".join(FAIL))
    sys.exit(1)
print("ALL VERSION TESTS PASSED")

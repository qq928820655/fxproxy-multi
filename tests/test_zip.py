#!/usr/bin/env python3
"""锁住 VirtualBrowser 扩展包的两条硬不变量。

1. zip 内必须有且仅有一个顶层目录，manifest.json 在该目录里
   （平铺 zip 会让 VirtualBrowser 解出错误前缀 -> ENOENT content.js）
2. manifest 引用的每个文件都在包内，且扩展目录形态与已成功安装的参考包一致

跑法：python tests/test_zip.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_zip as bz  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


print("[zip] 从 chrome-extension/ 重新打包")
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "test.zip"
    r = bz.build_zip(bz.SRC_DIR, out, bz.DEFAULT_TOP)
    check(out.exists(), "zip 已生成")

    with zipfile.ZipFile(out) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        tops = {n.split("/", 1)[0] for n in names}
        check(len(tops) == 1, f"顶层目录唯一（实际 {sorted(tops)}）")
        check(tops == {bz.DEFAULT_TOP}, f"顶层目录名 = {bz.DEFAULT_TOP}")
        check(all("/" in n for n in names), "没有文件直接落在 zip 根（非平铺）")
        check(f"{bz.DEFAULT_TOP}/manifest.json" in names, "manifest.json 在顶层目录内")

        manifest = json.loads(z.read(f"{bz.DEFAULT_TOP}/manifest.json").decode("utf-8"))
        for key in ("content_scripts",):
            for cs in manifest.get(key, []):
                for js in cs.get("js", []):
                    check(f"{bz.DEFAULT_TOP}/{js}" in names, f"content_scripts 的 {js} 在包内")
        check(f"{bz.DEFAULT_TOP}/popup.html" in names, "popup.html 在包内")
        check(f"{bz.DEFAULT_TOP}/formats.js" in names, "formats.js 在包内")

    v = bz.verify_zip(out)
    check(v["ok"] is True, "verify_zip 通过")
    check(v["manifest_version"] == 3, "manifest_version = 3")

print("[zip] 反向用例：平铺包必须被判失败")
with tempfile.TemporaryDirectory() as td:
    flat = Path(td) / "flat.zip"
    with zipfile.ZipFile(flat, "w") as z:
        for f in sorted(bz.SRC_DIR.iterdir()):
            if f.is_file():
                z.write(f, f.name)  # 直接放根，模拟旧包
    fv = bz.verify_zip(flat)
    check(fv["ok"] is False, "平铺包被判为不合法")
    check(any("平铺" in p for p in fv["problems"]), "报出「平铺」问题")

print("[zip] 反向用例：manifest 引用缺失文件必须被拦下")
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "src"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({
        "manifest_version": 3, "name": "x", "version": "1",
        "content_scripts": [{"matches": ["<all_urls>"], "js": ["missing.js"]}],
    }), encoding="utf-8")
    try:
        bz.build_zip(src, Path(td) / "x.zip", "x")
        check(False, "缺文件时应当抛错")
    except SystemExit as e:
        check("missing.js" in str(e), "抛错并指出缺失文件")

print("[zip] 仓库里分发的 zip 不能是陈旧产物")
shipped = ROOT / "fxproxy-chrome-extension.zip"
src_manifest = json.loads((bz.SRC_DIR / "manifest.json").read_text(encoding="utf-8"))
if shipped.exists():
    sv = bz.verify_zip(shipped)
    check(sv["ok"] is True, "分发 zip 结构合法")
    with zipfile.ZipFile(shipped) as z:
        top = sv["top"]
        pkg_manifest = json.loads(z.read(f"{top}/manifest.json").decode("utf-8"))
        check(pkg_manifest["version"] == src_manifest["version"],
              f"分发 zip 版本号 = 源码版本号（{pkg_manifest['version']}）")
        for f in ("manifest.json", "content.js", "formats.js", "popup.html", "popup.js"):
            check(z.read(f"{top}/{f}") == (bz.SRC_DIR / f).read_bytes(),
                  f"分发 zip 的 {f} 与源码逐字节一致")
else:
    print("  --  未找到分发 zip（跳过，先跑 tools/build_zip.py）")

print(f"\nPASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)

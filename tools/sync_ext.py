#!/usr/bin/env python3
"""把 chrome-extension/ 同步到浏览器实际加载的「已解压扩展」目录。

为什么需要这个：打包 zip/crx 只是产物，浏览器里跑的是解压出来的那份副本。
改了 chrome-extension/ 而不覆盖那份副本，界面就一直是旧的 —— 现象是
「代码改了但没有任何变化」，而且很容易误判成没保存。

用法：
    python tools/sync_ext.py --list              # 只列出探测到的已加载副本
    python tools/sync_ext.py                     # 同步到所有探测到的副本
    python tools/sync_ext.py --path "D:/xx/ext"  # 同步到指定目录
    python tools/sync_ext.py --dry-run           # 看看会动哪些文件

探测位置：Chrome / Edge / Chromium / VirtualBrowser 的
<User Data>/extensions/<name>/ 以及 <User Data>/*/Extensions/<id>/<ver>/ 下
manifest.json 的 name 含 fxproxy 的目录。

同步后仍需在浏览器里「重新加载」扩展（或关掉该环境重开），
扩展宿主进程不会主动重读磁盘。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "chrome-extension"

# 浏览器 User Data 根目录候选（Windows 为主，Linux/macOS 给常见路径）
USER_DATA_HINTS = [
    r"%LOCALAPPDATA%\Google\Chrome\User Data",
    r"%LOCALAPPDATA%\Chromium\User Data",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
    r"%LOCALAPPDATA%\VirtualBrowser\User Data",
    r"%APPDATA%\VirtualBrowser\User Data",
    "~/.config/google-chrome",
    "~/.config/chromium",
    "~/Library/Application Support/Google/Chrome",
]

# 要被同步的文件（不含 INSTALL.txt / source.json 这类宿主生成的元数据）
SYNC_FILES = ("manifest.json", "popup.html", "popup.js", "formats.js", "content.js")


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def _manifest_name(p: Path) -> str:
    try:
        return json.loads((p / "manifest.json").read_text(encoding="utf-8")).get("name", "")
    except Exception:
        return ""


def _manifest_ver(p: Path) -> str:
    try:
        return json.loads((p / "manifest.json").read_text(encoding="utf-8")).get("version", "?")
    except Exception:
        return "?"


def discover() -> list[Path]:
    """找出所有「浏览器已加载的 fxproxy 扩展副本」。"""
    hits: list[Path] = []
    seen: set[str] = set()
    for hint in USER_DATA_HINTS:
        base = _expand(hint)
        if not base.is_dir():
            continue
        # 形态 A：<User Data>/extensions/<name>/  （VirtualBrowser 这类指纹浏览器）
        ext_root = base / "extensions"
        if ext_root.is_dir():
            for d in sorted(ext_root.iterdir()):
                if d.is_dir() and (d / "manifest.json").exists() and "fxproxy" in _manifest_name(d).lower():
                    if str(d).lower() not in seen:
                        seen.add(str(d).lower())
                        hits.append(d)
        # 形态 B：<User Data>/<Profile>/Extensions/<id>/<version>/
        for profile in base.iterdir():
            edir = profile / "Extensions"
            if not edir.is_dir():
                continue
            for eid in edir.iterdir():
                if not eid.is_dir():
                    continue
                for ver in eid.iterdir():
                    if ver.is_dir() and (ver / "manifest.json").exists() \
                            and "fxproxy" in _manifest_name(ver).lower():
                        if str(ver).lower() not in seen:
                            seen.add(str(ver).lower())
                            hits.append(ver)
    return hits


def sync_one(dst: Path, dry_run: bool = False) -> list[str]:
    """把 SRC 里 SYNC_FILES 覆盖到 dst。返回变更说明。"""
    lines: list[str] = []
    before = _manifest_ver(dst)
    for name in SYNC_FILES:
        s = SRC / name
        if not s.exists():
            lines.append(f"  ! 源缺失 {name}，跳过")
            continue
        d = dst / name
        same = d.exists() and d.read_bytes() == s.read_bytes()
        if same:
            lines.append(f"  = {name} 未变")
            continue
        if not dry_run:
            shutil.copy2(s, d)
        lines.append(f"  {'~' if d.exists() else '+'} {name}")
    after = _manifest_ver(dst) if not dry_run else _manifest_ver(SRC)
    if before != after:
        lines.append(f"  版本 {before} -> {after}")
    return lines


def _check_ids() -> list[str]:
    """粗校验 popup.html 里的 id 都被 popup.js 取得到，防止改名漏改。"""
    import re
    html = (SRC / "popup.html").read_text(encoding="utf-8")
    js = (SRC / "popup.js").read_text(encoding="utf-8")
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    missing = sorted(js_ids - html_ids)
    return missing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同步扩展源码到浏览器已加载的副本")
    ap.add_argument("--path", action="append", default=None, help="指定目标目录（可重复）")
    ap.add_argument("--list", action="store_true", help="只列出探测结果")
    ap.add_argument("--dry-run", action="store_true", help="不写文件")
    args = ap.parse_args(argv)

    if not SRC.is_dir():
        print(f"错误：找不到源目录 {SRC}")
        return 1

    missing = _check_ids()
    if missing:
        print(f"! popup.js 引用了 popup.html 里不存在的 id：{missing}")

    targets = [Path(p) for p in args.path] if args.path else discover()
    if not targets:
        print("没有探测到已加载的 fxproxy 扩展副本。")
        print("用 --path 手动指定扩展目录，例如：")
        print(r'  python tools/sync_ext.py --path "%LOCALAPPDATA%\VirtualBrowser\User Data\extensions\fxproxy-chrome-extension"')
        return 0

    print(f"源：{SRC}  (v{_manifest_ver(SRC)})")
    for t in targets:
        print(f"\n目标：{t}")
        print(f"  当前 v{_manifest_ver(t)}")
        if args.list:
            continue
        for line in sync_one(t, args.dry_run):
            print(line)

    if not args.list:
        print("\n完成。浏览器不会自动重读磁盘：")
        print("  · Chrome/Edge：chrome://extensions -> 该扩展卡片上点「重新加载」")
        print("  · VirtualBrowser 等指纹浏览器：关闭该环境窗口再打开（或插件管理里重新配置）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

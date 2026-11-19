"""打 Windows 一键部署发行包。

    python tools/build_release.py              # -> dist/fxproxy-<ver>-win.zip
    python tools/build_release.py --check      # 只校验已有包的结构

包里是「解压即可双击」的形态：顶层唯一目录 ``fxproxy-<ver>/``，里面有
``deploy/windows/install.bat``（它按 ``PSScriptRoot`` 往上两级找项目根，
所以这个层级不能改），以及已构建好的浏览器扩展 zip/crx。

刻意排除：``.git`` / ``.venv`` / ``.run`` / ``dist`` / ``config.json``
（后者含真实凭证）/ ``tools/.crx-key.pem``（签名私钥）/ 各种缓存。
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

# 整目录收进去的
DIRS = ("fxproxy", "deploy", "tools", "tests", "bridge", "chrome-extension", "docs")
# 单个文件收进去的
FILES = (
    "README.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    "config.example.json",
    "fxproxy-chrome-extension.zip",
    "fxproxy-chrome-extension.crx",
)
# 任何层级都不要
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "dist", "build", ".run", ".pytest_cache"}
SKIP_SUFFIX = (".pyc", ".pyo", ".tmp.yaml", ".tmp.log", ".tmp.json")
SKIP_NAMES = {".crx-key.pem", "config.json"}

# 部署时缺一个就跑不起来的东西
REQUIRED = (
    "deploy/windows/install.bat",
    "deploy/windows/install.ps1",
    "deploy/windows/ctl.ps1",
    "pyproject.toml",
    "fxproxy/__init__.py",
    "fxproxy/webui.py",
    "config.example.json",
)


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("pyproject.toml 里没找到 version")
    return m.group(1)


def _wanted(rel: Path) -> bool:
    if any(p in SKIP_DIRS for p in rel.parts[:-1]):
        return False
    if rel.name in SKIP_NAMES:
        return False
    if rel.name.endswith(SKIP_SUFFIX):
        return False
    return True


def collect() -> list[Path]:
    out: list[Path] = []
    for d in DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and _wanted(p.relative_to(ROOT)):
                out.append(p)
    for f in FILES:
        p = ROOT / f
        if p.is_file():
            out.append(p)
    return out


def build(out_path: Path, top: str) -> dict:
    files = collect()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            rel = p.relative_to(ROOT)
            zf.write(p, f"{top}/{rel.as_posix()}")
    return {"path": out_path, "top": top, "count": len(files),
            "bytes": out_path.stat().st_size}


def verify(path: Path, top: str) -> tuple[bool, list[str]]:
    """结构校验：唯一顶层目录 + 部署必需文件在 + 没混进私钥/真实配置。"""
    problems: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    tops = {n.split("/", 1)[0] for n in names}
    if tops != {top}:
        problems.append(f"顶层目录不唯一：{sorted(tops)}")
    for req in REQUIRED:
        if f"{top}/{req}" not in names:
            problems.append(f"缺 {req}")
    for bad in names:
        base = bad.rsplit("/", 1)[-1]
        if base in (".crx-key.pem", "config.json"):
            problems.append(f"混进了不该带的文件：{bad}")
        if "__pycache__" in bad or base.endswith(".pyc"):
            problems.append(f"混进了缓存：{bad}")
    # install.bat 必须和 install.ps1 同层：它按 PSScriptRoot 定位
    if f"{top}/deploy/windows/install.bat" in names:
        with zipfile.ZipFile(path) as zf:
            bat = zf.read(f"{top}/deploy/windows/install.bat").decode("ascii", "replace")
        if 'install.ps1" %*' not in bat:
            problems.append("install.bat 没有把参数透传给 install.ps1")
    return (not problems), problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打 Windows 发行包")
    ap.add_argument("-o", "--out", help="输出 zip 路径（默认 dist/fxproxy-<ver>-win.zip）")
    ap.add_argument("--check", action="store_true", help="只校验已有包")
    args = ap.parse_args(argv)

    ver = version()
    top = f"fxproxy-{ver}"
    out = Path(args.out) if args.out else DIST / f"{top}-win.zip"

    if args.check:
        ok, problems = verify(out, top)
        print(f"{out}  顶层={top}  {'OK' if ok else 'FAIL'}")
        for p in problems:
            print(f"  - {p}")
        return 0 if ok else 1

    meta = build(out, top)
    ok, problems = verify(out, top)
    print(f"已生成 {meta['path']}")
    print(f"  顶层目录 : {meta['top']}/")
    print(f"  文件数   : {meta['count']}")
    print(f"  大小     : {meta['bytes'] / 1024:.0f} KB")
    print(f"  校验     : ok={ok}")
    for p in problems:
        print(f"    - {p}")
    if not ok:
        return 1
    print("\n解压后双击：deploy\\windows\\install.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())

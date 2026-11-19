"""打发行包（Windows zip + Linux tar.gz）。

    python tools/build_release.py                 # 两个都打
    python tools/build_release.py --target win    # 只打 Windows zip
    python tools/build_release.py --target linux  # 只打 Linux tar.gz
    python tools/build_release.py --check         # 只校验已有包

Windows 用 zip：解压后双击 ``deploy/windows/install.bat``。
Linux 用 tar.gz：必须保留 shell 脚本的可执行位，zip 存不住 0755，
解压后得手动 chmod +x，所以那边一律 tar.gz。

两者都是「顶层唯一目录 ``fxproxy-<ver>/``」——Windows 的 install.ps1 按
``PSScriptRoot`` 往上两级找项目根，Linux 的 install.sh 按 ``../..`` 找源码，
这个层级不能改。

刻意排除：``.git`` / ``.venv`` / ``.run`` / ``dist`` / ``config.json``
（后者含真实凭证）/ ``tools/.crx-key.pem``（签名私钥）/ 各种缓存。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import tarfile
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
    """Windows zip 包。"""
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


def build_tar(out_path: Path, top: str) -> dict:
    """Linux tar.gz 包：保留 .sh 的 0755，否则解压后跑不起来。"""
    files = collect()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    count = 0
    with tarfile.open(out_path, "w:gz") as tf:
        for p in files:
            rel = p.relative_to(ROOT)
            info = tf.gettarinfo(str(p), arcname=f"{top}/{rel.as_posix()}")
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            # 只有部署脚本需要可执行位；其它给 0644，避免带上本机 umask 的痕迹
            info.mode = 0o755 if p.suffix == ".sh" else 0o644
            with open(p, "rb") as fh:
                tf.addfile(info, fh)
            count += 1
    return {"path": out_path, "top": top, "count": count,
            "bytes": out_path.stat().st_size}


def verify_tar(path: Path, top: str) -> tuple[bool, list[str]]:
    problems: list[str] = []
    with tarfile.open(path) as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        names = {m.name for m in members}
        crlf = []
        for m in members:
            if m.name.endswith(".sh") or m.name.endswith(".service.in"):
                data = tf.extractfile(m).read()
                if b"\r\n" in data:
                    crlf.append(m.name)
    tops = {n.split("/", 1)[0] for n in names}
    if tops != {top}:
        problems.append(f"顶层目录不唯一：{sorted(tops)}")
    for req in REQUIRED + ("deploy/linux/install.sh", "deploy/linux/fxproxy-ctl.sh"):
        if f"{top}/{req}" not in names:
            problems.append(f"缺 {req}")
    for m in members:
        base = m.name.rsplit("/", 1)[-1]
        if base in (".crx-key.pem", "config.json"):
            problems.append(f"混进了不该带的文件：{m.name}")
        if "__pycache__" in m.name or base.endswith(".pyc"):
            problems.append(f"混进了缓存：{m.name}")
        if base.endswith(".sh") and not (m.mode & 0o111):
            problems.append(f"{m.name} 没有可执行位（mode {oct(m.mode)}）")
        if "\r" in (m.name or ""):
            problems.append(f"{m.name} 名字里有 CR")
    for n in crlf:
        problems.append(f"{n} 是 CRLF，bash 会报 bad interpreter")
    return (not problems), problems


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
    # Linux 脚本混进 CRLF 会 bad interpreter（zip 给两边用，一并查）
    with zipfile.ZipFile(path) as zf:
        for n in names:
            if (n.endswith(".sh") or n.endswith(".service.in")) and b"\r\n" in zf.read(n):
                problems.append(f"{n} 是 CRLF，bash 会报 bad interpreter")
    return (not problems), problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打发行包（Windows zip / Linux tar.gz）")
    ap.add_argument("-o", "--out", help="输出路径（默认按 target 生成到 dist/）")
    ap.add_argument("--target", choices=("win", "linux", "all"), default="all",
                    help="打哪个包（默认 all）")
    ap.add_argument("--check", action="store_true", help="只校验已有包")
    args = ap.parse_args(argv)

    ver = version()
    top = f"fxproxy-{ver}"
    targets: list[str] = ["win", "linux"] if args.target == "all" else [args.target]

    rc = 0
    for target in targets:
        if args.out:
            out = Path(args.out) if len(targets) == 1 else DIST / (
                f"{top}-win.zip" if target == "win" else f"{top}-linux.tar.gz")
        else:
            out = DIST / (f"{top}-win.zip" if target == "win" else f"{top}-linux.tar.gz")

        verify_fn = verify if target == "win" else verify_tar
        if args.check:
            ok, problems = verify_fn(out, top)
            print(f"{out}  顶层={top}  {'OK' if ok else 'FAIL'}")
            for p in problems:
                print(f"  - {p}")
            rc |= 0 if ok else 1
            continue

        meta = build_tar(out, top) if target == "linux" else build(out, top)
        ok, problems = verify_fn(out, top)
        print(f"已生成 {meta['path']}")
        print(f"  平台     : {'Windows zip' if target == 'win' else 'Linux tar.gz'}")
        print(f"  顶层目录 : {meta['top']}/")
        print(f"  文件数   : {meta['count']}")
        print(f"  大小     : {meta['bytes'] / 1024:.0f} KB")
        print(f"  校验     : ok={ok}")
        for p in problems:
            print(f"    - {p}")
        if not ok:
            rc = 1

    if not args.check and rc == 0:
        print("\nWindows: 解压后双击 deploy\\windows\\install.bat")
        print("Linux  : tar xzf <包> && sudo ./fxproxy-*/deploy/linux/install.sh --token <TOKEN>")
    return rc


if __name__ == "__main__":
    sys.exit(main())

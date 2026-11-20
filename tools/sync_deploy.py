"""把仓库代码同步到部署目录（默认 E:\\fxproxy）。

手工 cp 一个个文件必然漏 —— 上一轮就漏了 fxproxy/__init__.py，部署目录
停在 0.4.4，面板跑的是两周前的代码。这里改成整目录比对，只覆盖有差异的。

运行产物不动：config.json、.run/、.venv/、dist/、__pycache__/、*.egg-info。
Chrome 扩展解压副本由 tools/sync_ext.py 单独管，这里跳过。

用法:
    python tools/sync_deploy.py                  # 同步到 E:\\fxproxy
    python tools/sync_deploy.py --dest D:\\app
    python tools/sync_deploy.py --dry-run        # 只列差异
    python tools/sync_deploy.py --restart        # 同步后重启服务
"""
import argparse
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 目录级的运行产物：不存在于仓库语义里，同步会破坏现场
SKIP_DIRS = {
    ".git", ".venv", "venv", ".run", "dist", "node_modules", "__pycache__",
    ".idea", ".vscode", "fxproxy-chrome-extension", ".pytest_cache", ".workbuddy",
}
# 文件级：配置、缓存、构建产物
SKIP_FILES = {
    "config.json", "fxproxy-chrome-extension.zip", "fxproxy-chrome-extension.crx",
}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".pid"}
SKIP_NAME_SUFFIX = (".egg-info",)


def skip(rel: Path) -> bool:
    parts = rel.parts
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    if any(p in SKIP_DIRS for p in parts[:1]):
        return True
    name = rel.name
    if name in SKIP_FILES or rel.suffix.lower() in SKIP_SUFFIX:
        return True
    if any(name.endswith(s) for s in SKIP_NAME_SUFFIX):
        return True
    # 顶层 一键*.bat 之类是部署目录自己的入口，仓库里没有就保留
    return False


def walk(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if skip(rel):
            continue
        yield rel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=r"E:\fxproxy")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    dest = Path(a.dest)
    if not dest.is_dir():
        print(f"部署目录不存在: {dest}")
        return 2

    new, changed, same = [], [], 0
    for rel in walk(ROOT):
        src = ROOT / rel
        dst = dest / rel
        if not dst.exists():
            new.append(rel)
        elif not filecmp.cmp(src, dst, shallow=False):
            changed.append(rel)
        else:
            same += 1

    # 只在部署目录存在、仓库里没有的文件：不删，只提示（可能是现场脚本）
    extra = [rel for rel in walk(dest) if not (ROOT / rel).exists()]

    if not a.quiet:
        print(f"源   {ROOT}")
        print(f"目标 {dest}")
        print(f"相同 {same} · 新增 {len(new)} · 变更 {len(changed)} · 仅部署目录有 {len(extra)}")
        for rel in new:
            print(f"  + {rel}")
        for rel in changed:
            print(f"  ~ {rel}")
        if extra:
            print("  仅部署目录有（保留不动）:")
            for rel in extra[:10]:
                print(f"    ? {rel}")

    if a.dry_run:
        return 0
    if not new and not changed:
        print("已是最新，无需同步")
        return 0

    for rel in new + changed:
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)

    print(f"已同步 {len(new) + len(changed)} 个文件")

    # 版本号是最容易判断"服务该不该重启"的信号
    init = dest / "fxproxy" / "__init__.py"
    ver = ""
    if init.exists():
        for ln in init.read_text(encoding="utf-8").splitlines():
            if ln.startswith("__version__"):
                ver = ln.split('"')[1] if '"' in ln else ""
                break
    if ver:
        print(f"部署目录版本 fxproxy {ver}")

    if a.restart:
        ctl = dest / "deploy" / "windows" / "ctl.ps1"
        if ctl.exists():
            print("重启服务…")
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", str(ctl), "restart"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            print(r.stdout.strip()[-800:])
            return r.returncode
        print("没找到 deploy/windows/ctl.ps1，跳过重启")
    elif ver:
        print("代码已换，跑着的进程仍是旧代码 —— 需要重启：一键停止.bat → 一键启动.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())

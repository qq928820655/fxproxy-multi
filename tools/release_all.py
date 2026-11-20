"""一条命令走完发布链：定版本 -> 测试 -> 打包 -> 同步部署 -> 同步扩展副本 -> 提交推送。

为什么需要它：以前每次改完靠手工清单收尾，清单里只有扩展和油猴，于是
fxproxy 包停在 0.4.4、E 盘跑两周前的代码、zip 忘了重打。手工清单一定会漏，
所以这里把六步串成一条链，任一步失败立即中止，不打印「完成」。

用法：
    python tools/release_all.py -m "fix(panel): 修 xxx"                # 最常用
    python tools/release_all.py -m "..." --bump patch                  # 顺带升 fxproxy 0.5.4 -> 0.5.5
    python tools/release_all.py -m "..." --bump minor --ext-version 1.3.0
    python tools/release_all.py -m "..." --no-git --no-deploy          # 只测试 + 打包
    python tools/release_all.py -m "..." --dry-run                     # 只打印计划

版本规则（四个部件各自打号）：
    patch  1.2.5 -> 1.2.6    修 bug、调文案、恢复选项
    minor  1.2.5 -> 1.3.0    加删功能、改接口、改数据格式
    major  1.2.5 -> 2.0.0    不兼容改动
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

DEPLOY = Path(r"E:\fxproxy")
EXT_COPY = DEPLOY / "fxproxy-chrome-extension"


# ---------------------------------------------------------------- 基础工具
def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def write(rel: str, text: str) -> None:
    (ROOT / rel).write_text(text, encoding="utf-8", newline="\n")


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def die(msg: str, r=None):
    print(f"\n  FAIL  {msg}")
    if r is not None:
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        for ln in out.splitlines()[-20:]:
            print("        " + ln)
    print("\n发布中止：制品未更新，别当成已完成。")
    sys.exit(1)


def step(n: int, title: str):
    print(f"\n[{n}/6] {title}")


# ---------------------------------------------------------------- 版本号
def pkg_ver() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', read("fxproxy/__init__.py"))
    return m.group(1) if m else ""


def ext_ver() -> str:
    return json.loads(read("chrome-extension/manifest.json")).get("version", "")


def usr_ver() -> str:
    m = re.search(r"// @version\s+(\S+)", read("fxproxy-userscript/fxproxy.user.js"))
    return m.group(1) if m else ""


def bump(v: str, part: str) -> str:
    a, b, c = (int(x) for x in v.split("."))
    if part == "major":
        return f"{a + 1}.0.0"
    if part == "minor":
        return f"{a}.{b + 1}.0"
    return f"{a}.{b}.{c + 1}"


def set_pkg_ver(v: str):
    s = read("fxproxy/__init__.py")
    s = re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{v}"', s, count=1)
    write("fxproxy/__init__.py", s)

    s = read("pyproject.toml")
    s = re.sub(r'^version\s*=\s*"[^"]+"', f'version = "{v}"', s, count=1, flags=re.M)
    write("pyproject.toml", s)

    s = read("README.md")
    s = re.sub(r"(\| fxproxy（面板 \+ 代理 \+ CLI） \| `)[\d.]+(` \|)",
               rf"\g<1>{v}\g<2>", s, count=1)
    write("README.md", s)


def set_ext_ver(v: str):
    s = read("chrome-extension/manifest.json")
    s = re.sub(r'"version"\s*:\s*"[^"]+"', f'"version": "{v}"', s, count=1)
    write("chrome-extension/manifest.json", s)


def set_usr_ver(v: str):
    s = read("fxproxy-userscript/fxproxy.user.js")
    s = re.sub(r"// @version\s+\S+", f"// @version      {v}", s, count=1)
    write("fxproxy-userscript/fxproxy.user.js", s)


def _bullet(msg: str) -> str:
    """多行提交说明在 CHANGELOG 里续行缩进 2 空格，否则会被当成新段落。"""
    lines = [ln.rstrip() for ln in (msg or "").strip().splitlines()]
    if not lines:
        return "-"
    out = ["- " + lines[0]]
    out += [("  " + ln) if ln.strip() else "" for ln in lines[1:]]
    return "\n".join(out)


def changelog_new_entry(pkg: str, ext: str, usr: str, msg: str):
    """插入新条目；已存在同一版本条目时只改组件版本行 + 追加一条变更。"""
    cl = read("CHANGELOG.md")
    comp = (f"组件版本：fxproxy `{pkg}` · Chrome 扩展 `{ext}` · 油猴 `{usr}`")
    comp_re = r"组件版本：fxproxy `[^`]+` · Chrome 扩展 `[^`]+` · 油猴 `[^`]+`"
    m = re.search(r"^## \[([\d.]+)\]", cl, re.M)

    if m and m.group(1) == pkg:
        end = cl.find("\n## [", m.start() + 1)
        end = len(cl) if end == -1 else end
        block = cl[m.start():end]
        block = re.sub(comp_re, comp, block, count=1)
        # 追加到第一个 ### 小节末尾
        head = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
        if head and f"- {head}" not in block:
            block = block.rstrip("\n") + "\n" + _bullet(msg) + "\n"
        write("CHANGELOG.md", cl[:m.start()] + block + cl[end:])
        return

    entry = (f"\n## [{pkg}] — {date.today().isoformat()}\n\n"
             f"{comp}\n\n### 变更\n\n{_bullet(msg)}\n")
    idx = cl.find("\n## [")
    if idx == -1:
        write("CHANGELOG.md", cl.rstrip("\n") + "\n" + entry)
    else:
        write("CHANGELOG.md", cl[:idx] + entry + cl[idx:])


# ---------------------------------------------------------------- 各步
def do_tests(quiet=True):
    r = run([PY, "tools/run_tests.py"] + (["-q"] if quiet else []))
    if r.returncode != 0:
        die("测试未通过 —— 先修测试，别打包一个挂着的版本", r)
    out = (r.stdout or "").strip().splitlines()
    print("  " + (out[-1] if out else "测试通过"))


def do_build():
    for name, script in (("zip", "tools/build_zip.py"), ("crx", "tools/build_crx.py")):
        r = run([PY, script])
        if r.returncode != 0:
            die(f"{name} 打包失败", r)
        tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
        print(f"  {name}: {tail[-1] if tail else 'ok'}")


def do_deploy(dry: bool):
    if not DEPLOY.is_dir():
        print(f"  skip 部署目录不存在：{DEPLOY}")
        return
    r = run([PY, "tools/sync_deploy.py"] + (["--dry-run"] if dry else []))
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        die("同步部署目录失败", r)
    lines = out.splitlines()
    brief = [ln for ln in lines if ln.startswith(("相同", "已同步", "已是最新", "部署目录版本"))]
    for ln in brief:
        print("  " + ln)


def do_ext_sync(dry: bool):
    flags = ["--dry-run"] if dry else []
    r = run([PY, "tools/sync_ext.py"] + flags)
    if r.returncode != 0:
        die("同步扩展副本失败", r)
    hits = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("目标：")]
    print(f"  浏览器副本 {len(hits)} 处" + ("" if hits else "（未探测到）"))

    if EXT_COPY.is_dir():
        r = run([PY, "tools/sync_ext.py", "--path", str(EXT_COPY)] + flags)
        if r.returncode != 0:
            die(f"同步 {EXT_COPY} 失败", r)
        print(f"  部署副本 {EXT_COPY}")


def do_git(msg: str):
    r = run(["git", "add", "-A"])
    if r.returncode != 0:
        die("git add 失败", r)

    r = run(["git", "commit", "-m", msg])
    blob = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and "nothing to commit" not in blob:
        die("git commit 失败", r)
    if "nothing to commit" in blob:
        print("  工作区无改动，跳过 commit")

    remote = (run(["git", "remote"]).stdout or "origin").split()
    remote = remote[0] if remote else "origin"
    branch = (run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout or "main").strip()

    for i in range(3):
        run(["git", "fetch", "-q", remote, branch])
        cnt = run(["git", "rev-list", "--count", f"HEAD..{remote}/{branch}"])
        behind = cnt.stdout.strip()
        behind = int(behind) if behind.isdigit() else 0
        if not behind:
            break
        print(f"  远端领先 {behind} 个提交，合并后重推")
        r = run(["git", "merge", f"{remote}/{branch}", "--no-edit", "-q"])
        if r.returncode != 0:
            die("与远端冲突，手工解决后重跑本脚本", r)

    r = run(["git", "push", remote, branch])
    if r.returncode != 0:
        die("git push 失败", r)
    head = run(["git", "log", "--oneline", "-1"]).stdout.strip()
    print(f"  {head}")


# ---------------------------------------------------------------- 汇总
def summary(t0: float):
    print("\n" + "-" * 58)
    print("  发布完成")
    print("-" * 58)
    print(f"  fxproxy       {pkg_ver()}")
    print(f"  Chrome 扩展   {ext_ver()}")
    print(f"  油猴          {usr_ver()}")
    dep_init = DEPLOY / "fxproxy" / "__init__.py"
    if dep_init.exists():
        m = re.search(r'__version__\s*=\s*"([^"]+)"', dep_init.read_text(encoding="utf-8"))
        dv = m.group(1) if m else "?"
        flag = "" if dv == pkg_ver() else "   <- 未同步"
        print(f"  部署目录      {dv}{flag}")
    for name in ("fxproxy-chrome-extension.zip", "fxproxy-chrome-extension.crx"):
        p = ROOT / name
        if p.exists():
            ts = time.strftime("%H:%M", time.localtime(p.stat().st_mtime))
            print(f"  {name:28} {ts}  {p.stat().st_size // 1024} KB")
    print(f"  耗时          {time_cost(t0)}")
    print("-" * 58)


def time_cost(t0: float) -> str:
    return f"{time.time() - t0:.1f}s"


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="测试 -> 打包 -> 同步 -> 提交，一条命令")
    ap.add_argument("-m", "--message", help="git 提交说明；--no-git 时可省略")
    ap.add_argument("--bump", choices=("patch", "minor", "major"),
                    help="升 fxproxy 包版本（同时写 CHANGELOG/README/pyproject）")
    ap.add_argument("--ext-version", help="直接把扩展版本设为 x.y.z")
    ap.add_argument("--userscript-version", help="直接把油猴版本设为 x.y.z")
    ap.add_argument("--no-tests", action="store_true")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--no-ext-sync", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘不提交")
    a = ap.parse_args()

    if not a.no_git and not a.message:
        ap.error("-m 必填（或加 --no-git 跳过提交）")

    t0 = time.time()
    old = (pkg_ver(), ext_ver(), usr_ver())

    # 1. 版本
    step(1, "版本号")
    if a.dry_run:
        plan = []
        if a.bump:
            plan.append(f"fxproxy {old[0]} -> {bump(old[0], a.bump)}")
        if a.ext_version:
            plan.append(f"扩展 {old[1]} -> {a.ext_version}")
        if a.userscript_version:
            plan.append(f"油猴 {old[2]} -> {a.userscript_version}")
        print("  " + ("; ".join(plan) if plan else "不变"))
    else:
        if a.bump:
            set_pkg_ver(bump(old[0], a.bump))
        if a.ext_version:
            set_ext_ver(a.ext_version)
        if a.userscript_version:
            set_usr_ver(a.userscript_version)
        if a.bump or a.ext_version or a.userscript_version:
            changelog_new_entry(pkg_ver(), ext_ver(), usr_ver(), a.message or "见提交记录")
        print(f"  fxproxy {old[0]} -> {pkg_ver()} · 扩展 {old[1]} -> {ext_ver()} · "
              f"油猴 {old[2]} -> {usr_ver()}")

    # 2. 测试
    step(2, "全量测试")
    if a.no_tests:
        print("  skip --no-tests")
    elif a.dry_run:
        print("  将跑 tools/run_tests.py")
    else:
        do_tests()

    # 3. 打包
    step(3, "打包制品")
    if a.dry_run:
        print("  将跑 build_zip.py / build_crx.py")
    else:
        do_build()

    # 4. 部署目录
    step(4, "同步部署目录")
    if a.no_deploy:
        print("  skip --no-deploy")
    else:
        do_deploy(a.dry_run)

    # 5. 扩展副本
    step(5, "同步扩展副本")
    if a.no_ext_sync:
        print("  skip --no-ext-sync")
    else:
        do_ext_sync(a.dry_run)

    # 6. git
    step(6, "提交推送")
    if a.no_git:
        print("  skip --no-git")
    elif a.dry_run:
        print(f"  将提交：{a.message}")
    else:
        do_git(a.message)

    if a.dry_run:
        print("\n(dry-run) 以上动作均未执行。")
        return 0

    summary(t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

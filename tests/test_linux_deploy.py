"""Linux 一键部署脚本测试。

不依赖 Linux 环境：语法用 bash -n，流程用 --dry-run 走完整条链路，
ctl 用临时 ctl.env + 假 fxproxy 验证输出。真正的 systemd 行为在真机上验。

运行: python tests/test_linux_deploy.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "deploy" / "linux"

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


def find_bash():
    """定位真正的 bash。

    Windows 上 ``shutil.which("bash")`` 会先撞到 ``C:\\Windows\\System32\\bash.exe``
    —— 那是 WSL 的入口，不是 Git Bash，跑我们的脚本会直接失败。优先按 git
    安装位置推导 ``<git>\\bin\\bash.exe``。
    """
    for env in ("FXPROXY_BASH", "BASH"):
        v = os.environ.get(env, "")
        if v and Path(v).is_file():
            return v
    g = shutil.which("git")
    if g:
        root = Path(g).resolve().parent.parent
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
    for cand in ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"):
        if Path(cand).is_file():
            return cand
    w = shutil.which("bash")
    if w and "system32" not in w.lower():
        return w
    return None


BASH = find_bash()


def bash(*args, **kw):
    return subprocess.run([BASH, *args], capture_output=True, text=True,
                          errors="replace", **kw)


def posix(p):
    """Windows 路径转成 Git Bash 能用的形式。"""
    try:
        r = subprocess.run(["cygpath", "-u", str(p)], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    return str(p).replace("\\", "/")


def main():
    if not BASH:
        print("跳过：没有可用的 bash")
        return 0
    print(f"[linux] bash = {BASH}")

    install = LINUX / "install.sh"
    ctl = LINUX / "fxproxy-ctl.sh"

    print("[linux] 文件与权限")
    check(install.is_file(), "install.sh 存在")
    check(ctl.is_file(), "fxproxy-ctl.sh 存在")
    check((LINUX / "fxproxy.service.in").is_file(), "fxproxy.service.in 存在")
    check((LINUX / "fxproxy-web.service.in").is_file(), "fxproxy-web.service.in 存在")
    check((LINUX / "README.md").is_file(), "README.md 存在")
    # 可执行位：Windows 工作区不保留，看 git 索引里的 mode
    modes = subprocess.run(["git", "ls-files", "-s", "deploy/linux/"], cwd=ROOT,
                           capture_output=True, text=True).stdout
    for f in ("install.sh", "fxproxy-ctl.sh"):
        check(re.search(rf"100755 [0-9a-f]+ \d+\tdeploy/linux/{f}", modes) is not None,
              f"{f} 在 git 里是 100755（可执行）")

    print("[linux] 首行与严格模式")
    for f in (install, ctl):
        body = f.read_text(encoding="utf-8")
        check(body.startswith("#!/usr/bin/env bash\n"), f"{f.name} shebang 正确")
        check("set -euo pipefail" in body, f"{f.name} 有 set -euo pipefail")
        check("\ufffd" not in body, f"{f.name} 无乱码替换字符")
        check("\r\n" not in body, f"{f.name} 是 LF（CRLF 会让 bash 报 bad interpreter）")

    print("[linux] 语法")
    for f in (install, ctl):
        r = bash("-n", posix(f))
        check(r.returncode == 0, f"{f.name} bash -n 通过 {r.stderr.strip()[:80]}")

    print("[linux] install.sh 覆盖的能力")
    body = install.read_text(encoding="utf-8")
    for opt in ("--dir", "--token", "--cookie-file", "--country", "--http-port",
                "--socks-port", "--web-port", "--host", "--web-host", "--sub-server",
                "--index-url", "--proxy", "--with-login-deps", "--no-service",
                "--no-start", "--user", "--system", "--uninstall", "--purge", "--dry-run"):
        check(opt in body, f"支持 {opt}")
    for pm in ("apt-get", "dnf", "yum", "pacman", "apk", "zypper"):
        check(pm in body, f"包管理器 {pm} 有分支")
    check("sys.exit(0 if sys.version_info >= (3, 10)" in body, "Python >= 3.10 硬门槛")
    check("loginctl enable-linger" in body, "用户级 systemd 会开 linger")
    check("systemctl --user" in body, "支持用户级 systemd")
    check("nohup" in body, "无 systemd 时降级 nohup")
    check("so_reuseaddr" in body.lower(), "端口探测用 SO_REUSEADDR")
    check("chmod(path, 0o600)" in body, "config.json 权限 600")
    check('"$SELF_DIR/../.."' in body or "$SELF_DIR/../.." in body,
          "优先用本地源码（发行包场景）")

    print("[linux] 模板占位符齐全")
    for tpl in ("fxproxy.service.in", "fxproxy-web.service.in"):
        text = (LINUX / tpl).read_text(encoding="utf-8")
        for ph in re.findall(r"%[A-Z_]+%", text):
            check(ph in ("%DIR%", "%CONFIG%", "%BIN%", "%WEB_HOST%", "%WEB_PORT%"),
                  f"{tpl} 占位符 {ph} 会被 install.sh 替换")
        check("ExecStart=" in text, f"{tpl} 有 ExecStart")
        check("WantedBy=" in text, f"{tpl} 有 WantedBy")

    print("[linux] --help / 坏参数")
    r = bash(posix(install), "--help")
    check(r.returncode == 0 and "--dry-run" in r.stdout, "--help 输出用法")
    r = bash(posix(install), "--nope")
    check(r.returncode == 2 and "未知参数" in (r.stderr + r.stdout), "未知参数退出码 2")

    print("[linux] --dry-run 走完整流程")
    r = bash(posix(install), "--dry-run", "--token", "T1,T2", "--country", "us",
             "--http-port", "18080", "--socks-port", "11080", "--web-port", "18765")
    out = r.stdout + r.stderr
    check(r.returncode == 0, f"dry-run 退出码 0（{r.returncode}）")
    check("dry-run" in out, "打印 dry-run 提示")
    check("Python" in out, "检测到 Python")
    check("使用本地源码" in out, "识别发行包内源码")
    check("-m venv" in out, "计划创建 venv")
    check("生成 config.json" in out and "country=US" in out, "计划写配置（国家已大写）")
    check("18080" in out and "11080" in out and "18765" in out, "端口透传到流程")
    check("完成" in out, "走到收尾")

    print("[linux] dry-run 不写任何东西")
    target = Path(tempfile.mkdtemp()) / "should_not_exist"
    r = bash(posix(install), "--dry-run", "--dir", posix(target), "--no-start")
    check(not target.exists(), "dry-run 没创建安装目录")

    print("[linux] ctl 在假环境下的输出")
    tmp = Path(tempfile.mkdtemp())
    fake_bin = tmp / "bin" / "fxproxy"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text("#!/usr/bin/env bash\n"
                        'if [[ "${1:-}" == "--version" ]]; then echo "fxproxy 0.3.1"; fi\n',
                        encoding="utf-8")
    py_dir = Path(sys.executable).parent
    env_file = tmp / "ctl.env"
    env_file.write_text(
        f"FXPROXY_DIR={posix(tmp)}\n"
        f"FXPROXY_SRC={posix(ROOT)}\n"
        f"FXPROXY_CONFIG={posix(tmp)}/config.json\n"
        f"FXPROXY_BIN={posix(fake_bin)}\n"
        f"FXPROXY_BIN_DIR={posix(py_dir)}\n"
        "FXPROXY_HTTP_PORT=18080\n"
        "FXPROXY_SOCKS_PORT=11080\n"
        "FXPROXY_WEB_PORT=18765\n"
        "FXPROXY_WEB_HOST=127.0.0.1\n"
        "FXPROXY_SYSTEMD=0\n"
        "FXPROXY_USER_MODE=0\n"
        'FXPROXY_UNITS=""\n'
        f"FXPROXY_CTL_BIN={posix(tmp)}/fake-ctl\n",
        encoding="utf-8")

    def ctl_run(cmd):
        return subprocess.run(
            [BASH, posix(ctl), cmd],
            capture_output=True, text=True,
            env={**__import__("os").environ, "FXPROXY_CTL_ENV": posix(env_file)})

    r = ctl_run("url")
    check(r.returncode == 0, "ctl url 正常退出")
    check("18765" in r.stdout and "clash.yaml" in r.stdout, "url 打印面板/订阅端口")
    check("?provider=1" in r.stdout, "url 打印 provider 地址")

    r = ctl_run("status")
    check(r.returncode == 0, "ctl status 正常退出")
    check("fxproxy 0.3.1" in r.stdout, "status 显示版本")
    check("未运行" in r.stdout, "空环境下显示未运行")
    check("18080" in r.stdout and "11080" in r.stdout, "status 显示两个代理端口")

    r = ctl_run("bogus")
    check(r.returncode == 2, "未知子命令退出码 2")

    fake_ctl = tmp / "fake-ctl"
    fake_ctl.write_text("#!/bin/sh\n", encoding="utf-8")
    r = ctl_run("uninstall")
    check(r.returncode == 0, "uninstall 在 nohup 模式下不炸")
    check(not fake_ctl.exists(), "uninstall 删掉自己安装的 ctl")

    print("\n[update] 一行更新脚本")
    upd = LINUX / "update.sh"
    check(upd.is_file(), "update.sh 存在")
    if upd.is_file():
        body = upd.read_text(encoding="utf-8")
        check(b"\r\n" not in upd.read_bytes(), "是 LF（Linux 上才能直接跑）")
        if os.name != "nt":
            check(os.access(upd, os.X_OK), "有可执行位")
        check("BASH_SOURCE" in body, "按 BASH_SOURCE 判断管道执行，不是看 -t 0")
        check('"$CTL" update' in body, "委托给 fxproxy-ctl update")
        check("FXPROXY_CTL_ENV" in body, "用户级安装时把 ctl.env 显式传下去")
        check("/etc/fxproxy/ctl.env" in body, "找得到 root 安装的 ctl.env")
        check("bootstrap.sh" in body, "没装过时给出安装命令")
        if BASH:
            r = subprocess.run([BASH, "-n", str(upd)], capture_output=True, text=True)
            check(r.returncode == 0, f"bash -n 通过（{r.stderr.strip()[:60]}）")
            r = subprocess.run([BASH, str(upd), "--help"], capture_output=True, text=True,
                               timeout=60)
            check(r.returncode == 0, "--help 退出 0")
            check("--check" in r.stdout, "--help 列出 --check")
            # 管道模式：cat 进来也要能自愈跑起来
            with open(upd, "rb") as f:
                r = subprocess.run([BASH, "-s", "--", "--help"], input=f.read(),
                                   capture_output=True, timeout=60)
            check(r.returncode == 0, "管道模式 --help 正常")

    print("\n[update] ctl 对发行包部署是真更新")
    ctl_body = (LINUX / "fxproxy-ctl.sh").read_text(encoding="utf-8")
    check("releases/latest" in ctl_body, "取 Release API")
    check("strip-components=1" in ctl_body, "按顶层目录剥离，盖到安装目录")
    check("--exclude=.venv" in ctl_body, "备份排除 .venv")
    check("backup-" in ctl_body, "覆盖前留备份")
    check("--check" in ctl_body, "支持 --check 只看不升")
    check("deploy/linux/install.sh" in ctl_body, "校验制品结构（缺 install.sh 就拒）")
    check("重跑 install.sh 覆盖安装即可" not in ctl_body,
          "不再只是提示「重跑 install.sh」——发行包部署有真更新路径")

    print(f"\nPASS={PASS}  FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

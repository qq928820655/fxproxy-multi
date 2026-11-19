#!/usr/bin/env python3
"""deploy/windows 静态回归：编码、线尾、参数顺序。

这些不是"能跑就行"的细节，每一条都对应过线上真实故障：
- bat 混进非 ASCII  -> cmd.exe 用 GBK 解析，中文注释里的字节被当成命令吞掉
- bat 用 LF         -> cmd.exe 偶尔把两行粘成一行
- ps1 没 BOM        -> Windows PowerShell 5.1 按 GBK 读，中文全乱、语法报错
- `run -c cfg`      -> argparse 顶层参数放子命令后面，直接 unrecognized arguments
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WD = ROOT / "deploy" / "windows"

PASS = 0
FAIL = 0


def check(ok, msg):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok  {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


# 子命令名，用来抓 "-c 写在子命令后面" 的反模式
SUBCOMMANDS = ("login", "accounts", "status", "token", "servers",
               "run", "export-clash", "web")


def main():
    print("[win] 文件齐全")
    for name in ("install.bat", "start.bat", "stop.bat", "status.bat",
                 "install.ps1", "ctl.ps1", "README.md"):
        check((WD / name).is_file(), f"{name} 存在")

    print("\n[win] .bat 编码与线尾")
    for bat in sorted(WD.glob("*.bat")):
        raw = bat.read_bytes()
        try:
            raw.decode("ascii")
            ascii_ok = True
        except UnicodeDecodeError:
            ascii_ok = False
        check(ascii_ok, f"{bat.name} 纯 ASCII（cmd 不会因 GBK 咬字节）")
        check(not raw.startswith(b"\xef\xbb\xbf"), f"{bat.name} 无 BOM")
        lone_lf = len(re.findall(rb"(?<!\r)\n", raw))
        check(lone_lf == 0, f"{bat.name} 全部 CRLF（裸 LF {lone_lf} 处）")
        check(b"pause" in raw, f"{bat.name} 双击场景有 pause")
        check(b'if "%~1"=="" pause' in raw or b'if "%~1"=="-q" exit /b 0' in raw,
              f"{bat.name} 有参数时不等按键（可被脚本调用）")

    print("\n[win] .ps1 编码")
    for ps in sorted(WD.glob("*.ps1")):
        raw = ps.read_bytes()
        check(raw.startswith(b"\xef\xbb\xbf"),
              f"{ps.name} 带 UTF-8 BOM（PowerShell 5.1 才不乱码）")
        body = raw[3:].decode("utf-8")
        check("\r\n" in body, f"{ps.name} 使用 CRLF")
        # 中文函数名/注释乱码的典型残留
        check("\ufffd" not in body, f"{ps.name} 无替换字符（没被错误转码）")

    print("\n[win] fxproxy 参数顺序（-c 必须在子命令前）")
    bad = []
    for ps in sorted(WD.glob("*.ps1")):
        text = ps.read_text(encoding="utf-8-sig")
        # 形如 "run", "-c"  或  status -c $Cfg  或  login ... -c $Cfg
        for m in re.finditer(r'"(%s)"\s*,\s*"-c"' % "|".join(SUBCOMMANDS), text):
            bad.append(f"{ps.name}: \"{m.group(1)}\", \"-c\"")
        for m in re.finditer(r'(?<![-"\w])(%s)\b[^\r\n]*?\s-c\s' % "|".join(SUBCOMMANDS), text):
            seg = m.group(0)
            if "-c" in seg.split("--")[0]:
                bad.append(f"{ps.name}: {seg.strip()}")
    check(not bad, "没有把 -c 写在子命令之后" + ("" if not bad else f" -> {bad}"))

    print("\n[win] -c 与子命令的正确形态存在")
    ctl = (WD / "ctl.ps1").read_text(encoding="utf-8-sig")
    ins = (WD / "install.ps1").read_text(encoding="utf-8-sig")
    check(re.search(r'"-c",\s*\$CfgPath,\s*"run"', ctl) is not None,
          "ctl.ps1 启动代理用 -c $CfgPath run")
    check(re.search(r'"-c",\s*\$CfgPath,\s*"web"', ctl) is not None,
          "ctl.ps1 启动面板用 -c $CfgPath web")
    check(re.search(r'&\s*\$Fx\s+-c\s+\$CfgPath\s+status', ctl) is not None,
          "ctl.ps1 查状态用 -c $CfgPath status")
    check(re.search(r'&\s*\$Fx\s+-c\s+\$CfgPath\s+login', ins) is not None,
          "install.ps1 导入凭证用 -c $CfgPath login")

    print("\n[win] install.bat 透传参数")
    ib = (WD / "install.bat").read_text(encoding="ascii")
    check('install.ps1" %*' in ib, "install.bat 用 %* 把参数透传给 install.ps1")

    print("\n[win] 端口/进程清理有防护")
    check("Test-PortOpen" in ctl, "ctl.ps1 起之前检查端口占用")
    check("socks_port" in ctl, "ctl.ps1 也检查 SOCKS5 端口占用")
    check("HasExited" in ctl, "ctl.ps1 检查进程是否秒退")
    check("Get-RunningPid" in ctl, "ctl.ps1 用 pid 文件防重复启动")

    print("\n[win] 端口冲突不硬上")
    check("Test-PortBusy $Old" in ins, "install.ps1 旧端口被占时重新选")
    check("自动顺延" in ins, "install.ps1 非交互重跑时顺延到空闲端口")
    check("1-65535" in ins, "install.ps1 校验端口范围")

    print("\n[win] 一键启动")
    sb = (WD / "start.bat")
    check(sb.is_file(), "start.bat 存在（双击即启动）")
    check("ctl.ps1\" start -Open" in sb.read_text(encoding="ascii"),
          "start.bat 调 ctl.ps1 start -Open（起服务并开面板）")
    check("[switch]$Shortcut" in ins, "install.ps1 支持 -Shortcut")
    check("New-StartShortcut" in ins, "有创建快捷方式的函数")
    check("WScript.Shell" in ins, "用 WScript.Shell 建 .lnk")
    check('"fxproxy 启动.lnk"' in ins, "快捷方式名固定，重复安装覆盖同一个")
    check("start.bat" in ins, "快捷方式指向 start.bat")
    check(".PARAMETER Shortcut" in ins, "帮助里有 -Shortcut 说明")

    print("\n[win] 卸载路径存在")
    check("Uninstall" in ins, "install.ps1 支持 -Uninstall")
    check("fxproxy-run" in ins or "schtasks" in ins.lower(),
          "install.ps1 处理了计划任务（-Autostart 的清理/注册）")

    print("\n[win] 复用的 venv 必须真的能 pip")
    # 只有 python.exe 就复用的旧逻辑会撞上「No module named pip」，
    # 然后每条 pip 命令都报同一句，还容易被误判成网络问题去重试镜像。
    check("function Test-VenvPip" in ins, "有 Test-VenvPip 检查 pip 可用")
    check("-m pip --version" in ins, "用 python -m pip --version 判定")
    check("ensurepip" in ins, "没有 pip 时先试 ensurepip 补")
    check("删掉重建 .venv" in ins, "补不上就重建 venv")
    check("venv 里没有 pip，无法安装依赖" in ins, "装依赖前还挡一道并给可执行结论")
    check("winget install Python.Python.3.12" in ins, "给出换完整版 Python 的具体命令")
    # 新建的 venv 也要验：精简版 Python 建出来就没有 pip
    check(ins.count("Test-VenvPip $VenvPy") >= 3, "新建 / 复用 / 装依赖前都验过")

    print(f"\nPASS={PASS}  FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

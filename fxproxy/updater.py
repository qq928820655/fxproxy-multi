"""面板/CLI 一键更新。

流程：查 GitHub 最新 release -> 按当前平台挑制品（win 用 zip，其余用
linux tar.gz）-> 下载校验 -> 生成一个脱离当前进程的升级脚本 -> 脚本等
几秒、停服务、覆盖源码、重装依赖、再拉起来。

为什么要脱离进程做：面板自己就是被更新的那个进程，它在覆盖自己源码的
时候不能还占着端口，也不能指望自己重启自己。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

from . import __version__ as VERSION

REPO = os.environ.get("FXPROXY_REPO", "qq928820655/fxproxy-multi")
BRANCH = os.environ.get("FXPROXY_BRANCH", "main")
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
UA = f"fxproxy-updater/{VERSION}"

# 国内直连 api.github.com 常被 reset；给一个可配的镜像前缀
GH_PROXY_PREFIX = os.environ.get("FXPROXY_GH_PROXY", "")


# ------------------------------------------------------------------ 环境识别
def platform_tag() -> str:
    """当前平台对应的制品后缀。macOS 复用 linux 源码包（纯 Python，无二进制）。"""
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def asset_suffix(plat: str | None = None) -> str:
    plat = plat or platform_tag()
    return "-win.zip" if plat == "win" else "-linux.tar.gz"


def platform_label(plat: str | None = None) -> str:
    return {"win": "Windows", "macos": "macOS", "linux": "Linux"}[plat or platform_tag()]


def run_dir(root: Path) -> Path:
    d = root / ".run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def detect_install() -> dict:
    """找出可覆盖的源码根目录。

    优先级：FXPROXY_HOME / FXPROXY_SRC 环境变量 -> 包所在目录的上一层
    -> cwd。都不像项目根就返回 unknown（更新按钮置灰）。
    """
    cands: list[Path] = []
    for key in ("FXPROXY_HOME", "FXPROXY_SRC"):
        v = os.environ.get(key)
        if v:
            cands.append(Path(v))
    cands.append(Path(__file__).resolve().parents[1])
    cands.append(Path.cwd())
    seen = set()
    for p in cands:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        if (rp / "pyproject.toml").is_file() and (rp / "fxproxy" / "__init__.py").is_file():
            return {
                "root": str(rp),
                "kind": "git" if (rp / ".git").exists() else "release",
                "writable": os.access(rp, os.W_OK) and os.access(rp / "fxproxy", os.W_OK),
            }
    return {"root": "", "kind": "unknown", "writable": False}


# ------------------------------------------------------------------ 版本比较
def parse_version(s) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(s or ""))
    return tuple(int(x) for x in nums[:4]) or (0,)


def is_newer(candidate, base) -> bool:
    return parse_version(candidate) > parse_version(base)


def version_of_tag(tag: str) -> str:
    return str(tag or "").strip().lstrip("vV")


# ------------------------------------------------------------------ 查最新版
# 匿名 GitHub API 每小时只有 60 次。面板上连点几下「检查更新」就可能打光，
# 然后一小时里都报限流。留个 60 秒缓存挡住手速。
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 60.0


def _ratelimit_hint(resp, body_empty: bool = False) -> str:
    """把 403 分成「额度用完」和「别的拒绝」，并算出几分钟后恢复。"""
    remaining = (resp.headers.get("x-ratelimit-remaining") or "").strip()
    reset = (resp.headers.get("x-ratelimit-reset") or "").strip()
    when = ""
    if reset.isdigit():
        secs = int(reset) - int(time.time())
        if secs > 0:
            import datetime as _dt
            hhmm = _dt.datetime.fromtimestamp(int(reset)).strftime("%H:%M")
            when = f"{max(1, secs // 60)} 分钟后（{hhmm}）恢复"
    if remaining == "0" or when:
        return ("GitHub API 额度用完了（匿名每小时 60 次）。"
                + (f"{when}。" if when else "过一会儿再试。")
                + "急的话在面板「GitHub 代理」里填代理，或设 FXPROXY_UPDATE_API "
                  "指向自建镜像。")
    return ("GitHub API 拒绝了请求（403），可能是代理返回的页面、或被墙。"
            "检查面板里的 GitHub 代理，或用 FXPROXY_GH_PROXY 套镜像。")


async def fetch_latest(api_url: str | None = None, proxy: str | None = None,
                       timeout: float = 12.0, use_cache: bool = True) -> dict:
    """拉最新 release。返回 {tag, version, name, notes, html_url, assets}。"""
    import aiohttp

    url = api_url or os.environ.get("FXPROXY_UPDATE_API") or API_LATEST.format(repo=REPO)
    key = f"{url}|{proxy or ''}"
    if use_cache:
        hit = _CACHE.get(key)
        if hit and (time.time() - hit[0]) < CACHE_TTL:
            return dict(hit[1])
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=timeout)
        ) as sess:
            async with sess.get(url, headers=headers, proxy=proxy or None) as r:
                if r.status == 404:
                    raise RuntimeError("仓库还没有 Release（或 API 地址不对）")
                if r.status == 403:
                    raise RuntimeError(_ratelimit_hint(r))
                r.raise_for_status()
                data = await r.json()
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"连接超时（{timeout:.0f}s）：{url}") from e
    except aiohttp.ClientError as e:
        raise RuntimeError(f"请求失败：{e.__class__.__name__} {e}") from e

    tag = data.get("tag_name") or data.get("name") or ""
    assets = [
        {"name": a.get("name", ""), "url": a.get("browser_download_url", ""),
         "size": a.get("size", 0)}
        for a in (data.get("assets") or [])
    ]
    out = {
        "tag": tag,
        "version": version_of_tag(tag),
        "name": data.get("name") or tag,
        "notes": (data.get("body") or "")[:4000],
        "html_url": data.get("html_url") or "",
        "published_at": data.get("published_at") or "",
        "assets": assets,
    }
    _CACHE[key] = (time.time(), out)
    return dict(out)


def pick_asset(assets: list[dict], tag: str, plat: str | None = None) -> dict | None:
    """按平台挑制品：先精确匹配 fxproxy-<ver>-xxx，再退到后缀匹配。"""
    suf = asset_suffix(plat)
    exact = f"fxproxy-{version_of_tag(tag)}{suf}"
    for a in assets:
        if a.get("name") == exact:
            return a
    for a in assets:
        if str(a.get("name", "")).endswith(suf):
            return a
    return None


def asset_url(url: str, gh_proxy: str | None = None) -> str:
    """给下载地址套镜像前缀（https://ghproxy.net/ 这种）。"""
    p = (gh_proxy if gh_proxy is not None else GH_PROXY_PREFIX) or ""
    if not p:
        return url
    if not p.endswith("/"):
        p += "/"
    return p + url


# ------------------------------------------------------------------ 下载校验
async def download(url: str, dest: Path, proxy: str | None = None,
                   timeout: float = 30.0) -> int:
    import aiohttp

    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA}
    total = 0
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=timeout, sock_read=timeout)
    ) as sess:
        async with sess.get(url, headers=headers, proxy=proxy or None) as r:
            if r.status >= 400:
                raise RuntimeError(f"下载失败 HTTP {r.status}：{url}")
            with open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 16):
                    f.write(chunk)
                    total += len(chunk)
    # 只挡空响应：内容重复度高的包压缩后可能只有几十字节，用大小当质量
    # 判据会误杀。是不是真包交给 verify_archive 解析。
    if total < 16:
        raise RuntimeError(f"下载到的文件只有 {total} 字节，不像是发行包")
    return total


def _zip_top_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def _tar_members(path: Path):
    with tarfile.open(path, "r:gz") as tf:
        return [(m.name, m.isfile(), m.mode) for m in tf.getmembers()]


def verify_archive(path: Path) -> list[str]:
    """检查包结构。返回问题列表，空列表表示没问题。"""
    problems: list[str] = []
    if not path.is_file():
        return [f"文件不存在：{path}"]
    if path.stat().st_size == 0:
        return ["文件是空的"]
    try:
        if path.suffix == ".zip":
            names = _zip_top_names(path)
        else:
            names = [n for n, isfile, _ in _tar_members(path) if isfile]
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        return [f"不是有效的归档：{e}"]
    if not names:
        return ["归档是空的"]
    tops = {n.split("/", 1)[0] for n in names if n}
    if len(tops) != 1:
        problems.append(f"顶层目录不唯一：{sorted(tops)[:4]}")
        return problems
    top = next(iter(tops))
    if f"{top}/pyproject.toml" not in names:
        problems.append("缺 pyproject.toml")
    if f"{top}/fxproxy/__init__.py" not in names:
        problems.append("缺 fxproxy/__init__.py")
    if any(n.endswith(".crx-key.pem") or n.endswith("/config.json") for n in names):
        problems.append("包里混进了私钥或真实配置")
    return problems


# ------------------------------------------------------------------ 升级脚本
_BASH_TPL = r"""#!/usr/bin/env bash
# 由 fxproxy 面板生成：在进程外完成升级。别手动改。
set -uo pipefail
LOG=__LOG__
PKG=__PKG__
DEST=__DEST__
RUN=__RUN__
WANT=__WANT__
FROM=__FROM__
WH=__WH__
WP=__WP__
CFG=__CFG__
exec >>"$LOG" 2>&1
say() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
status() {
  printf 'state=%s\nfrom=%s\nto=%s\nts=%s\nmessage=%s\n' "$1" "$FROM" "$WANT" "$(date +%s)" "${2:-}" >"$RUN/update.status"
}
fail() { say "FAIL $*"; status fail "$*"; exit 1; }

say "== 升级 $FROM -> $WANT 开始 =="
# 先让发起请求的 HTTP 响应落地，否则浏览器拿到的是断连
sleep 3

MODE=__MODE__
SYSD=0
SCOPE=""
if [[ "$MODE" == "systemd" ]]; then
  if systemctl list-unit-files 2>/dev/null | grep -q '^fxproxy'; then
    SYSD=1; SCOPE=""
  elif systemctl --user list-unit-files 2>/dev/null | grep -q '^fxproxy'; then
    SYSD=1; SCOPE="--user"
  fi
fi

stop_all() {
  if [[ $SYSD -eq 1 ]]; then
    systemctl $SCOPE stop fxproxy-web fxproxy 2>/dev/null || true
    return
  fi
  if [[ -x "$(command -v fxproxy-ctl 2>/dev/null || echo /nonexistent)" ]]; then
    fxproxy-ctl stop >/dev/null 2>&1 || true
  fi
  local f pid
  for f in "$RUN"/*.pid; do
    [[ -f "$f" ]] || continue
    pid=$(cat "$f" 2>/dev/null || echo "")
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    rm -f "$f"
  done
  sleep 1
}

start_all() {
  if [[ $SYSD -eq 1 ]]; then
    systemctl $SCOPE start fxproxy 2>/dev/null || true
    systemctl $SCOPE start fxproxy-web 2>/dev/null || true
    return
  fi
  if [[ -x "$(command -v fxproxy-ctl 2>/dev/null || echo /nonexistent)" ]]; then
    fxproxy-ctl start >/dev/null 2>&1 || true
    return
  fi
  ( cd "$DEST" && nohup "$DEST/.venv/bin/python" -m fxproxy -c "$CFG" web \
      --no-open --web-host "$WH" --web-port "$WP" >>"$RUN/web.log" 2>&1 & \
    echo $! >"$RUN/web.pid" )
}

say "停止服务 ..."
stop_all

say "覆盖源码 $DEST ..."
if [[ -d "$DEST/.git" ]]; then
  command -v git >/dev/null 2>&1 || fail "需要 git，但没装"
  git -C "$DEST" fetch --depth 1 origin __BRANCH__ >>"$LOG" 2>&1 || fail "git fetch 失败"
  git -C "$DEST" reset --hard FETCH_HEAD >>"$LOG" 2>&1 || fail "git reset 失败"
else
  [[ -f "$PKG" ]] || fail "制品不见了：$PKG"
  TMPX="$RUN/update-x"
  rm -rf "$TMPX"; mkdir -p "$TMPX"
  tar xzf "$PKG" -C "$TMPX" || fail "解压失败"
  TOP=$(find "$TMPX" -maxdepth 1 -mindepth 1 -type d | head -1)
  [[ -n "$TOP" && -f "$TOP/pyproject.toml" ]] || fail "包结构不对，缺 pyproject.toml"
  # 备份只留代码，不拷 .venv（几百 MB）。逐个确认存在再打包：直接列一串
  # 目录名时，缺任何一个 tar 就整体失败，等于没备份。
  BAKLIST=""
  for d in fxproxy deploy chrome-extension tools pyproject.toml config.example.json; do
    [[ -e "$DEST/$d" ]] && BAKLIST="$BAKLIST $d"
  done
  if [[ -n "$BAKLIST" ]]; then
    # shellcheck disable=SC2086
    tar czf "$RUN/backup-$WANT.tar.gz" -C "$DEST" $BAKLIST 2>/dev/null \
      && say "已备份到 $RUN/backup-$WANT.tar.gz" || say "备份失败（继续）"
  else
    say "目录里没有可备份的代码（继续）"
  fi
  # --strip-components=1：把包内唯一顶层目录拆掉盖到 DEST。
  # 包里没有 config.json / .venv / .run，所以不会覆盖凭证与虚拟环境。
  tar xzf "$PKG" -C "$DEST" --strip-components=1 || fail "覆盖失败"
  rm -rf "$TMPX"
fi

if [[ -x "$DEST/.venv/bin/pip" ]]; then
  say "重装依赖 ..."
  "$DEST/.venv/bin/pip" install -q -e "$DEST" >>"$LOG" 2>&1 \
    || say "pip 失败（继续，旧依赖多半还能用）"
fi

say "启动服务 ..."
start_all
sleep 4

LEFT=0
if [[ $SYSD -eq 1 ]]; then
  systemctl $SCOPE is-active --quiet fxproxy-web 2>/dev/null || LEFT=1
else
  [[ -n "$(command -v curl 2>/dev/null)" ]] && {
    curl -fsS -m 5 "http://$WH:$WP/api/state" >/dev/null 2>&1 || LEFT=1
  }
fi

if [[ $LEFT -eq 1 ]]; then
  say "服务没起来，看日志：$RUN/web.log / journalctl -u fxproxy-web"
  status warn "已覆盖到 $WANT，但服务未确认启动"
else
  say "== 升级完成：$FROM -> $WANT =="
  status ok "已升级到 $WANT"
fi
rm -f "$PKG"
"""

_PS_TPL = r"""# 由 fxproxy 面板生成：在进程外完成升级。别手动改。
$ErrorActionPreference = 'Continue'
$Log   = __LOG__
$Pkg   = __PKG__
$Dest  = __DEST__
$Run   = __RUN__
$Want  = __WANT__
$From  = __FROM__
$WebHost = '__WH__'
$WebPort = __WP__
$Cfg   = __CFG__

function Say([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Add-Content -Path $Log -Value $line -Encoding utf8
}
function Set-Status([string]$state, [string]$msg) {
    $txt = "state=$state`nfrom=$From`nto=$Want`nts={0}`nmessage=$msg`n" -f ([int][double]::Parse((Get-Date -UFormat %s)))
    Set-Content -Path (Join-Path $Run 'update.status') -Value $txt -Encoding utf8
}

Say "== 升级 $From -> $Want 开始 =="
Start-Sleep -Seconds 3

Say "停止服务 ..."
Get-ChildItem -Path $Run -Filter '*.pid' -ErrorAction SilentlyContinue | ForEach-Object {
    $procId = 0
    if ([int]::TryParse((Get-Content $_.FullName -ErrorAction SilentlyContinue), [ref]$procId)) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
}
# 兜底：占着面板端口的进程
Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

try {
    if (Test-Path (Join-Path $Dest '.git')) {
        Say "git 模式更新 ..."
        git -C $Dest fetch --depth 1 origin __BRANCH__ 2>&1 | Add-Content $Log
        if ($LASTEXITCODE -ne 0) { throw "git fetch 失败" }
        git -C $Dest reset --hard FETCH_HEAD 2>&1 | Add-Content $Log
        if ($LASTEXITCODE -ne 0) { throw "git reset 失败" }
    } else {
        if (-not (Test-Path $Pkg)) { throw "制品不见了：$Pkg" }
        $tmp = Join-Path $Run 'update-x'
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        Say "解压 $Pkg ..."
        Expand-Archive -Path $Pkg -DestinationPath $tmp -Force
        $top = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (-not $top -or -not (Test-Path (Join-Path $top.FullName 'pyproject.toml'))) {
            throw "包结构不对，缺 pyproject.toml"
        }
        # 备份代码（不拷 .venv）
        $bak = Join-Path $Run ("backup-$Want")
        New-Item -ItemType Directory -Path $bak -Force | Out-Null
        foreach ($n in @('fxproxy','deploy','chrome-extension','tools')) {
            $s = Join-Path $Dest $n
            if (Test-Path $s) { Copy-Item $s -Destination $bak -Recurse -Force -ErrorAction SilentlyContinue }
        }
        Say "覆盖源码 ..."
        # 逐项覆盖：目标已存在时先删掉，否则 Copy-Item -Recurse 会套成 <dest>\<name>\<name>
        foreach ($item in (Get-ChildItem $top.FullName -Force)) {
            if ($item.Name -in @('config.json', '.venv', '.run', '.git')) { continue }
            $target = Join-Path $Dest $item.Name
            if (Test-Path $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
            Copy-Item $item.FullName -Destination $target -Recurse -Force
        }
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    $py = Join-Path $Dest '.venv\Scripts\python.exe'
    if (Test-Path $py) {
        Say "重装依赖 ..."
        & $py -m pip install -q -e $Dest 2>&1 | Add-Content $Log
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source }
    }

    Say "启动服务 ..."
    Start-Process -FilePath $py `
        -ArgumentList @('-m','fxproxy','-c',$Cfg,'web','--no-open','--web-host',$WebHost,'--web-port',"$WebPort") `
        -WorkingDirectory $Dest -WindowStyle Hidden
    Start-Sleep -Seconds 5
    Say "== 升级完成：$From -> $Want =="
    Set-Status 'ok' "已升级到 $Want"
} catch {
    Say "FAIL $_"
    Set-Status 'fail' "$_"
    exit 1
}
Remove-Item $Pkg -Force -ErrorAction SilentlyContinue
"""


def _ps_quote(p: Path | str) -> str:
    return "'" + str(p).replace("'", "''") + "'"


def build_script(kind: str, dest: Path, pkg: Path | None, run: Path, want: str,
                 cur: str, cfg: Path, web_host: str, web_port: int,
                 mode: str = "") -> tuple[Path, str]:
    """生成升级脚本，返回 (脚本路径, 类型 "bash"|"powershell")。"""
    run.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        log = run / "update.log"
        body = (_PS_TPL
                .replace("__LOG__", _ps_quote(log))
                .replace("__PKG__", _ps_quote(pkg or ""))
                .replace("__DEST__", _ps_quote(dest))
                .replace("__RUN__", _ps_quote(run))
                .replace("__CFG__", _ps_quote(cfg))
                .replace("__WANT__", _ps_quote(want))
                .replace("__FROM__", _ps_quote(cur))
                .replace("__BRANCH__", BRANCH)
                .replace("__WH__", web_host.replace("'", "''"))
                .replace("__WP__", str(int(web_port))))
        script = run / f"update-{int(time.time())}.ps1"
        # PowerShell 5.1 按 ANSI 读无 BOM 文件，中文会乱码，必须带 BOM
        script.write_text(body, encoding="utf-8-sig", newline="\r\n")
        return script, "powershell"

    log = run / "update.log"
    q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"  # noqa: E731
    body = (_BASH_TPL
            .replace("__LOG__", q(log))
            .replace("__PKG__", q(pkg or ""))
            .replace("__DEST__", q(dest))
            .replace("__RUN__", q(run))
            .replace("__CFG__", q(cfg))
            .replace("__WANT__", q(want))
            .replace("__FROM__", q(cur))
            .replace("__WH__", q(web_host))
            .replace("__WP__", str(int(web_port)))
            .replace("__MODE__", q(mode or ("systemd" if _has_systemd() else "pid")))
            .replace("__BRANCH__", BRANCH))
    script = run / f"update-{int(time.time())}.sh"
    script.write_text(body, encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return script, "bash"


def _has_systemd() -> bool:
    if not shutil.which("systemctl"):
        return False
    for d in (Path("/etc/systemd/system"), Path.home() / ".config/systemd/user"):
        try:
            if any(d.glob("fxproxy*.service")):
                return True
        except OSError:
            pass
    return False


def spawn(script: Path, kind: str, log: Path | None = None) -> int:
    """脱离当前进程启动升级脚本（面板随后会被它杀掉，所以必须 detached）。"""
    log = log or script.parent / "update.log"
    fh = open(log, "ab", buffering=0)
    try:
        if kind == "powershell":
            flags = 0
            if hasattr(subprocess, "DETACHED_PROCESS"):
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-File", str(script)],
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                close_fds=True, creationflags=flags,
            )
        else:
            p = subprocess.Popen(
                ["bash", str(script)],
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                close_fds=True, start_new_session=True,
            )
    finally:
        fh.close()
    return p.pid


# ------------------------------------------------------------------ 状态读取
def read_status(root: Path) -> dict:
    run = root / ".run" if root else Path(".run")
    st = run / "update.status"
    out: dict = {"state": "", "from": "", "to": "", "ts": 0, "message": ""}
    if st.is_file():
        try:
            for line in st.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k in out:
                    out[k] = int(v) if k == "ts" and v.strip().isdigit() else v.strip()
        except OSError:
            pass
    log = run / "update.log"
    lines: list[str] = []
    if log.is_file():
        try:
            raw = log.read_bytes()
            if len(raw) > 64_000:
                raw = raw[-64_000:]
            lines = raw.decode("utf-8", "replace").splitlines()[-40:]
        except OSError:
            pass
    out["log"] = lines
    return out


async def run_update(dest: Path, cfg: Path, tag: str, asset: dict, *,
                     proxy: str | None = None, gh_proxy: str | None = None,
                     web_host: str = "127.0.0.1", web_port: int = 8765,
                     mode: str = "") -> dict:
    """下载 + 校验 + 生成脚本 + 拉起。返回给面板展示的信息。"""
    run = run_dir(dest)
    # 用制品原名当文件名：同一目录可能先后放过 win/linux 两种包，
    # 只留扩展名容易互相覆盖，出问题时也不好认。
    pkg = run / Path(asset["name"]).name
    if pkg.exists():
        pkg.unlink()
    url = asset_url(asset["url"], gh_proxy)
    await download(url, pkg, proxy=proxy)
    problems = verify_archive(pkg)
    if problems:
        pkg.unlink(missing_ok=True)
        raise RuntimeError("制品校验失败：" + "；".join(problems))
    script, kind = build_script(
        "git" if (dest / ".git").exists() else "release",
        dest, pkg, run, version_of_tag(tag), VERSION, cfg,
        web_host, web_port, mode=mode,
    )
    (run / "update.status").unlink(missing_ok=True)
    pid = spawn(script, kind, run / "update.log")
    return {
        "ok": True,
        "from": VERSION,
        "to": version_of_tag(tag),
        "asset": asset["name"],
        "bytes": pkg.stat().st_size if pkg.exists() else 0,
        "script": str(script),
        "runner": kind,
        "pid": pid,
        "log": str(run / "update.log"),
    }

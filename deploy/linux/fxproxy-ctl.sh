#!/usr/bin/env bash
# fxproxy 服务管理（install.sh 安装到 /usr/local/bin 或 ~/.local/bin）。
#
#   fxproxy-ctl start|stop|restart|status|logs|update|uninstall
#   fxproxy-ctl url            # 打印面板与订阅地址
#
# 路径与模式全部来自 ctl.env（安装时生成），脚本本身不含任何硬编码路径，
# 所以同一份文件既能当系统服务管理，也能当用户级 / nohup 部署管理。
set -euo pipefail

ENV_FILE="${FXPROXY_CTL_ENV:-/etc/fxproxy/ctl.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[x] 找不到 $ENV_FILE，先跑 deploy/linux/install.sh" >&2
  exit 2
fi
# shellcheck source=/dev/null
. "$ENV_FILE"

: "${FXPROXY_DIR:?ctl.env 缺 FXPROXY_DIR}"
: "${FXPROXY_CONFIG:?ctl.env 缺 FXPROXY_CONFIG}"
: "${FXPROXY_BIN:?ctl.env 缺 FXPROXY_BIN}"

RUN_DIR="$FXPROXY_DIR/.run"
WEB_PORT="${FXPROXY_WEB_PORT:-8765}"
SYSTEMD="${FXPROXY_SYSTEMD:-0}"
USER_MODE="${FXPROXY_USER_MODE:-0}"
# 空字符串 = 没装 systemd 单元，用 nohup 路径。用 `-` 而非 `:-`，否则空值会被
# 默认值顶掉，导致 ctl 去操作根本不存在的 unit。
UNITS="${FXPROXY_UNITS-fxproxy fxproxy-web}"

log()  { printf '[fxproxy] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[x] %s\n' "$*" >&2; exit 1; }

# systemd 只有在真被 PID 1 用着的时候才算数（容器、WSL 里常见装了但没跑）
have_systemd() {
  [[ "$SYSTEMD" == "1" ]] || return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  [[ -d /run/systemd/system ]] || return 1
  return 0
}

sysctl_() {
  if [[ "$USER_MODE" == "1" ]]; then
    systemctl --user "$@"
  else
    systemctl "$@"
  fi
}

py() { "$FXPROXY_BIN_DIR/python" "$@"; }

# 端口在监听吗（用 venv 里的 python，Alpine 上不一定有 ss/netstat）
port_open() {
  py - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

pid_alive() {
  local f="$1" p
  [[ -r "$f" ]] || return 1
  p="$(cat "$f" 2>/dev/null || true)"
  [[ -n "$p" ]] || return 1
  kill -0 "$p" 2>/dev/null
}

# nohup 模式：单进程起停，pid 落 .run/
start_nohup() {
  mkdir -p "$RUN_DIR"
  for pair in "fxproxy:$FXPROXY_HTTP_PORT" "fxproxy-web:$WEB_PORT"; do
    name="${pair%%:*}"; port="${pair##*:}"
    pidf="$RUN_DIR/$name.pid"
    if pid_alive "$pidf"; then
      log "$name 已在运行 (pid $(cat "$pidf"))"
      continue
    fi
    if port_open "$port"; then
      warn "端口 $port 被别的程序占着，$name 跳过；改 config.json 或面板里的端口"
      continue
    fi
    if [[ "$name" == "fxproxy" ]]; then
      nohup "$FXPROXY_BIN" -c "$FXPROXY_CONFIG" run \
        >>"$RUN_DIR/proxy.log" 2>&1 &
    else
      nohup "$FXPROXY_BIN" -c "$FXPROXY_CONFIG" web --no-open \
        --web-host "${FXPROXY_WEB_HOST:-127.0.0.1}" --web-port "$WEB_PORT" \
        >>"$RUN_DIR/web.log" 2>&1 &
    fi
    echo $! >"$pidf"
    log "$name 已启动 (pid $!)"
  done
}

stop_nohup() {
  for name in fxproxy fxproxy-web; do
    pidf="$RUN_DIR/$name.pid"
    if pid_alive "$pidf"; then
      p="$(cat "$pidf")"
      kill "$p" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$p" 2>/dev/null || break
        sleep 0.3
      done
      kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null || true
      log "$name 已停止 (pid $p)"
    fi
    rm -f "$pidf"
  done
}

cmd_start() {
  if have_systemd; then
    # shellcheck disable=SC2086
    sysctl_ start $UNITS
    log "已通过 systemd 启动：$UNITS"
  else
    start_nohup
  fi
  sleep 1
  cmd_status
}

cmd_stop() {
  if have_systemd; then
    # shellcheck disable=SC2086
    sysctl_ stop $UNITS || true
    log "已通过 systemd 停止"
  else
    stop_nohup
  fi
}

cmd_restart() { cmd_stop; sleep 0.5; cmd_start; }

cmd_status() {
  echo "── fxproxy ──────────────────────────────"
  "$FXPROXY_BIN" --version 2>/dev/null | sed 's/^/  版本   : /' || true
  echo "  安装   : $FXPROXY_DIR"
  echo "  配置   : $FXPROXY_CONFIG"
  echo "  面板   : http://$( [[ "${FXPROXY_WEB_HOST:-127.0.0.1}" == "0.0.0.0" ]] && echo "127.0.0.1" || echo "${FXPROXY_WEB_HOST:-127.0.0.1}" ):$WEB_PORT"
  if have_systemd; then
    for u in $UNITS; do
      printf '  %-11s: %s\n' "$u" "$(sysctl_ is-active "$u" 2>/dev/null || echo unknown)"
    done
  else
    for name in fxproxy fxproxy-web; do
      if pid_alive "$RUN_DIR/$name.pid"; then
        printf '  %-11s: 运行中 (pid %s)\n' "$name" "$(cat "$RUN_DIR/$name.pid")"
      else
        printf '  %-11s: 未运行\n' "$name"
      fi
    done
  fi
  if port_open "$FXPROXY_HTTP_PORT"; then
    echo "  HTTP     : 127.0.0.1:$FXPROXY_HTTP_PORT 可用"
  else
    echo "  HTTP     : 127.0.0.1:$FXPROXY_HTTP_PORT 未监听"
  fi
  if port_open "$FXPROXY_SOCKS_PORT"; then
    echo "  SOCKS5   : 127.0.0.1:$FXPROXY_SOCKS_PORT 可用"
  else
    echo "  SOCKS5   : 127.0.0.1:$FXPROXY_SOCKS_PORT 未监听"
  fi
  if port_open "$WEB_PORT"; then
    py - "$WEB_PORT" <<'PY' || true
import json, sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=4) as r:
        s = json.load(r)
except Exception as e:
    print(f"  状态     : 面板无响应（{e}）")
    raise SystemExit(0)
gb = 1024 ** 3
print(f"  账号     : {s.get('account_count', 0)} 个")
print(f"  代理     : {'运行中' if s.get('running') else '未运行'}")
print(f"  策略     : {s.get('strategy', '-')}")
print(f"  出口     : {s.get('egress_country') or s.get('country') or '任意'}")
PY
  fi
  echo "  订阅     : http://<本机地址>:$WEB_PORT/clash.yaml"
  echo "─────────────────────────────────────────"
}

cmd_logs() {
  local n="${1:-100}"
  if have_systemd; then
    # shellcheck disable=SC2086
    if [[ "$USER_MODE" == "1" ]]; then
      journalctl --user -u fxproxy -u fxproxy-web -n "$n" --no-pager
    else
      journalctl -u fxproxy -u fxproxy-web -n "$n" --no-pager
    fi
  else
    for f in "$RUN_DIR/proxy.log" "$RUN_DIR/web.log"; do
      [[ -f "$f" ]] || continue
      echo "── $f ──"
      tail -n "$n" "$f"
    done
  fi
}

# ---------------------------------------------------------------- 自更新
# 发行包部署（非 git）以前只会提示「重跑 install.sh」，等于没有更新路径。
# 这里补上：取最新 Release 的 tar.gz -> 备份代码 -> 覆盖 -> 重装依赖 -> 重启。
# config.json / .venv / .run 一律不动（tar 只覆盖包里存在的文件）。
CTL_REPO="${FXPROXY_REPO:-qq928820655/fxproxy-multi}"
CTL_PROXY="${FXPROXY_GH_PROXY:-${FXPROXY_UPDATE_PROXY:-}}"
CTL_API="${FXPROXY_UPDATE_API:-https://api.github.com/repos/$CTL_REPO/releases/latest}"

fetch() {
  # $1=url $2=输出文件；给了镜像前缀就拼上（ghproxy.net/https://github.com/...）
  local u="$1"
  [[ -n "$CTL_PROXY" ]] && u="${CTL_PROXY%/}/$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 2 --connect-timeout 15 --max-time 600 -o "$2" "$u"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 30 -O "$2" "$u"
  else
    warn "没有 curl 也没有 wget"
    return 127
  fi
}

release_asset() {
  # 输出 "tag<TAB>tar url"；没有 jq，按逗号切开抠字段
  local jf="$1"
  fetch "$CTL_API" "$jf" || return 1
  [[ -s "$jf" ]] || return 1
  local tag url
  tag="$(tr ',' '\n' <"$jf" | grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' \
         | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
  url="$(tr ',' '\n' <"$jf" | grep -o 'https://[^"]*-linux\.tar\.gz' | head -1)"
  [[ -n "$url" ]] || return 1
  printf '%s\t%s\n' "${tag:-v0}" "$url"
}

cmd_update() {
  local check=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check)   check=1; shift ;;
      --proxy)   CTL_PROXY="${2:-}"; shift 2 ;;
      --repo)    CTL_REPO="${2:-}"; CTL_API="https://api.github.com/repos/$CTL_REPO/releases/latest"; shift 2 ;;
      -y|--yes)  shift ;;
      *) die "update: 未知参数 $1" ;;
    esac
  done

  local src="${FXPROXY_SRC:-$FXPROXY_DIR}"
  local cur=""
  cur="$("$FXPROXY_BIN" --version 2>/dev/null | tr -dc '0-9.' || true)"

  if [[ -d "$src/.git" ]]; then
    log "git 部署，pull 更新 $src ..."
    git -C "$src" fetch --quiet origin 2>/dev/null || die "git fetch 失败"
    # 解析要比较/快进的远端分支：优先本分支 upstream；没设 upstream 时退回
    # origin/HEAD（默认分支），再退回 origin/main。老逻辑只认 @{u}，取不到时
    # rev-list 失败被当成 0，会误报「已是最新」，pull 也会直接报错。
    local uref=""
    uref="$(git -C "$src" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [[ -z "$uref" || "$uref" == "HEAD" ]]; then
      if git -C "$src" rev-parse --verify --quiet 'origin/main' >/dev/null 2>&1; then
        uref="origin/main"
      else
        uref="$(git -C "$src" rev-parse --abbrev-ref 'origin/HEAD' 2>/dev/null || true)"
      fi
    fi
    if [[ -z "$uref" || "$uref" == "origin/HEAD" ]]; then
      die "无法确定远端分支（没有 upstream，也找不到 origin/main）"
    fi
    if [[ "$check" == "1" ]]; then
      local behind
      behind="$(git -C "$src" rev-list --count "HEAD..$uref" 2>/dev/null || echo '?')"
      if [[ "$behind" == "0" ]]; then
        log "已是最新（${cur:-未知}，对比 $uref）"
      elif [[ "$behind" == "?" ]]; then
        warn "无法比较 HEAD 与 $uref"
      else
        log "落后 $behind 个提交，可更新（$uref）"
      fi
      return 0
    fi
    # 已经 fetch 过，直接按解析出的远端分支快进；无 upstream 也能用
    git -C "$src" merge --ff-only "$uref" \
      || die "快进到 $uref 失败（本地有分叉提交，请手动处理）"
  else
    local tmp info tag url pkg
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/fxproxy-up.XXXXXX")"
    log "检查最新发行版 ..."
    if ! info="$(release_asset "$tmp/rel.json")"; then
      rm -rf "$tmp"
      die "取不到 GitHub Release（网络不通或被限流）。可 export FXPROXY_GH_PROXY=https://ghproxy.net/ 再试"
    fi
    tag="${info%%$'\t'*}"
    url="${info##*$'\t'}"
    log "最新 ${tag}，当前 ${cur:-未知}"
    if [[ "$check" == "1" ]]; then
      if [[ -n "$cur" && "${tag#v}" == "$cur" ]]; then log "已是最新"; else log "可更新到 ${tag#v}"; fi
      rm -rf "$tmp"
      return 0
    fi
    if [[ -n "$cur" && "${tag#v}" == "$cur" ]]; then
      log "已是最新，不重复下载"; rm -rf "$tmp"; return 0
    fi

    pkg="$tmp/pkg.tar.gz"
    log "下载 $(basename "$url") ..."
    if ! fetch "$url" "$pkg" || [[ ! -s "$pkg" ]]; then rm -rf "$tmp"; die "下载失败"; fi
    if ! tar tzf "$pkg" >/dev/null 2>&1; then rm -rf "$tmp"; die "包不是合法 tar.gz（可能下到了错误页）"; fi
    if ! tar tzf "$pkg" | grep -q '/deploy/linux/install.sh$'; then
      rm -rf "$tmp"; die "制品结构不对：缺 deploy/linux/install.sh"
    fi

    mkdir -p "$RUN_DIR"
    local bak="$RUN_DIR/backup-${cur:-old}-$(date +%Y%m%d%H%M%S).tar.gz"
    tar czf "$bak" -C "$FXPROXY_DIR" --exclude=.venv --exclude=.run --exclude=config.json . \
      2>/dev/null || warn "备份失败（继续更新）"
    [[ -f "$bak" ]] && log "备份 $bak"

    # --strip-components=1：包内顶层目录现在固定叫 fxproxy，直接盖到安装目录
    tar xzf "$pkg" -C "$FXPROXY_DIR" --strip-components=1 || { rm -rf "$tmp"; die "覆盖失败"; }
    log "代码已覆盖到 $tag"
    rm -rf "$tmp"
  fi

  log "重装依赖 ..."
  "$FXPROXY_BIN_DIR/pip" install -q -e "$src" 2>/dev/null \
    || "$FXPROXY_BIN_DIR/pip" install -q -e "$src" -i https://pypi.tuna.tsinghua.edu.cn/simple \
    || die "依赖安装失败，看上面的 pip 输出"
  cmd_restart
  log "完成：$("$FXPROXY_BIN" --version 2>/dev/null || echo 未知)"
}

cmd_uninstall() {
  local purge="${1:-}"
  cmd_stop || true
  if have_systemd; then
    # shellcheck disable=SC2086
    sysctl_ disable $UNITS 2>/dev/null || true
    if [[ "$USER_MODE" == "1" ]]; then
      rm -f "${HOME}/.config/systemd/user/fxproxy.service" \
            "${HOME}/.config/systemd/user/fxproxy-web.service"
      systemctl --user daemon-reload 2>/dev/null || true
    else
      rm -f /etc/systemd/system/fxproxy.service /etc/systemd/system/fxproxy-web.service
      systemctl daemon-reload 2>/dev/null || true
    fi
  fi
  # 只删安装时记录的路径：用 command -v 猜会误删别人装的同名命令
  if [[ -n "${FXPROXY_CTL_BIN:-}" ]]; then
    rm -f "$FXPROXY_CTL_BIN" 2>/dev/null || true
  fi
  if [[ "$purge" == "--purge" ]]; then
    log "删除安装目录 $FXPROXY_DIR（含 config.json 里的凭证）"
    rm -rf "$FXPROXY_DIR"
  else
    log "保留 $FXPROXY_DIR（含 config.json）；要连凭证一起删：fxproxy-ctl uninstall --purge"
  fi
  log "已卸载"
}

cmd_url() {
  local host
  host="$( [[ "${FXPROXY_WEB_HOST:-127.0.0.1}" == "0.0.0.0" ]] \
    && hostname -I 2>/dev/null | awk '{print $1}' || echo "${FXPROXY_WEB_HOST:-127.0.0.1}" )"
  echo "面板    : http://$host:$WEB_PORT"
  echo "订阅    : http://$host:$WEB_PORT/clash.yaml"
  echo "provider: http://$host:$WEB_PORT/clash.yaml?provider=1"
  echo "HTTP    : $host:$FXPROXY_HTTP_PORT"
  echo "SOCKS5  : $host:$FXPROXY_SOCKS_PORT"
}

usage() {
  cat <<EOF
用法: fxproxy-ctl <命令>

  start        启动代理 + 面板
  stop         停止
  restart      重启
  status       进程 / 端口 / 账号 / 出口
  logs [N]     最近 N 行日志（默认 100）
  update       git pull + 重装依赖 + 重启
  uninstall [--purge]   卸载（--purge 连安装目录和凭证一起删）
  url          打印面板与订阅地址

配置文件: $ENV_FILE
EOF
}

case "${1:-}" in
  start)      cmd_start ;;
  stop)       cmd_stop ;;
  restart)    cmd_restart ;;
  status)     cmd_status ;;
  logs)       cmd_logs "${2:-100}" ;;
  update)     cmd_update ;;
  uninstall)  cmd_uninstall "${2:-}" ;;
  url)        cmd_url ;;
  ""|-h|--help|help) usage ;;
  *) echo "未知命令: $1" >&2; usage >&2; exit 2 ;;
esac

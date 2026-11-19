#!/usr/bin/env bash
# fxproxy 菜单式一键部署（Linux / macOS 服务器）。
#
#   curl -fsSL https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/linux/bootstrap.sh | bash
#   curl -fsSL .../bootstrap.sh -o b.sh && bash b.sh          # 想先看代码就这样
#   bash bootstrap.sh --yes --token TOK --country US          # 全参数，不弹菜单
#   bash bootstrap.sh --uninstall                             # 卸载
#
# 制品自动挑：先取 GitHub 最新 Release 的 linux tar.gz，取不到退回分支源码包，
# 再不行才 git clone。下载慢可先 export FXPROXY_GH_PROXY=https://ghproxy.net/
set -uo pipefail

REPO="${FXPROXY_REPO:-qq928820655/fxproxy-multi}"
BRANCH="${FXPROXY_BRANCH:-main}"
GH_PROXY="${FXPROXY_GH_PROXY:-}"
API_URL="${FXPROXY_UPDATE_API:-https://api.github.com/repos/$REPO/releases/latest}"

# ---------------------------------------------------------------- 管道执行自愈
# `curl | bash` 时脚本从 stdin 读入：这时 BASH_SOURCE 为空，而且 bash 正占着
# fd0 读脚本体，脚本里任何 read 都会读到脚本尾部然后 EOF。所以先把脚本体落
# 盘，再以文件形式带 tty 重跑一遍。
# 注意别用 `-t 0` 当判据：cron / CI / `bash x.sh --help` 的 stdin 也不是 tty。
if [[ -z "${BASH_SOURCE[0]:-}" ]]; then
  _self="$(mktemp "${TMPDIR:-/tmp}/fxproxy-boot.XXXXXX.sh")"
  cat >"$_self"
  # 探测能否真的打开 /dev/tty：Git Bash 之类 /dev/tty 存在但打不开
  if (exec 3</dev/tty) 2>/dev/null; then
    bash "$_self" "$@" </dev/tty
  else
    FXPROXY_NO_TTY=1 bash "$_self" "$@" </dev/null
  fi
  _rc=$?
  unlink "$_self" 2>/dev/null || rm -f "$_self" 2>/dev/null || true
  exit $_rc
fi

# ---------------------------------------------------------------- 输出
if [[ -t 1 ]]; then
  C0=$'\033[0m'; CB=$'\033[1m'; CC=$'\033[36m'; CG=$'\033[32m'
  CY=$'\033[33m'; CR=$'\033[31m'; CD=$'\033[90m'
else
  C0=""; CB=""; CC=""; CG=""; CY=""; CR=""; CD=""
fi
say()  { printf '%s\n' "${CC}[fxproxy]${C0} $*"; }
ok()   { printf '  %sok%s %s\n' "$CG" "$C0" "$*"; }
warn() { printf '  %s!%s %s\n' "$CY" "$C0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$CR" "$C0" "$*" >&2; exit 1; }
line() { printf '%s\n' "${CD}────────────────────────────────────────────────────────${C0}"; }

banner() {
  printf '\n%s\n' "${CB}${CC}  fxproxy 一键部署${C0}"
  printf '%s\n' "${CD}  Firefox IP Protection → 本地 HTTP/SOCKS5 代理池${C0}"
  line
  printf '  系统   %s\n' "${PRETTY:-$(uname -s)}"
  printf '  架构   %s\n' "$ARCH"
  printf '  身份   %s\n' "$([ "$IS_ROOT" -eq 1 ] && echo root || echo "普通用户 $USER")"
  printf '  服务   %s\n' "$([ "$HAS_SYSTEMD" -eq 1 ] && echo systemd || echo "无 systemd（用 nohup 后台）")"
  printf '  Python %s\n' "${PY_VER:-未检测到 3.10+}"
  [[ -n "${INSTALLED_VER:-}" ]] && printf '  已装   %s%s%s\n' "$CG" "$INSTALLED_VER" "$C0"
  line
}

# ---------------------------------------------------------------- 环境探测
PRETTY=""
ARCH="$(uname -m 2>/dev/null || echo unknown)"
IS_ROOT=0
[[ ${EUID:-$(id -u)} -eq 0 ]] && IS_ROOT=1
HAS_SYSTEMD=0
[[ -d /run/systemd/system ]] && HAS_SYSTEMD=1
PY_VER=""
INSTALLED_VER=""

detect_env() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    PRETTY="$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-$NAME}")"
  elif [[ "$(uname -s)" == "Darwin" ]]; then
    PRETTY="macOS $(sw_vers -productVersion 2>/dev/null || echo)"
  fi
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
      PY_VER="$("$c" -V 2>&1)"
      PY="$c"
      break
    fi
  done
  local envf
  for envf in /etc/fxproxy/ctl.env "${XDG_CONFIG_HOME:-$HOME/.config}/fxproxy/ctl.env"; do
    if [[ -r "$envf" ]]; then
      # shellcheck disable=SC1090
      INSTALLED_VER="$(. "$envf" 2>/dev/null; [ -x "${FXPROXY_BIN:-}" ] && "$FXPROXY_BIN" --version 2>/dev/null || true)"
      [[ -n "$INSTALLED_VER" ]] && break
    fi
  done
}

# ---------------------------------------------------------------- 下载制品
DL=""
pick_downloader() {
  if command -v curl >/dev/null 2>&1; then DL="curl"
  elif command -v wget >/dev/null 2>&1; then DL="wget"
  else DL=""; fi
}

fetch_url() {
  # $1=url $2=输出文件
  case "$DL" in
    curl) curl -fsSL --retry 2 --connect-timeout 15 --max-time 600 -o "$2" "$1" ;;
    wget) wget -q -T 30 -O "$2" "$1" ;;
    *) return 1 ;;
  esac
}

proxied() {
  [[ -z "$GH_PROXY" ]] && { printf '%s' "$1"; return; }
  printf '%s/%s' "${GH_PROXY%/}" "$1"
}

release_tar_url() {
  # 从 GitHub API 里挑 linux tar.gz 的下载地址；没有 jq，用 sed 抠
  local json="$1"
  printf '%s' "$json" \
    | tr ',' '\n' \
    | grep -o 'https://[^"]*-linux\.tar\.gz' \
    | head -1
}

WORK=""
cleanup_work() { [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"; }
trap cleanup_work EXIT

prepare_src() {
  mkdir -p "$WORK"
  local tar="$WORK/src.tar.gz" url=""

  if [[ -n "$DL" ]]; then
    say "查最新发行版 ..."
    local jf="$WORK/rel.json"
    if fetch_url "$(proxied "$API_URL")" "$jf" 2>/dev/null && [[ -s "$jf" ]]; then
      url="$(release_tar_url "$(cat "$jf")")"
      [[ -n "$url" ]] && ok "发现制品 $(basename "$url")"
    fi
    if [[ -z "$url" ]]; then
      warn "没取到 Release 资产，改用 $BRANCH 分支源码包"
      url="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    fi
    say "下载 $(basename "$url") ..."
    if fetch_url "$(proxied "$url")" "$tar" && [[ -s "$tar" ]]; then
      mkdir -p "$WORK/x"
      if tar xzf "$tar" -C "$WORK/x" 2>/dev/null; then
        local top
        top="$(find "$WORK/x" -maxdepth 1 -mindepth 1 -type d | head -1)"
        if [[ -n "$top" && -f "$top/deploy/linux/install.sh" ]]; then
          ok "已解压到 $(basename "$top")"
          SRC="$top"
          return 0
        fi
      fi
      warn "源码包结构不对，改用 git"
    else
      warn "下载失败"
    fi
  fi

  if command -v git >/dev/null 2>&1; then
    say "git clone 源码 ..."
    rm -rf "$WORK/src"
    if git clone --depth 1 -b "$BRANCH" "$(proxied "https://github.com/$REPO.git")" \
         "$WORK/src" 2>/dev/null; then
      # 镜像前缀只对 http 下载有效，git 走镜像多半失败，再直连试一次
      :
    elif git clone --depth 1 -b "$BRANCH" "https://github.com/$REPO.git" "$WORK/src"; then
      :
    else
      die "git clone 也失败，检查网络或设置 FXPROXY_GH_PROXY"
    fi
    SRC="$WORK/src"
    ok "已 clone"
    return 0
  fi
  die "既下载不了制品，也没有 git。手动装 git 后重试"
}

# ---------------------------------------------------------------- 问答
ask() {
  # $1=提示 $2=默认值 → 回显到 ASK_RESULT
  local hint="$1" def="$2" ans=""
  if [[ -n "$def" ]]; then
    read -r -p "  $hint ${CD}[$def]${C0}: " ans || ans=""
    ASK_RESULT="${ans:-$def}"
  else
    read -r -p "  $hint: " ans || ans=""
    ASK_RESULT="$ans"
  fi
}

ask_yn() {
  # $1=提示 $2=默认 y|n → ASK_RESULT=1|0
  local hint="$1" def="${2:-y}" ans=""
  local tip="[Y/n]"; [[ "$def" == "n" ]] && tip="[y/N]"
  read -r -p "  $hint $tip: " ans || ans=""
  ans="${ans:-$def}"
  [[ "$ans" =~ ^[Yy] ]] && ASK_RESULT=1 || ASK_RESULT=0
}

port_hint() {
  # 给个"被占就顺延"的提示，不阻断
  local p="$1" name="$2"
  [[ -z "${PY:-}" ]] && { printf '%s' "$p"; return; }
  if "$PY" - "$p" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
PY
  then printf '%s' "$p"
  else
    warn "$name 端口 $p 已被占用，安装时会自动顺延"
    printf '%s' "$p"
  fi
}

ARGS=()
add_arg() { ARGS+=("$@"); }

interactive_config() {
  local dir country token hport sport wport host sub idx autostart dostart

  line
  say "逐项配置（直接回车用默认值）"
  ls /dev/tty >/dev/null 2>&1 || true

  local defdir="/opt/fxproxy"
  [[ "$IS_ROOT" -eq 0 ]] && defdir="$HOME/.local/fxproxy"
  ask "安装目录" "$defdir"; dir="$ASK_RESULT"

  ask "refresh_token（多个用逗号分隔，留空则装完在面板里加）" ""; token="$ASK_RESULT"

  printf '  常用国家：US 美国 · GB 英国 · DE 德国 · JP 日本 · SG 新加坡 · 留空=任意\n'
  ask "出口国家 ISO 码" ""; country="$ASK_RESULT"

  ask "HTTP 代理端口" "8080"; hport="$(port_hint "$ASK_RESULT" HTTP)"
  ask "SOCKS5 端口" "1080"; sport="$(port_hint "$ASK_RESULT" SOCKS5)"
  ask "面板/订阅端口" "8765"; wport="$(port_hint "$ASK_RESULT" 面板)"

  ask "代理监听地址（127.0.0.1 本机 / 0.0.0.0 对外开放）" "127.0.0.1"; host="$ASK_RESULT"
  ask "订阅里给客户端的公网地址（域名或 IP，留空自动推导）" ""; sub="$ASK_RESULT"
  ask "pip 镜像（留空用官方，失败自动回退清华）" ""; idx="$ASK_RESULT"

  if [[ "$HAS_SYSTEMD" -eq 1 ]]; then
    ask_yn "注册 systemd 并开机自启" "y"; autostart="$ASK_RESULT"
  else
    autostart=0
  fi
  ask_yn "装完立即启动" "y"; dostart="$ASK_RESULT"

  add_arg --dir "$dir"
  [[ -n "$token" ]]   && add_arg --token "$token"
  [[ -n "$country" ]] && add_arg --country "$country"
  add_arg --http-port "$hport" --socks-port "$sport" --web-port "$wport"
  add_arg --host "$host"
  [[ -n "$sub" ]] && add_arg --sub-server "$sub"
  [[ -n "$idx" ]] && add_arg --index-url "$idx"
  [[ "$autostart" -eq 0 ]] && add_arg --no-service
  [[ "$dostart" -eq 0 ]] && add_arg --no-start
  if [[ "$autostart" -eq 0 && "$HAS_SYSTEMD" -eq 1 ]]; then
    warn "不注册 systemd 时用 nohup 后台，重启机器需要手动 fxproxy-ctl start"
  fi
}

# ---------------------------------------------------------------- 动作
run_install() {
  prepare_src
  local inst="$SRC/deploy/linux/install.sh"
  [[ -f "$inst" ]] || die "源码里没有 deploy/linux/install.sh"
  local cmd=(bash "$inst")
  [[ ${#ARGS[@]} -gt 0 ]] && cmd+=("${ARGS[@]}")
  line
  say "执行安装：${cmd[*]}"
  line
  "${cmd[@]}"
  local rc=$?
  line
  if [[ $rc -eq 0 ]]; then
    ok "安装完成"
    if [[ -x "$(command -v fxproxy-ctl 2>/dev/null || echo /nonexistent)" ]]; then
      printf '\n  管理命令：%sfxproxy-ctl status|start|stop|restart|logs|update|uninstall%s\n\n' "$CB" "$C0"
    elif [[ -x "${HOME}/.local/bin/fxproxy-ctl" ]]; then
      printf '\n  管理命令：%s%s/.local/bin/fxproxy-ctl status%s\n\n' "$CB" "$HOME" "$C0"
    fi
  else
    warn "安装返回 $rc，往上翻日志"
  fi
  return $rc
}

run_download_only() {
  local out="${1:-$PWD}"
  prepare_src
  local name="fxproxy-$(date +%Y%m%d)-src.tar.gz"
  tar czf "$out/$name" -C "$(dirname "$SRC")" "$(basename "$SRC")" 2>/dev/null ||
    die "打包失败"
  ok "已保存 $out/$name"
  printf '  在那台机器上：tar xzf %s && bash %s/deploy/linux/install.sh\n' \
    "$name" "${name%.tar.gz}"
}

run_upgrade() {
  if command -v fxproxy-ctl >/dev/null 2>&1; then
    say "升级已安装实例 ..."
    fxproxy-ctl update
    return
  fi
  for p in /usr/local/bin/fxproxy-ctl "${HOME}/.local/bin/fxproxy-ctl"; do
    if [[ -x "$p" ]]; then
      say "升级已安装实例 ..."
      "$p" update
      return
    fi
  done
  warn "没找到 fxproxy-ctl，看起来还没装过"
  ask_yn "现在装一份？" "y"
  [[ $ASK_RESULT -eq 1 ]] && { interactive_config; run_install; }
}

run_status() {
  for p in /usr/local/bin/fxproxy-ctl "${HOME}/.local/bin/fxproxy-ctl"; do
    if [[ -x "$p" ]]; then "$p" status; return; fi
  done
  warn "没找到 fxproxy-ctl"
}

run_uninstall() {
  local purge="$1" inst=""
  for p in /usr/local/bin/fxproxy-ctl "${HOME}/.local/bin/fxproxy-ctl"; do
    [[ -x "$p" ]] && inst="$p"
  done
  if [[ -n "$inst" ]]; then
    if [[ "$purge" -eq 1 ]]; then
      say "卸载（含 config 与凭证）..."
      "$inst" uninstall --purge
    else
      say "卸载（保留 config.json）..."
      "$inst" uninstall
    fi
    return
  fi
  warn "没找到 fxproxy-ctl，可能没装过"
}

# ---------------------------------------------------------------- 菜单
menu() {
  while true; do
    banner
    cat <<EOF
  ${CB}1${C0}) 交互式安装        逐项配置端口/凭证/国家
  ${CB}2${C0}) 快速安装          默认端口，凭证稍后在面板加
  ${CB}3${C0}) 只下载制品        存到当前目录，拿去别的机器装
  ${CB}4${C0}) 升级已安装        拉最新版覆盖 + 重启
  ${CB}5${C0}) 查看状态          账号数、端口、出口
  ${CB}6${C0}) 卸载
  ${CB}0${C0}) 退出
EOF
    line
    local c=""
    read -r -p "  选择: " c || exit 0
    case "$c" in
      1) interactive_config; run_install; read -r -p "  回车回菜单" _ || true ;;
      2) add_arg --no-start; line; say "快速安装（仅默认端口，不启动）"; run_install
         read -r -p "  回车回菜单" _ || true ;;
      3) run_download_only "$PWD"; read -r -p "  回车回菜单" _ || true ;;
      4) run_upgrade; read -r -p "  回车回菜单" _ || true ;;
      5) run_status; read -r -p "  回车回菜单" _ || true ;;
      6) local pg=0
         ask_yn "连 config.json 和凭证一起删？" "n"; pg=$ASK_RESULT
         run_uninstall "$pg"; read -r -p "  回车回菜单" _ || true ;;
      0|q|Q) exit 0 ;;
      "") ;;
      *) warn "没有这个选项：$c" ;;
    esac
  done
}

usage() {
  cat <<'EOF'
fxproxy 菜单式一键部署

用法:
  curl -fsSL <raw-url>/deploy/linux/bootstrap.sh | bash
  bash bootstrap.sh [选项]          # 有参数就不进菜单，直接按参数装

无参数时进菜单；给下面任意参数则直接执行（适合脚本化 / 批量）:
  --yes, -y              全默认（凭据后填，装完启动）
  --token T1,T2          refresh_token，逗号分隔
  --country US           出口国家 ISO 码
  --dir PATH             安装目录
  --http-port N          HTTP 端口（默认 8080）
  --socks-port N         SOCKS5 端口（默认 1080）
  --web-port N           面板端口（默认 8765）
  --host ADDR            代理监听地址（默认 127.0.0.1）
  --web-host ADDR        面板监听地址
  --sub-server HOST      订阅里的公网地址
  --index-url URL        pip 镜像
  --proxy URL            给 pip/git 的代理
  --no-service           不注册 systemd
  --no-start             装完不启动
  --uninstall [--purge]  卸载（--purge 连凭证一起删）
  --dry-run              只打印步骤

环境变量:
  FXPROXY_REPO        owner/repo（默认 qq928820655/fxproxy-multi）
  FXPROXY_BRANCH      分支（默认 main）
  FXPROXY_GH_PROXY    GitHub 镜像前缀，如 https://ghproxy.net/
  FXPROXY_UPDATE_API  自定义 release API 地址

示例:
  # 机房批量装，指定国家 + 三个账号
  curl -fsSL <raw-url> | bash -s -- --yes --country US --token A,B,C

  # 内网慢，走镜像
  curl -fsSL <raw-url> | FXPROXY_GH_PROXY=https://ghproxy.net/ bash

  # 对外开放 8080/1080，面板只给内网
  bash bootstrap.sh --host 0.0.0.0 --web-host 127.0.0.1 --country DE
EOF
}

# ---------------------------------------------------------------- 入口
main() {
  detect_env
  pick_downloader
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/fxproxy-src.XXXXXX")"

  # 认识的参数直接透传给 install.sh。要先自己校验一遍：不然打错一个字母
  # 会先进去下载几十 MB，最后才由 install.sh 报「未知参数」。
  local known=" --yes -y --token --tokens --cookie-file --country --dir
    --http-port --socks-port --web-port --host --web-host --sub-server
    --index-url --proxy --with-login-deps --no-service --no-start --user
    --system --dry-run --uninstall --purge --menu -h --help "

  local passthru=()
  local want_menu=1 uninstall=0 purge=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --menu)      want_menu=1; shift ;;
      --uninstall) uninstall=1; passthru+=("$1"); shift ;;
      --purge)     purge=1; passthru+=("$1"); shift ;;
      -h|--help)   usage; exit 0 ;;
      -*)
        if [[ "$known" != *" $1 "* ]]; then
          printf '%s[x]%s 未知参数: %s（--help 看用法）\n' "$CR" "$C0" "$1" >&2
          exit 2
        fi
        passthru+=("$1"); shift; want_menu=0 ;;
      *)           passthru+=("$1"); shift; want_menu=0 ;;
    esac
  done

  if [[ $uninstall -eq 1 ]]; then
    run_uninstall "$purge"
    exit 0
  fi

  if [[ $want_menu -eq 1 ]]; then
    [[ -n "${FXPROXY_NO_TTY:-}" ]] && die "没有可交互的终端，请带参数运行（--help 看参数）"
    menu
    exit 0
  fi

  # 参数直通：把参数转成 install.sh 的调用
  ARGS=("${passthru[@]}")
  if [[ "$DL" == "" ]]; then
    warn "没有 curl/wget，尝试 git"
  fi
  run_install
  exit $?
}

main "$@"

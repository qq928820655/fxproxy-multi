#!/usr/bin/env bash
# fxproxy Linux 一键部署。
#
#   ./install.sh                          # 交互：问国家、凭证、端口
#   ./install.sh --token TOK1,TOK2        # 直接带凭证（机房部署最常用）
#   ./install.sh --cookie-file /tmp/export.json
#   ./install.sh --country US --no-start
#   ./install.sh --uninstall              # 只停服务 + 删 unit，保留 config
#   ./install.sh --dry-run                # 只打印将要做什么，不改系统
#
# 面向：Debian/Ubuntu (apt)、RHEL/Rocky/Alma/Fedora (dnf/yum)、Arch (pacman)、
#       Alpine (apk)，x86_64 与 aarch64 都行。
# root 装成系统服务（systemd，开机自启）；普通用户装到 ~/.local/fxproxy，
# 有 systemd 用 --user，没有就 nohup 后台（容器/WSL 也能用）。
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="${FXPROXY_REPO_URL:-https://github.com/qq928820655/fxproxy-multi.git}"

COUNTRY=""
TOKENS=""
COOKIE_FILE=""
HTTP_PORT=8080
SOCKS_PORT=1080
WEB_PORT=8765
HOST=127.0.0.1
WEB_HOST=127.0.0.1
SUB_SERVER=""
INSTALL_DIR=""
INDEX_URL=""
PROXY=""
WITH_LOGIN_DEPS=0
NO_START=0
NO_SERVICE=0
FORCE_USER=0
FORCE_SYSTEM=0
UNINSTALL=0
PURGE=0
DRY_RUN=0

log()  { printf '\033[36m[fxproxy]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
fxproxy Linux 一键部署

用法: sudo ./install.sh [选项]

安装位置
  --dir PATH           安装目录（root 默认 /opt/fxproxy，普通用户默认 ~/.local/fxproxy）
  --user               强制用户级安装（systemctl --user，无 root 时自动）
  --system             强制系统级安装（需 root）

凭证（任选，都不给就装完在面板里加）
  --token T1,T2        直接给 refresh_token，逗号/换行分隔
  --cookie-file PATH   插件导出的 Cookie/JSON 文件

网络
  --country CODE       出口国家 ISO 码，如 US / GB / DE；留空 = 任意
  --http-port N        HTTP 代理端口（默认 8080）
  --socks-port N       SOCKS5 端口（默认 1080）
  --web-port N         面板/订阅端口（默认 8765）
  --host ADDR          代理监听地址（默认 127.0.0.1，对外开放用 0.0.0.0）
  --web-host ADDR      面板监听地址（默认 127.0.0.1）
  --sub-server HOST    订阅里给客户端用的地址（公网 IP/域名），留空自动推导

依赖
  --index-url URL      pip 镜像（默认官方源，失败自动回退清华）
  --proxy URL          给 pip/git 用的代理，如 http://127.0.0.1:7890
  --with-login-deps    额外装 pyfxa/curl_cffi（只有要在本机跑 login 才需要）

其它
  --no-service         不注册 systemd，只用 nohup 后台
  --no-start           装完不启动
  --uninstall          停止、删 unit、卸 ctl（保留 config 与凭证）
  --purge              配合 --uninstall：连安装目录和凭证一起删
  --dry-run            只打印步骤，不改系统
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)              INSTALL_DIR="${2:?}"; shift 2 ;;
    --token|--tokens)   TOKENS="${2:?}"; shift 2 ;;
    --cookie-file)      COOKIE_FILE="${2:?}"; shift 2 ;;
    --country)          COUNTRY="${2:?}"; shift 2 ;;
    --http-port)        HTTP_PORT="${2:?}"; shift 2 ;;
    --socks-port)       SOCKS_PORT="${2:?}"; shift 2 ;;
    --web-port)         WEB_PORT="${2:?}"; shift 2 ;;
    --host)             HOST="${2:?}"; shift 2 ;;
    --web-host)         WEB_HOST="${2:?}"; shift 2 ;;
    --sub-server)       SUB_SERVER="${2:?}"; shift 2 ;;
    --index-url)        INDEX_URL="${2:?}"; shift 2 ;;
    --proxy)            PROXY="${2:?}"; shift 2 ;;
    --with-login-deps)  WITH_LOGIN_DEPS=1; shift ;;
    --no-service)       NO_SERVICE=1; shift ;;
    --no-start)         NO_START=1; shift ;;
    --user)             FORCE_USER=1; shift ;;
    --system)           FORCE_SYSTEM=1; shift ;;
    --uninstall)        UNINSTALL=1; shift ;;
    --purge)            PURGE=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    -h|--help)          usage; exit 0 ;;
    *) printf '[x] 未知参数: %s（--help 看用法）\n' "$1" >&2; exit 2 ;;
  esac
done

COUNTRY="${COUNTRY^^}"

# ---------------------------------------------------------------- 运行身份
IS_ROOT=0
[[ ${EUID:-$(id -u)} -eq 0 ]] && IS_ROOT=1

SYSTEM_MODE=0
if [[ $FORCE_SYSTEM -eq 1 ]]; then
  [[ $IS_ROOT -eq 1 ]] || die "--system 需要 root"
  SYSTEM_MODE=1
elif [[ $FORCE_USER -eq 1 ]]; then
  SYSTEM_MODE=0
elif [[ $IS_ROOT -eq 1 ]]; then
  SYSTEM_MODE=1
fi

if [[ -z "$INSTALL_DIR" ]]; then
  if [[ $SYSTEM_MODE -eq 1 ]]; then INSTALL_DIR=/opt/fxproxy
  else INSTALL_DIR="$HOME/.local/fxproxy"; fi
fi
INSTALL_DIR="${INSTALL_DIR%/}"

if [[ $SYSTEM_MODE -eq 1 ]]; then
  CTL_ENV_DIR=/etc/fxproxy
  CTL_ENV="$CTL_ENV_DIR/ctl.env"
  CTL_BIN=/usr/local/bin/fxproxy-ctl
  UNIT_DIR=""
else
  CTL_ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fxproxy"
  CTL_ENV="$CTL_ENV_DIR/ctl.env"
  CTL_BIN="${HOME}/.local/bin/fxproxy-ctl"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "${HOME}/.local/bin" 2>/dev/null || true
fi

CONFIG="$INSTALL_DIR/config.json"
VENV="$INSTALL_DIR/.venv"
RUN_DIR="$INSTALL_DIR/.run"

# ---------------------------------------------------------------- 执行包装
run_cmd() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  [dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

run_sh() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  [dry-run] %s\n' "$1"
    return 0
  fi
  bash -c "$1"
}

# ---------------------------------------------------------------- 包管理器
PKG=""
detect_pkg() {
  for c in apt-get dnf yum pacman apk zypper; do
    command -v "$c" >/dev/null 2>&1 && { PKG="$c"; return 0; }
  done
  PKG=""
  return 1
}

need_root_for_pkg() {
  [[ $IS_ROOT -eq 1 ]] || die "装系统包需要 root：sudo $0 $*"
}

pkg_install() {
  local pkgs="$*"
  log "安装系统包：$pkgs"
  case "$PKG" in
    apt-get) run_sh "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $pkgs" ;;
    dnf)     run_cmd dnf install -y -q $pkgs ;;
    yum)     run_cmd yum install -y -q $pkgs ;;
    pacman)  run_cmd pacman -Sy --noconfirm --needed $pkgs ;;
    apk)     run_cmd apk add --no-cache $pkgs ;;
    zypper)  run_cmd zypper --non-interactive install -y $pkgs ;;
    *) die "不认识的包管理器，手动装：$pkgs" ;;
  esac
}

# ---------------------------------------------------------------- Python
py_ok() {
  local c="$1"
  command -v "$c" >/dev/null 2>&1 || return 1
  "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

find_py() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    py_ok "$c" && { command -v "$c"; return 0; }
  done
  return 1
}

ensure_python() {
  local py
  if py="$(find_py)"; then
    ok "$("$py" -V 2>&1)  $py"
    PYTHON="$py"
    return 0
  fi
  warn "没找到 Python >= 3.10，尝试安装"
  detect_pkg || die "找不到包管理器，手动装 Python 3.10+ 后再跑"
  need_root_for_pkg "$@"
  case "$PKG" in
    apt-get) pkg_install python3 python3-venv python3-pip ;;
    dnf|yum) pkg_install python3 python3-pip ;;
    pacman)  pkg_install python python-pip ;;
    apk)     pkg_install python3 py3-pip python3-dev gcc musl-dev libffi-dev openssl-dev ;;
    zypper)  pkg_install python3 python3-pip ;;
  esac
  py="$(find_py)" || {
    "$PKG" --version >/dev/null 2>&1 || true
    local v; v="$(python3 -V 2>&1 || echo '无 python3')"
    die "系统自带的是 $v，需要 3.10+。Ubuntu 20.04 请升级到 22.04+，或用 pyenv 装 3.12"
  }
  PYTHON="$py"
  ok "$("$PYTHON" -V 2>&1)"
}

# ---------------------------------------------------------------- 源码
SRC=""
resolve_source() {
  # 发行包 / 仓库内直接跑：脚本上两级就是项目根
  local cand
  cand="$(cd "$SELF_DIR/../.." && pwd)"
  if [[ -f "$cand/pyproject.toml" && -d "$cand/fxproxy" ]]; then
    SRC="$cand"
    ok "使用本地源码 $SRC"
    return 0
  fi
  # 否则 clone
  command -v git >/dev/null 2>&1 || {
    detect_pkg || die "需要 git 拉源码"
    need_root_for_pkg
    case "$PKG" in
      apt-get) pkg_install git ;;
      dnf|yum) pkg_install git ;;
      pacman)  pkg_install git ;;
      apk)     pkg_install git ;;
      zypper)  pkg_install git ;;
    esac
  }
  SRC="$INSTALL_DIR/src"
  if [[ -d "$SRC/.git" ]]; then
    log "更新已有源码 $SRC"
    run_sh "git -C '$SRC' pull --ff-only || true"
  else
    log "克隆源码到 $SRC"
    run_sh "git clone --depth 1 '$REPO_URL' '$SRC'"
  fi
}

# ---------------------------------------------------------------- venv + pip
PIP_ARGS=()
venv_has_pip() {
  [[ -x "$VENV/bin/python" ]] || return 1
  "$VENV/bin/python" -m pip --version >/dev/null 2>&1
}

make_venv() {
  log "创建 venv $VENV"
  if ! run_sh "'$PYTHON' -m venv '$VENV'"; then
    warn "venv 创建失败，补装 python3-venv 再试"
    need_root_for_pkg
    case "$PKG" in
      apt-get) pkg_install python3-venv ;;
      apk)     pkg_install py3-virtualenv ;;
      *) ;;
    esac
    run_sh "'$PYTHON' -m venv '$VENV'"
  fi
  # ensurepip 被裁掉的发行版（部分精简镜像）建出来照样没 pip
  if [[ $DRY_RUN -eq 0 ]] && ! venv_has_pip; then
    run_sh "'$VENV/bin/python' -m ensurepip --upgrade" || true
  fi
}

setup_venv() {
  [[ -n "$INDEX_URL" ]] && PIP_ARGS+=(-i "$INDEX_URL")
  [[ -n "$PROXY" ]] && PIP_ARGS+=(--proxy "$PROXY")
  # 只有 python 不算好的 venv：Debian 系的 python3-venv 没装全、上次安装
  # 中断，都会留下一个没有 pip 的目录，后面每条 pip 命令都报
  # "No module named pip"，还容易被当成网络问题反复重试。
  if venv_has_pip; then
    ok "复用已有 venv"
  elif [[ -x "$VENV/bin/python" ]]; then
    warn "$VENV 里没有 pip（venv 不完整），用 ensurepip 补"
    if ! run_sh "'$VENV/bin/python' -m ensurepip --upgrade --default-pip"; then
      warn "补不上，删掉重建"
      run_sh "rm -rf '$VENV'"
      make_venv
    fi
  else
    make_venv
  fi
  venv_has_pip || [[ $DRY_RUN -eq 1 ]] || die "venv 里仍然没有 pip，删掉 $VENV 重跑本脚本"
  run_sh "'$VENV/bin/pip' install -q -U pip setuptools wheel ${PIP_ARGS[*]:-}"
  local extras=""
  [[ $WITH_LOGIN_DEPS -eq 1 ]] && extras="[login]"
  log "安装 fxproxy${extras} ..."
  # 发行包里没有 .git，-e 也可用；失败退回非 editable 安装
  run_sh "'$VENV/bin/pip' install -q -e '$SRC$extras' ${PIP_ARGS[*]:-}" \
    || run_sh "'$VENV/bin/pip' install -q '$SRC$extras' ${PIP_ARGS[*]:-}"
  [[ $DRY_RUN -eq 1 ]] && return 0
  [[ -x "$VENV/bin/fxproxy" ]] || die "安装后没有 $VENV/bin/fxproxy，看上面的 pip 报错"
  ok "$("$VENV/bin/fxproxy" --version)"
}

# ---------------------------------------------------------------- 端口
port_free() {
  "$VENV/bin/python" - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
sys.exit(0)
PY
}

pick_port() {
  # $1=期望端口 $2=名字；被占就顺延，并告知
  local p="$1" name="$2"
  [[ "$p" =~ ^[0-9]+$ ]] || die "$name 端口不是数字：$p"
  (( p >= 1 && p <= 65535 )) || die "$name 端口超出 1-65535：$p"
  [[ $DRY_RUN -eq 1 ]] && { echo "$p"; return 0; }
  local start="$p"
  while ! port_free "$p"; do
    p=$((p + 1))
    (( p <= 65535 )) || die "$name 从 $start 起找不到空闲端口"
  done
  (( p != start )) && warn "$name 端口 $start 被占，改用 $p"
  echo "$p"
}

# ---------------------------------------------------------------- config.json
write_config() {
  log "写配置 $CONFIG"
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  [dry-run] 生成 config.json（tokens=%s country=%s http=%s socks=%s）\n' \
      "$([[ -n $TOKENS ]] && echo 有 || echo 无)" "${COUNTRY:-任意}" "$HTTP_PORT" "$SOCKS_PORT"
    return 0
  fi
  mkdir -p "$INSTALL_DIR"
  TOKENS="$TOKENS" COUNTRY="$COUNTRY" HOST="$HOST" HTTP_PORT="$HTTP_PORT" \
  SOCKS_PORT="$SOCKS_PORT" SUB_SERVER="$SUB_SERVER" COOKIE_FILE="$COOKIE_FILE" \
  "$VENV/bin/python" - "$CONFIG" <<'PY'
import json, os, sys
from pathlib import Path

path = Path(sys.argv[1])
cfg = {}
if path.exists():
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}

env = os.environ
raw = env.get("TOKENS", "").strip()
if raw:
    parts = [p.strip() for p in raw.replace(";", ",").replace("\n", ",").split(",")]
    tokens = [p for p in parts if p]
    # 追加去重，不覆盖已有账号
    existing = [t for t in cfg.get("refresh_tokens", []) if t]
    cfg["refresh_tokens"] = list(dict.fromkeys(existing + tokens))

country = env.get("COUNTRY", "").strip().upper()
if country:
    cfg["country"] = country
cfg.setdefault("country", "")
cfg["host"] = env.get("HOST") or cfg.get("host") or "127.0.0.1"
cfg["http_port"] = int(env.get("HTTP_PORT") or cfg.get("http_port") or 8080)
cfg["socks_port"] = int(env.get("SOCKS_PORT") or cfg.get("socks_port") or 1080)
cfg.setdefault("failover", 3)
cfg.setdefault("pool_strategy", "round-robin")
sub = env.get("SUB_SERVER", "").strip()
if sub:
    cfg["sub_server"] = sub
cfg.setdefault("sub_server", "")
cfg.setdefault("sub_http_port", 0)
cfg.setdefault("sub_socks_port", 0)

path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
print(f'  账号 {len(cfg["refresh_tokens"])} 个，监听 {cfg["host"]}:{cfg["http_port"]}/{cfg["socks_port"]}')
PY
}

maybe_import_cookie() {
  [[ -n "$COOKIE_FILE" ]] || return 0
  [[ -f "$COOKIE_FILE" ]] || die "凭证文件不存在：$COOKIE_FILE"
  log "导入凭证 $COOKIE_FILE"
  run_cmd "$VENV/bin/fxproxy" -c "$CONFIG" login --cookie-file "$COOKIE_FILE" --append \
    || warn "导入失败，稍后在面板里重试"
}

# ---------------------------------------------------------------- systemd
systemd_usable() {
  [[ $NO_SERVICE -eq 1 ]] && return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  [[ -d /run/systemd/system ]] || return 1
  return 0
}

install_units() {
  systemd_usable || return 1
  local dir="$UNIT_DIR" scope=""
  if [[ $SYSTEM_MODE -eq 1 ]]; then
    dir=/etc/systemd/system
  else
    mkdir -p "$dir"
    scope="--user"
  fi
  log "安装 systemd 单元到 $dir"
  [[ $DRY_RUN -eq 1 ]] && { printf '  [dry-run] 写 fxproxy.service / fxproxy-web.service\n'; return 0; }

  sed -e "s|%DIR%|$INSTALL_DIR|g" \
      -e "s|%CONFIG%|$CONFIG|g" \
      -e "s|%BIN%|$VENV/bin/fxproxy|g" \
      "$SELF_DIR/fxproxy.service.in" >"$dir/fxproxy.service"
  sed -e "s|%DIR%|$INSTALL_DIR|g" \
      -e "s|%CONFIG%|$CONFIG|g" \
      -e "s|%BIN%|$VENV/bin/fxproxy|g" \
      -e "s|%WEB_HOST%|$WEB_HOST|g" \
      -e "s|%WEB_PORT%|$WEB_PORT|g" \
      "$SELF_DIR/fxproxy-web.service.in" >"$dir/fxproxy-web.service"

  if [[ $SYSTEM_MODE -eq 1 ]]; then
    run_cmd systemctl daemon-reload
    run_cmd systemctl enable fxproxy.service fxproxy-web.service
  else
    run_cmd systemctl --user daemon-reload
    run_cmd systemctl --user enable fxproxy.service fxproxy-web.service
    # 用户级服务要 linger 才会在未登录时也启动
    if command -v loginctl >/dev/null 2>&1; then
      run_sh "loginctl enable-linger '$(id -un)' 2>/dev/null || true"
    fi
  fi
  ok "systemd 单元已安装（开机自启）"
  return 0
}

write_ctl_env() {
  local units="fxproxy fxproxy-web"
  [[ $NO_SERVICE -eq 1 ]] && units=""
  log "写 $CTL_ENV"
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  [dry-run] FXPROXY_DIR=%s SYSTEMD=%s\n' "$INSTALL_DIR" "$([[ -n $units ]] && echo 1 || echo 0)"
    return 0
  fi
  mkdir -p "$CTL_ENV_DIR"
  local sysd=0 usermode=0
  [[ -n "$units" ]] && sysd=1
  [[ "$SYSTEM_MODE" -eq 0 && -n "$units" ]] && usermode=1
  cat >"$CTL_ENV" <<EOF
# fxproxy-ctl 的路径配置（install.sh 生成，改完不用重装）
FXPROXY_DIR=$INSTALL_DIR
FXPROXY_SRC=$SRC
FXPROXY_CONFIG=$CONFIG
FXPROXY_BIN=$VENV/bin/fxproxy
FXPROXY_BIN_DIR=$VENV/bin
FXPROXY_HTTP_PORT=$HTTP_PORT
FXPROXY_SOCKS_PORT=$SOCKS_PORT
FXPROXY_WEB_PORT=$WEB_PORT
FXPROXY_WEB_HOST=$WEB_HOST
FXPROXY_SYSTEMD=$sysd
FXPROXY_USER_MODE=$usermode
FXPROXY_UNITS="$units"
FXPROXY_CTL_BIN=$CTL_BIN
EOF
  chmod 644 "$CTL_ENV"

  install -m 0755 "$SELF_DIR/fxproxy-ctl.sh" "$CTL_BIN"
  ok "管理命令：fxproxy-ctl（$CTL_BIN）"
  case ":$PATH:" in
    *":$(dirname "$CTL_BIN"):"*) ;;
    *) warn "$(dirname "$CTL_BIN") 不在 PATH 里，用绝对路径 $CTL_BIN 或加进 PATH" ;;
  esac
}

# ---------------------------------------------------------------- 起服务
start_now() {
  [[ $NO_START -eq 1 ]] && { log "--no-start，跳过启动"; return 0; }
  log "启动 fxproxy ..."
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  [dry-run] fxproxy-ctl start\n'
    return 0
  fi
  "$CTL_BIN" start || warn "启动失败，看 fxproxy-ctl logs"
  local i
  # 面板端口从「没人听」等到「有人听」，最多 15 秒；超时不报错，交给 status 显示
  for ((i = 0; i < 15; i++)); do
    port_free "$WEB_PORT" || break
    sleep 1
  done
  "$CTL_BIN" status || true
}

# ---------------------------------------------------------------- 卸载
do_uninstall() {
  log "卸载 fxproxy"
  if [[ -x "$CTL_BIN" ]]; then
    if [[ $PURGE -eq 1 ]]; then run_cmd "$CTL_BIN" uninstall --purge
    else run_cmd "$CTL_BIN" uninstall; fi
    return 0
  fi
  # ctl 不在了就手工清
  systemd_usable || true
  local dir="$UNIT_DIR"
  [[ $SYSTEM_MODE -eq 1 ]] && dir=/etc/systemd/system
  if [[ -d "$dir" ]]; then
    run_cmd rm -f "$dir/fxproxy.service" "$dir/fxproxy-web.service"
    if [[ $SYSTEM_MODE -eq 1 ]]; then run_cmd systemctl daemon-reload
    else run_cmd systemctl --user daemon-reload || true; fi
  fi
  [[ $PURGE -eq 1 ]] && run_cmd rm -rf "$INSTALL_DIR"
  ok "已清理"
}

# ---------------------------------------------------------------- 主流程
main() {
  [[ $DRY_RUN -eq 1 ]] && warn "dry-run：只打印步骤，不改系统"

  if [[ $UNINSTALL -eq 1 ]]; then
    do_uninstall
    return 0
  fi

  log "安装模式：$([[ $SYSTEM_MODE -eq 1 ]] && echo 系统级 || echo 用户级)  目录：$INSTALL_DIR"

  ensure_python
  resolve_source
  setup_venv

  HTTP_PORT="$(pick_port "$HTTP_PORT" HTTP)"
  SOCKS_PORT="$(pick_port "$SOCKS_PORT" SOCKS5)"
  WEB_PORT="$(pick_port "$WEB_PORT" 面板)"
  [[ "$HTTP_PORT" != "$SOCKS_PORT" && "$HTTP_PORT" != "$WEB_PORT" && "$SOCKS_PORT" != "$WEB_PORT" ]] \
    || die "三个端口不能重复（HTTP=$HTTP_PORT SOCKS5=$SOCKS_PORT 面板=$WEB_PORT）"

  write_config
  maybe_import_cookie

  if install_units; then
    :
  else
    warn "没有可用的 systemd，用 nohup 后台运行（重启机器后需手动 fxproxy-ctl start）"
    NO_SERVICE=1
  fi
  write_ctl_env

  start_now

  echo
  ok "完成"
  echo "  面板     : http://${WEB_HOST/0.0.0.0/127.0.0.1}:$WEB_PORT"
  echo "  订阅     : http://<本机地址>:$WEB_PORT/clash.yaml"
  echo "  HTTP     : $HOST:$HTTP_PORT"
  echo "  SOCKS5   : $HOST:$SOCKS_PORT"
  echo "  管理     : fxproxy-ctl {start|stop|restart|status|logs|update|url}"
  echo "  卸载     : sudo $SELF_DIR/install.sh --uninstall [--purge]"
}

main "$@"

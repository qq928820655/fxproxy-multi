#!/usr/bin/env bash
# fxproxy 一行更新（Linux / macOS）
#
#   curl -fsSL https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/linux/update.sh | bash
#   curl -fsSL .../update.sh | bash -s -- --check
#   curl -fsSL .../update.sh | bash -s -- --proxy https://ghproxy.net/
#
# 这个脚本只是取壳：找到已装实例（ctl.env + fxproxy-ctl），把活交给 `fxproxy-ctl update`。
# 更新逻辑只维护一份（deploy/linux/fxproxy-ctl.sh），一行命令、面板「一键更新」、
# `fxproxy update` 三条入口走的是同一条路，不会出现"某个入口忘了改"。
set -uo pipefail

# ---------------------------------------------------------------- 管道执行自愈
# `curl | bash` 时脚本从 stdin 进来：BASH_SOURCE 为空，且 bash 正占着 fd0，
# 脚本里任何 read 都会读到脚本尾巴。先落盘，再带 tty 重跑。
if [[ -z "${BASH_SOURCE[0]:-}" ]]; then
  _self="$(mktemp "${TMPDIR:-/tmp}/fxproxy-upd.XXXXXX.sh")"
  cat >"$_self"
  if (exec 3</dev/tty) 2>/dev/null; then
    bash "$_self" "$@" </dev/tty
  else
    bash "$_self" "$@" </dev/null
  fi
  _rc=$?
  rm -f "$_self" 2>/dev/null || true
  exit $_rc
fi

if [[ -t 1 ]]; then
  C0=$'\033[0m'; CB=$'\033[1m'; CC=$'\033[36m'; CG=$'\033[32m'; CY=$'\033[33m'; CR=$'\033[31m'; CD=$'\033[90m'
else
  C0=""; CB=""; CC=""; CG=""; CY=""; CR=""; CD=""
fi
say()  { printf '%s\n' "${CC}[fxproxy]${C0} $*"; }
ok()   { printf '  %sok%s %s\n' "$CG" "$C0" "$*"; }
warn() { printf '  %s!%s %s\n' "$CY" "$C0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$CR" "$C0" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
fxproxy 一行更新

用法:
  curl -fsSL <raw>/deploy/linux/update.sh | bash
  curl -fsSL <raw>/deploy/linux/update.sh | bash -s -- --check

选项（原样转给 fxproxy-ctl update）:
  --check            只看有没有新版，不下载不重启
  --proxy URL        GitHub 镜像前缀，如 https://ghproxy.net/
  --repo owner/repo  换仓库
  -h, --help         这屏

想装新机器（不是更新）用 deploy/linux/bootstrap.sh。
EOF
}

ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

# ---------------------------------------------------------------- 找已装实例
CTL=""
for c in "${FXPROXY_CTL:-}" "$(command -v fxproxy-ctl 2>/dev/null || true)" \
         /usr/local/bin/fxproxy-ctl "${HOME}/.local/bin/fxproxy-ctl"; do
  if [[ -n "$c" && -x "$c" ]]; then CTL="$c"; break; fi
done

ENVF=""
for f in "${FXPROXY_CTL_ENV:-}" /etc/fxproxy/ctl.env "${XDG_CONFIG_HOME:-$HOME/.config}/fxproxy/ctl.env"; do
  if [[ -n "$f" && -r "$f" ]]; then ENVF="$f"; break; fi
done

if [[ -z "$CTL" && -z "$ENVF" ]]; then
  warn "这台机器上没找到 fxproxy 的安装记录（ctl.env / fxproxy-ctl 都没有）"
  printf '\n  装新实例：\n' >&2
  printf '    curl -fsSL https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/linux/bootstrap.sh | bash\n\n' >&2
  printf '  已手动解压但没跑过 install.sh 的，先跑一次它生成 ctl.env：\n' >&2
  printf '    sudo bash <解压目录>/deploy/linux/install.sh\n\n' >&2
  exit 1
fi

if [[ -z "$CTL" ]]; then
  # 有 ctl.env 但命令没进 PATH（装完没重登、或 ~/.local/bin 不在 PATH）
  # shellcheck disable=SC1090
  FX_DIR="$(. "$ENVF" && printf '%s' "${FXPROXY_DIR:-}")"
  for c in "$FX_DIR/deploy/linux/fxproxy-ctl.sh" "$FX_DIR/deploy/linux/ctl.sh"; do
    if [[ -x "$c" ]]; then CTL="$c"; break; fi
  done
  if [[ -z "$CTL" ]]; then
    warn "找到 $ENVF，但里面记的目录里没有 ctl 脚本：$FX_DIR"
    die "重跑一次 install.sh 修复（不会动 config.json 里的凭证）"
  fi
  warn "fxproxy-ctl 不在 PATH，直接用 $CTL"
fi

# ctl 自己靠 ctl.env 定位安装目录；用户级装在 ~/.config 时默认路径对不上，
# 这里把它找到的那份显式传下去。
if [[ -n "$ENVF" ]]; then export FXPROXY_CTL_ENV="$ENVF"; fi

say "更新 $CTL ${ARGS[*]:-}"
if [[ ${#ARGS[@]} -gt 0 ]]; then
  "$CTL" update "${ARGS[@]}"
else
  "$CTL" update
fi
rc=$?
if [[ $rc -eq 0 ]]; then
  ok "更新流程结束"
  printf '\n  看状态：%sfxproxy-ctl status%s   看日志：%sfxproxy-ctl logs%s\n\n' "$CB" "$C0" "$CB" "$C0"
else
  warn "更新返回 $rc，往上翻日志"
fi
exit $rc

#!/usr/bin/env bash
# Oracle Cloud Always Free VM 一键部署 fxproxy。
# 适用：Ubuntu 22.04/24.04、Debian 12（Oracle ARM/A1 或 AMD E1 免费机）。
#
# 前提：凭证已在住宅 IP 上取好（refresh_token），机房 IP 只做落地转发。
# 用法：
#   export FXPROXY_REFRESH_TOKENS="TOKEN1,TOKEN2"
#   sudo -E bash deploy/oracle-free-bootstrap.sh --country US
#
# 脚本做四件事：装依赖、写 systemd（代理 + 面板）、探测公网 IP、放行端口。
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/qq928820655/fxproxy-multi.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/fxproxy}"
ENV_FILE="${ENV_FILE:-/etc/fxproxy.env}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
COUNTRY=""
SUB_SERVER=""
HTTP_PORT=8080
SOCKS_PORT=1080
WEB_PORT=8765

usage() {
  cat <<'EOF'
用法: oracle-free-bootstrap.sh [选项]
  --country CODE      出口国家 ISO 码，默认 US
  --sub-server HOST   订阅对外地址（公网 IP 或域名）。留空则自动探测本机公网 IP
  --http-port N       HTTP 代理端口，默认 8080
  --socks-port N      SOCKS5 端口，默认 1080
  --web-port N        面板/订阅端口，默认 8765
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --country)     COUNTRY="${2:-}"; shift 2 ;;
    --sub-server)  SUB_SERVER="${2:-}"; shift 2 ;;
    --http-port)   HTTP_PORT="${2:-}"; shift 2 ;;
    --socks-port)  SOCKS_PORT="${2:-}"; shift 2 ;;
    --web-port)    WEB_PORT="${2:-}"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "${FXPROXY_REFRESH_TOKENS:-}" ]]; then
  echo "[!] 缺少 FXPROXY_REFRESH_TOKENS。先在住宅 IP 取 token 再 export。" >&2
  exit 2
fi

if [[ $EUID -ne 0 ]]; then
  echo "[!] 需要 root 运行（写 /etc 与 systemd）。用 sudo -E 保留环境变量。" >&2
  exit 2
fi

log() { echo "[bootstrap] $*"; }

# ---- 1. 依赖 ----
log "安装 Python 与 venv ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl git >/dev/null

# ---- 2. 代码 ----
if [[ -d "$INSTALL_DIR/.git" ]]; then
  log "更新已有仓库 $INSTALL_DIR ..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  log "克隆仓库到 $INSTALL_DIR ..."
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
  log "创建 venv 并安装运行依赖 ..."
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r requirements.txt

# ---- 3. 探测公网 IP（订阅必须指向可达地址，127.0.0.1 指向客户端自己）----
if [[ -z "$SUB_SERVER" ]]; then
  log "探测公网 IPv4 ..."
  SUB_SERVER="$(curl -4 -sS --max-time 10 https://api.ipify.org || true)"
  if [[ -z "$SUB_SERVER" ]]; then
    SUB_SERVER="$(curl -4 -sS --max-time 10 https://ifconfig.me/ip || true)"
  fi
fi
if [[ -z "$SUB_SERVER" ]]; then
  echo "[!] 公网 IP 探测失败，用 --sub-server 手动指定（域名也可）。" >&2
  exit 2
fi
log "订阅对外地址: $SUB_SERVER"

# ---- 4. 环境文件（凭证不进仓库，600 权限）----
log "写环境文件 $ENV_FILE ..."
umask 077
cat > "$ENV_FILE" <<EOF
FXPROXY_REFRESH_TOKENS=${FXPROXY_REFRESH_TOKENS}
FXPROXY_HOST=0.0.0.0
FXPROXY_COUNTRY=${COUNTRY:-US}
FXPROXY_HTTP_PORT=${HTTP_PORT}
FXPROXY_SOCKS_PORT=${SOCKS_PORT}
FXPROXY_SUB_SERVER=${SUB_SERVER}
FXPROXY_SUB_HTTP_PORT=${HTTP_PORT}
FXPROXY_SUB_SOCKS_PORT=${SOCKS_PORT}
EOF
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

PY="$INSTALL_DIR/.venv/bin/python"

# ---- 5. systemd：代理与面板各一个 unit，失败自动重启 ----
log "写 systemd units ..."
cat > /etc/systemd/system/fxproxy.service <<EOF
[Unit]
Description=fxproxy landing proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PY} -m fxproxy run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/fxproxy-web.service <<EOF
[Unit]
Description=fxproxy control panel + subscription
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PY} -m fxproxy web --no-open --web-host 0.0.0.0 --web-port ${WEB_PORT}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now fxproxy.service fxproxy-web.service

# ---- 6. 放行本机端口 ----
# 注意：Oracle 除系统防火墙外，还必须在云控制台的 ingress 规则放行同样端口。
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  log "ufw 放行 ${HTTP_PORT}/${SOCKS_PORT}/${WEB_PORT} ..."
  ufw allow "${HTTP_PORT}/tcp" >/dev/null
  ufw allow "${SOCKS_PORT}/tcp" >/dev/null
  ufw allow "${WEB_PORT}/tcp" >/dev/null
fi
if command -v iptables >/dev/null 2>&1; then
  iptables -C INPUT -p tcp --dport "$HTTP_PORT" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT -p tcp --dport "$HTTP_PORT" -j ACCEPT
  iptables -C INPUT -p tcp --dport "$SOCKS_PORT" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT -p tcp --dport "$SOCKS_PORT" -j ACCEPT
  iptables -C INPUT -p tcp --dport "$WEB_PORT" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT -p tcp --dport "$WEB_PORT" -j ACCEPT
fi

log "完成。"
echo
echo "  面板:      http://${SUB_SERVER}:${WEB_PORT}"
echo "  订阅地址:  http://${SUB_SERVER}:${WEB_PORT}/clash.yaml"
echo "  HTTP 代理: ${SUB_SERVER}:${HTTP_PORT}"
echo "  SOCKS5:    ${SUB_SERVER}:${SOCKS_PORT}"
echo
echo "  查看状态:  systemctl status fxproxy fxproxy-web"
echo "  查看日志:  journalctl -u fxproxy -f"
echo
echo "  Oracle 还需在控制台 → 实例 → 网络安全组 → ingress 放行"
echo "  ${HTTP_PORT}/${SOCKS_PORT}/${WEB_PORT} 三个 TCP 端口，否则外网连不上。"

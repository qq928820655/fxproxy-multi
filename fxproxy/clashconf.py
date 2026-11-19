"""Generate a Clash/Mihomo subscription YAML pointing at the local proxy.

The fxproxy server is a *local* HTTP CONNECT + SOCKS5 listener, so the
subscription only describes how a Clash/Mihomo client should reach it. There
are no remote nodes baked in: account rotation and node failover happen inside
fxproxy, the client just sees two plain local proxies.
"""
from __future__ import annotations


def build_clash_yaml(
    server: str = "127.0.0.1",
    http_port: int = 8080,
    socks_port: int = 1080,
    name_prefix: str = "fxproxy",
    http_name: str | None = None,
    socks_name: str | None = None,
) -> str:
    """Return a complete Clash/Mihomo config as YAML text.

    ``server`` must be reachable from the device importing the subscription —
    use the LAN/VPS address when importing on another machine, not 127.0.0.1.
    """
    server = (server or "127.0.0.1").strip()
    http_port = int(http_port)
    socks_port = int(socks_port)
    hname = http_name or f"{name_prefix}-http"
    sname = socks_name or f"{name_prefix}-socks5"
    # Quote the server: IPv6 literals contain colons and need YAML quoting.
    q = '"' + server.replace('"', '\\"') + '"'
    return (
        "# fxproxy Clash/Mihomo subscription\n"
        "# fxproxy handles account rotation + node failover internally.\n"
        f"# Proxy endpoint: {server}:{http_port}\n"
        "port: 7890\n"
        "socks-port: 7891\n"
        "allow-lan: false\n"
        "mode: rule\n"
        "log-level: info\n"
        # 网关是双栈，但到 Cloudflare 的 IPv6 国际链路经常不稳/不通。
        # 强制用 IPv4 解析并连接 server，避免客户端优先走 IPv6 导致
        # 单连接也频繁超时。目标域名仍由服务端解析，不受此影响。
        "ip-version: ipv4\n"
        "proxies:\n"
        f'  - name: "{hname}"\n'
        "    type: http\n"
        f"    server: {q}\n"
        f"    port: {http_port}\n"
        f'  - name: "{sname}"\n'
        "    type: socks5\n"
        f"    server: {q}\n"
        f"    port: {socks_port}\n"
        "proxy-groups:\n"
        '  - name: "PROXY"\n'
        "    type: select\n"
        "    proxies:\n"
        f'      - "{hname}"\n'
        f'      - "{sname}"\n'
        "      - DIRECT\n"
        "rules:\n"
        "  - MATCH,PROXY\n"
    )


def build_surge_conf(server: str = "127.0.0.1", http_port: int = 8080,
                     name_prefix: str = "fxproxy") -> str:
    """Surge/Shadowrocket-style single-line proxy (bonus, no dependency)."""
    return f'{name_prefix}=https, {server}, {int(http_port)}, direct\n'

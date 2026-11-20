"""Local proxy servers (HTTP CONNECT + SOCKS5) that egress through a
Firefox IP Protection Fastly node.

For every client connection we open a TLS connection to a Fastly node
(``<host>:2499``), issue an HTTP ``CONNECT <target>`` with the current
proxy pass in ``Proxy-Authorization: Bearer <jwt>``, then splice bytes
in both directions.
"""
from __future__ import annotations

import asyncio
import errno
import ipaddress
import random
import socket
import ssl
import struct
from typing import Optional

from .guardian import GuardianClient, TokenPool

FASTLY_CONNECT_TIMEOUT = 20

# Windows 的 socket 错误码与 POSIX 不同，同一含义要两个都认。
_EADDRINUSE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}


def _bind_error(label: str, flag: str, host: str, port: int, exc: OSError) -> str:
    """把 bind 失败翻译成一条能直接照做的中文提示。

    默认端口 8080 是本地开发的重灾区（Jenkins、Tomcat、各种 dev server），
    裸抛 OSError 只会看到 Errno 10048，用户不知道下一步做什么。
    """
    if exc.errno in _EADDRINUSE:
        return (f"{label} 端口 {port} 已被其它程序占用（{host}:{port}）。"
                f"换一个端口：加参数 {flag} <新端口>，"
                f"或在面板「运行状态 → 端口」里直接改后应用。")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (f"{label} 端口 {port} 没有绑定权限（1024 以下端口需要 root/管理员）。"
                f"改用 1024 以上的端口，例如 {flag} 18080。")
    return f"{label} 端口 {port} 监听失败：{exc}"


class AccountAuthFailure(ConnectionError):
    """The account's proxy pass was rejected (401/403) — cool down that account."""


class NodePicker:
    """Chooses a Fastly node for a country preference."""

    def __init__(self, client: GuardianClient, country: Optional[str] = None,
                 node: Optional[str] = None, exclude=None):
        self.client = client
        self.country = (country or "").upper() or None
        # Pin to one concrete node hostname (overrides country fan-out).
        self.node = (node or "").strip() or None
        # Hosts the user disabled in the panel; excluded from random pools.
        self.exclude = {str(h) for h in (exclude or ()) if h}
        self._by_country: dict[str, dict] = {}
        # Hostname of the most recently picked node; lets the panel show the
        # actual egress country in "any country" mode. Set from worker threads,
        # read from the asyncio thread — attribute assignment is atomic (GIL).
        self.last_host: Optional[str] = None

    def refresh(self) -> None:
        self._by_country = self.client.nodes_by_country()
        if not self._by_country:
            raise RuntimeError("no Fastly nodes in server list")
        if self.country and self.country not in self._by_country:
            raise RuntimeError(
                f"country {self.country!r} not available; choices: "
                + ", ".join(sorted(self._by_country))
            )

    def _pool(self) -> list[tuple[str, int]]:
        if not self._by_country:
            self.refresh()
        if self.node:
            for v in self._by_country.values():
                for h, p in v["nodes"]:
                    if h == self.node:
                        return [(h, p)]
            raise RuntimeError(f"node {self.node!r} not in server list")
        if self.country:
            pool = list(self._by_country[self.country]["nodes"])
        else:
            pool = [n for v in self._by_country.values() for n in v["nodes"]]
        if self.exclude:
            pool = [(h, p) for h, p in pool if h not in self.exclude]
            if not pool:
                raise RuntimeError("所有可用节点都被禁用了")
        return pool

    def pick(self) -> tuple[str, int]:
        h, p = random.choice(self._pool())
        self.last_host = h
        return h, p

    def country_of(self, host: str) -> str:
        """Reverse-lookup the country code for a node hostname."""
        for code, meta in (self._by_country or {}).items():
            for h, _ in meta["nodes"]:
                if h == host:
                    return code
        return ""

    def pick_many(self, n: int) -> list[tuple[str, int]]:
        """Return up to n distinct random nodes (for failover)."""
        pool = self._pool()
        random.shuffle(pool)
        return pool[:n]


class ProxyServer:
    def __init__(
        self,
        client,  # TokenPool | GuardianClient
        node_picker: NodePicker,
        host: str = "127.0.0.1",
        http_port: int = 8080,
        socks_port: int = 1080,
        failover: int = 3,
        force_ipv4_egress: bool = False,
    ):
        # 兼容旧调用：传单个 GuardianClient 时包成单账号池
        if isinstance(client, TokenPool):
            self.pool = client
            self.client = client.clients[0]
        else:
            self.pool = TokenPool([client.refresh_token], session=client._s)
            self.client = client
        self.picker = node_picker
        self.host = host
        self.http_port = http_port
        self.socks_port = socks_port
        self.failover = max(1, failover)
        self.force_ipv4_egress = bool(force_ipv4_egress)
        self._ssl = ssl.create_default_context()
        # 每完成一条连接回调 (up, down) 字节数；面板用它把 8080/1080 的
        # 外部流量并入本地统计。为 None 时不统计（如测速临时实例）。
        self._on_traffic = None

    # ---- egress address policy ---------------------------------------
    @staticmethod
    def _is_ip_literal(host: str) -> bool:
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    async def _resolve_ipv4(self, host: str) -> str:
        """Resolve ``host`` to its first IPv4 address.

        Used when ``force_ipv4_egress`` is on: CONNECTing an IPv4 literal makes
        Fastly reach the target over IPv4, so the target sees an IPv4 source IP
        (the node's IPv4 egress) instead of the node's IPv6 egress.
        """
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        for info in infos:
            return info[4][0]
        raise ConnectionError(f"no IPv4 address for {host}")

    # ---- upstream tunnel ---------------------------------------------
    async def _connect_one(self, node_host, node_port, target_host, target_port,
                           jwt, conn=None):
        """CONNECT through one node. ``conn`` may carry a pre-handshaked
        (reader, writer) pair from a connection pool; TLS setup is skipped
        then and only the CONNECT round-trip is paid."""
        if conn is None:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    node_host, node_port, ssl=self._ssl, server_hostname=node_host
                ),
                timeout=FASTLY_CONNECT_TIMEOUT,
            )
        else:
            reader, writer = conn
        # 出口强制 IPv4：域名先在本机解析成 IPv4，再 CONNECT IP 字面量。
        # Fastly 只能用 IPv4 连目标，目标看到的源 IP 就是节点的 IPv4 出口。
        # 已是 IP 字面量（含 IPv6）时不再改写——无法把 IPv6 目标“转成”IPv4。
        if self.force_ipv4_egress and not self._is_ip_literal(target_host):
            target_host = await self._resolve_ipv4(target_host)
        target = f"{target_host}:{target_port}"
        req = (
            f"CONNECT {target} HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            f"Proxy-Authorization: Bearer {jwt}\r\n"
            f"\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=FASTLY_CONNECT_TIMEOUT)
            if not chunk:
                break
            head += chunk
        status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200 " not in status_line:
            try:
                writer.close()
            except Exception:
                pass
            # 401/403 = 账号凭证/配额问题，冷却账号；其它 = 节点问题，只换节点
            if " 401 " in status_line or " 403 " in status_line:
                raise AccountAuthFailure(f"CONNECT via {node_host}: {status_line}")
            raise ConnectionError(f"CONNECT via {node_host}: {status_line}")
        leftover = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        return reader, writer, leftover, node_host

    async def _open_tunnel(self, target_host: str, target_port: int):
        """Open a tunnel with two-level failover: accounts × nodes.

        The pool picks an account order (strategy-dependent). For each account
        we mint its own proxy pass and try several nodes. A node-level failure
        just rotates nodes; an auth failure (401/403) cools down that account
        and moves to the next one. Complexity: O(accounts × failover) attempts,
        bounded by the pool size to avoid one request hammering every account.
        """
        accounts = self.pool.pick_many(len(self.pool.clients))
        last_err = None
        tried_nodes = 0
        for acct_idx, gc in accounts:
            try:
                jwt = await asyncio.to_thread(gc.proxy_pass)
            except Exception as e:
                last_err = e
                self.pool.report_failure(gc)
                _log(f"账号 #{acct_idx + 1} 取凭证失败（{e!r}），换下一个账号")
                continue
            nodes = self.picker.pick_many(self.failover)
            for node_host, node_port in nodes:
                tried_nodes += 1
                try:
                    conn = await self._connect_one(
                        node_host, node_port, target_host, target_port, jwt
                    )
                    # 出口已确立：记录实际使用的节点，供面板显示国家
                    self.picker.last_host = node_host
                    return conn
                except AccountAuthFailure as e:
                    # 账号级失败：冷却账号，不再试这个账号的其它节点
                    last_err = e
                    self.pool.report_failure(gc)
                    _log(f"账号 #{acct_idx + 1} 鉴权失败（{e!r}），换下一个账号")
                    break
                except Exception as e:
                    last_err = e
                    _log(f"节点 {node_host} 连接失败（{e!r}），尝试下一个")
        raise ConnectionError(f"{tried_nodes} 次尝试全部失败: {last_err!r}")

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    counter=None, key=None):
        # OSError covers ConnectionError and ssl.SSLError (incl. the benign
        # APPLICATION_DATA_AFTER_CLOSE_NOTIFY seen when a peer tears the TLS
        # session down mid-transfer); CancelledError fires when the sibling
        # direction finishes first and we cancel this one.
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
                if counter is not None:
                    counter[key] += len(data)
        except (OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _splice(self, c_reader, c_writer, u_reader, u_writer, leftover=b""):
        stat = {"up": len(leftover), "down": 0}
        if leftover:
            c_writer.write(leftover)
            await c_writer.drain()
        t1 = asyncio.ensure_future(self._pipe(c_reader, u_writer, stat, "up"))
        t2 = asyncio.ensure_future(self._pipe(u_reader, c_writer, stat, "down"))
        try:
            _, pending = await asyncio.wait(
                {t1, t2}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            pending = {t1, t2}
            raise
        finally:
            # one direction ended -> close both ends so the other unblocks,
            # then cancel and reap it (avoids "Task was destroyed but pending").
            for w in (c_writer, u_writer):
                try:
                    w.close()
                except Exception:
                    pass
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self._on_traffic is not None and (stat["up"] or stat["down"]):
                try:
                    self._on_traffic(stat["up"], stat["down"])
                except Exception:
                    pass

    # ---- HTTP proxy ---------------------------------------------------
    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            parts = request_line.decode(errors="replace").split()
            if len(parts) < 3:
                writer.close()
                return
            method, uri, _ = parts[0], parts[1], parts[2]

            # drain request headers
            headers = b""
            while True:
                line = await reader.readline()
                headers += line
                if line in (b"\r\n", b"", b"\n"):
                    break

            if method.upper() == "CONNECT":
                host, _, port = uri.partition(":")
                target_port = int(port or 443)
                await self._connect_and_splice(host, target_port, reader, writer, client_ack=True)
            else:
                # absolute-form: http://host[:port]/path
                host, target_port, rest = _parse_absolute(uri)
                if host is None:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
                # rebuild origin-form request for upstream target
                first = f"{method} {rest} HTTP/1.1\r\n".encode()
                initial = first + headers
                await self._connect_and_splice(
                    host, target_port, reader, writer, client_ack=False, initial=initial
                )
        except Exception as e:
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
            _log(f"HTTP 连接出错 {peer}: {e!r}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _connect_and_splice(
        self, host, port, c_reader, c_writer, client_ack, initial=b""
    ):
        u_reader, u_writer, leftover, node = await self._open_tunnel(host, port)
        if client_ack:
            c_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await c_writer.drain()
        if initial:
            u_writer.write(initial)
            await u_writer.drain()
        await self._splice(c_reader, c_writer, u_reader, u_writer, leftover)

    # ---- SOCKS5 proxy -------------------------------------------------
    async def _handle_socks(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            # greeting
            ver = await reader.readexactly(1)
            if ver != b"\x05":
                writer.close()
                return
            nmethods = (await reader.readexactly(1))[0]
            await reader.readexactly(nmethods)
            writer.write(b"\x05\x00")  # no auth
            await writer.drain()

            # request
            hdr = await reader.readexactly(4)
            _, cmd, _, atyp = hdr
            if cmd != 0x01:  # only CONNECT
                writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                writer.close()
                return
            if atyp == 0x01:  # IPv4
                host = ".".join(str(b) for b in await reader.readexactly(4))
            elif atyp == 0x03:  # domain
                ln = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(ln)).decode()
            elif atyp == 0x04:  # IPv6
                raw = await reader.readexactly(16)
                host = ":".join(f"{raw[i]<<8|raw[i+1]:x}" for i in range(0, 16, 2))
            else:
                writer.close()
                return
            port = struct.unpack("!H", await reader.readexactly(2))[0]

            try:
                u_reader, u_writer, leftover, node = await self._open_tunnel(host, port)
            except Exception as e:
                writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                writer.close()
                _log(f"SOCKS 连接失败 {host}:{port}: {e!r}")
                return
            # success reply
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await self._splice(reader, writer, u_reader, u_writer, leftover)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:
            _log(f"SOCKS 连接出错 {peer}: {e!r}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # ---- lifecycle ----------------------------------------------------
    async def start(self):
        """Start listeners and return once they're bound (non-blocking)."""
        self.picker.refresh()
        # 预热各账号凭证：失败账号进冷却，成功的账号立即可用
        def _prime():
            for _, gc in self.pool.pick_many(len(self.pool.clients)):
                try:
                    gc.proxy_pass()
                except Exception as e:
                    self.pool.report_failure(gc)
                    _log(f"账号启动预热失败: {e!r}")
        await asyncio.to_thread(_prime)
        try:
            self._http_srv = await asyncio.start_server(
                self._handle_http, self.host, self.http_port
            )
        except OSError as e:
            raise RuntimeError(
                _bind_error("HTTP", "--http-port", self.host, self.http_port, e)
            ) from e
        try:
            self._socks_srv = await asyncio.start_server(
                self._handle_socks, self.host, self.socks_port
            )
        except OSError as e:
            # HTTP 已绑上，回滚掉，避免半个服务留在那里占端口
            self._http_srv.close()
            try:
                await self._http_srv.wait_closed()
            except Exception:
                pass
            raise RuntimeError(
                _bind_error("SOCKS5", "--socks-port", self.host, self.socks_port, e)
            ) from e
        _log(f"HTTP 代理  http://{self.host}:{self.http_port}")
        _log(f"SOCKS5 代理 socks5://{self.host}:{self.socks_port}")
        _log(f"出口国家: {self.picker.country or '任意'}")

    async def switch_country(self, country: Optional[str]):
        """Switch egress country without touching the listeners.

        New connections immediately use the new country; already-open
        tunnels keep running until they close on their own.
        """
        picker = NodePicker(self.client, country)
        await asyncio.to_thread(picker.refresh)
        self.picker = picker
        _log(f"出口国家切换为: {picker.country or '任意'}")

    async def stop(self):
        for attr in ("_http_srv", "_socks_srv"):
            srv = getattr(self, attr, None)
            if srv is not None:
                srv.close()
                try:
                    await srv.wait_closed()
                except Exception:
                    pass
                setattr(self, attr, None)

    async def serve(self):
        await self.start()
        async with self._http_srv, self._socks_srv:
            await asyncio.gather(
                self._http_srv.serve_forever(), self._socks_srv.serve_forever()
            )


def _parse_absolute(uri: str):
    # http://host:port/path  ->  (host, port, "/path")
    if "://" not in uri:
        return None, None, None
    scheme, rest = uri.split("://", 1)
    authority, _, path = rest.partition("/")
    path = "/" + path
    host, _, port = authority.partition(":")
    return host, int(port) if port else (443 if scheme == "https" else 80), path


def _log(msg: str):
    print(f"[fxproxy] {msg}", flush=True)

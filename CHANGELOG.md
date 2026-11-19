# 变更记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
上游基线：`c4ba254`（ios151/firefox）。

## [0.3.1] — 2026-11-19

### 新增

- **发行包**：`tools/build_release.py` 打 `dist/fxproxy-<ver>-win.zip`（解压即可双击
  `deploy\windows\install.bat`），内置扩展 zip/crx；`--check` 校验结构，刻意排除
  `.venv` / 真实 `config.json` / `tools/.crx-key.pem`。`tests/test_release.py` 锁住
  唯一顶层目录与必需文件。
- **端口可配置**：面板「② 运行状态 → 端口」直接改 HTTP / SOCKS5 端口并应用，
  运行中的代理按新端口自动重启，`/clash.yaml` 订阅同步跟随。新增
  `POST /api/ports`（范围校验、同端口拒绝、面板端口冲突拒绝、占用预检），
  `GET /api/state` 增加 `web_port` 供前端排除冲突。

### 修复

- **订阅端口不跟随**：`config.load()` 把 `sub_http_port: 0`（语义是「跟随监听端口」）
  在加载时固化成了当时的端口，之后改监听端口订阅仍指向旧端口。现在保留 0，
  由消费端用 `or` 回退到 `http_port` / `socks_port`。
- **测速端口写死 8095/8096**：撞上本机其它服务时测速直接失败且看不出原因。
  改为现场挑一对空闲端口。
- **端口被占的报错**：原来只有 `OSError: [Errno 10048]`。现在给出中文提示与
  具体改法（`--http-port` / 面板路径 / 低位端口权限提示），SOCKS5 绑定失败会
  回滚已绑定的 HTTP 监听。
- **Windows**：`install.bat` 重跑时旧端口被别的程序抢走会启动失败，现在检测到
  占用后自动顺延到空闲端口；`start.bat` 增加 SOCKS5 与面板端口占用前置检查。

## [0.3.0] — 2026-11-19

### 新增

- **多账号池**：`refresh_tokens` 列表 + `pool_strategy`（`round-robin` / `quota` / `failover`），
  账号级失败冷却与节点级自动切换。CLI `accounts` / `login --append`。
- **凭证迁移**：Chrome MV3 扩展读取 `__fxa_storage.accounts` 的 `sessionToken`，
  支持 `fxs_` Bearer 形式；六种导出格式（json / lines / cookie / netscape / csv / accounts）。
  `fxproxy login --cookie / --cookie-file` 直接吃插件导出内容。
- **订阅**：`GET /clash.yaml` 与 `fxproxy export-clash`，生成 Clash / Mihomo 配置；
  公网部署用 `FXPROXY_SUB_SERVER` / `FXPROXY_SUB_HTTP_PORT` / `FXPROXY_SUB_SOCKS_PORT` 覆盖。
  `?provider=1` 只返回 `proxies:` 段，可直接挂进 mihomo 的 `proxy-providers`，
  不会覆盖客户端自己的 `port` / `mode` / `rules`；面板上两个地址分开给、分开复制。
- **节点延迟探测**：`GET /api/nodes` 返回全部节点（中文国名 + 洲域 + 主机 + 端口），
  `?probe=1` 用 `ThreadPoolExecutor(max_workers=16)` + 4s TCP connect 并发探测，附 `latency_ms` / `ok`。
- **节点固定与测速**：per-node pinning（面板里锁死某个节点）+ 速度测试；账号表增加邮箱列。
- **任意国家模式显示实际出口**：`country=any` 时回报真实落地国家，不再显示成 `ANY`。
- **VLESS / WebSocket 桥**：`bridge/`（`config.example.json` + README），把本地 SOCKS5 转成
  VLESS over WS，供只支持该协议的客户端接入。
- **面板节点卡片**：「② 运行状态」下方，加载即拉列表、点按钮才探测；四档着色
  <100ms 绿 / <250ms 黄 / ≥250ms 橙 / 超时红，`max-height:320px` 滚动。
- **部署**：`Dockerfile` + `docker-compose.yml` + `deploy/container_entry.py`（双进程托管）+
  `deploy/oracle-free-bootstrap.sh`（Oracle Always Free 一键）+ systemd 模板。
  确认 GitHub Codespaces / Actions 不适合做代理出口（L7 端口转发过不了 CONNECT/SOCKS5 裸 TCP）。
- **Windows 一键部署**：`deploy/windows/`（`install.bat` / `start.bat` / `stop.bat` / `status.bat`
  外壳 + `install.ps1` / `ctl.ps1` 主体）。装在 `.venv` 里，pid 与日志落 `.run/`，
  支持 `-Country` / `-CookieFile` / `-HttpPort` / `-IndexUrl` / `-Proxy` / `-NoProxy` /
  `-Autostart` / `-Uninstall`。已实测：装 → 起 → 状态 → 停，面板 `GET /` 返回 200。
- **CLI**：`-V/--version`。

### 修复

- 空账号池导入 cookie 报 `TokenPool needs at least one refresh_token`：先验证全部 token 再建池。
- `api_start` 空池防御：`self.pool is None or not self.pool.tokens`，挡住历史 `refresh_tokens=[]`
  导致的「空池非 None」→ `clients[0]` 裸抛 `IndexError`。
- 全部重复的 cookie 被误报为失败；`--append` 语义错误；缺登录依赖时丢弃已验证 token；原始 traceback 外漏。
- `/clash.yaml` 的 Content-Type charset 问题。
- 面板节点列表为空、额度显示「无限」：改为以 `limited_bandwidth` 为权威，优先显示「已用 / 月度」。
- 插件 `popup.js` 增加 `executeScript({files:["content.js"]})` 兜底注入，
  指纹浏览器或长时间未刷新的标签页不自动跑 content script 时也能取到凭证。
- `-c/--config` 原先只能写在子命令前（`fxproxy -c X run`），写成 `fxproxy run -c X` 会
  `unrecognized arguments`。现在每个子命令也挂了 `-c`（`default=SUPPRESS`，不覆盖顶层值），两个位置都认。
- Windows 部署脚本三处 `-c` 位置全部改成前置；`config.json` 由 here-string 生成，
  修掉之前数组字面量拼接导致 JSON 中间插换行的问题。

### 打包

- `tools/build_zip.py`：按 `fxproxy-chrome-extension/` 顶层目录打 zip 并自校验
  （顶层唯一 / 非平铺 / manifest 在层内 / manifest 引用齐全 / 无垃圾文件）。
  **平铺 zip 会让 VirtualBrowser 解出错位前缀，报 `ENOENT ...\extensions\<name>\content.js`。**
- `tools/build_crx.py`：CRX2（默认）/ CRX3（`--crx3`）生成与签名校验。
  VirtualBrowser 的 CRX 通道只认 CRX3 且对自建签名不稳（`null (reading 'toString')`），
  CRX2 会被拒（`not a crx3 file`）——**这个通道不要用，传 ZIP**。

### 测试

`test_pool`(37) / `test_cookieutil`(8) / `test_webapi`(68) / `test_cli`(24) /
`test_geo`(7) / `test_crx`(14) / `test_zip`(20) / `test_windows_deploy`(44) /
`test_chrome_formats.js`(9)

## [0.2.0]

- 上游 `c4ba254` 基线：本地 HTTP/SOCKS5 代理、FxA 登录、Mozilla `vpn-serverlist` 节点选择、
  面板（选国家 / 启停 / 配额 / 测出口 / cookie 导入）、节点按洲域中文分组、额度按实际消耗显示。

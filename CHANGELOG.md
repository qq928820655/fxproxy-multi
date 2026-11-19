# 变更记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
上游基线：`c4ba254`（ios151/firefox）。

## [0.3.0] — 2026-11-19

### 新增

- **多账号池**：`refresh_tokens` 列表 + `pool_strategy`（`round-robin` / `quota` / `failover`），
  账号级失败冷却与节点级自动切换。CLI `accounts` / `login --append`。
- **凭证迁移**：Chrome MV3 扩展读取 `__fxa_storage.accounts` 的 `sessionToken`，
  支持 `fxs_` Bearer 形式；六种导出格式（json / lines / cookie / netscape / csv / accounts）。
  `fxproxy login --cookie / --cookie-file` 直接吃插件导出内容。
- **订阅**：`GET /clash.yaml` 与 `fxproxy export-clash`，生成 Clash / Mihomo 配置；
  公网部署用 `FXPROXY_SUB_SERVER` / `FXPROXY_SUB_HTTP_PORT` / `FXPROXY_SUB_SOCKS_PORT` 覆盖。
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

### 打包

- `tools/build_zip.py`：按 `fxproxy-chrome-extension/` 顶层目录打 zip 并自校验
  （顶层唯一 / 非平铺 / manifest 在层内 / manifest 引用齐全 / 无垃圾文件）。
  **平铺 zip 会让 VirtualBrowser 解出错位前缀，报 `ENOENT ...\extensions\<name>\content.js`。**
- `tools/build_crx.py`：CRX2（默认）/ CRX3（`--crx3`）生成与签名校验。
  VirtualBrowser 的 CRX 通道只认 CRX3 且对自建签名不稳（`null (reading 'toString')`），
  CRX2 会被拒（`not a crx3 file`）——**这个通道不要用，传 ZIP**。

### 测试

`test_pool`(37) / `test_cookieutil`(8) / `test_webapi`(55) / `test_cli`(16) /
`test_geo`(7) / `test_crx`(14) / `test_zip`(20) / `test_chrome_formats.js`(9)

## [0.2.0]

- 上游 `c4ba254` 基线：本地 HTTP/SOCKS5 代理、FxA 登录、Mozilla `vpn-serverlist` 节点选择、
  面板（选国家 / 启停 / 配额 / 测出口 / cookie 导入）、节点按洲域中文分组、额度按实际消耗显示。

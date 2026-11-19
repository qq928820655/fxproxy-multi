# 变更记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
上游基线：`c4ba254`（ios151/firefox）。

## [0.4.3] — 2026-11-19

### 新增（合并上游）

- **订阅响应带 `subscription-userinfo` 头**：Clash / FlClash 导入订阅后能直接看到
  已用流量与总额度，不再显示 0。
- **本地计量真实流量**：Firefox 的 `x-quota` 对免费账号恒返回满额，面板按转发字节
  自行累计；Firefox 一旦报告真实用量（`used>0`）则以它为准，不叠加本地计数。
- **可配置的出口强制 IPv4**：账号池卡片里加勾选框，域名先在本机解析成 IPv4 再连目标，
  避免落地出口走 IPv6 时被站点判为异常。

### 改进

- **添加账号卡片挪到账号池下方**：原来它在账号池上面，导致账号池右上角的「+ 添加账号」
  按钮点了之后表单出现在按钮上面，位置很别扭。现在账号池在上、添加表单在下，
  按钮展开后表单就在按钮下方，展开时自动滚到可见位置。
- **运行管理页显示总额度**：运行状态卡片底部加了「总额度 已用 / 总额 + 百分比 + 进度条」，
  以及「账号 N 个 · 可用 M 个 · 剩余 X」，用超 70% 变黄、超 90% 变红；带「刷新额度」按钮。
  数据复用 `/api/accounts`，不额外发请求。

### 修复

- **移动端账号池排版错乱**：6 列的表格在手机上把整页撑出屏幕（左右能拖动、删除按钮跑出屏外）。
  窄屏（≤600px）下账号列表改成一行一张卡：`#1 邮箱` 一行，`uid / 状态 / 已用 / 操作` 各一行标签+取值，
  进度条占满卡片宽度。节点列表同样改成卡片式，`设为出口 / 测速` 两个按钮并排等宽。
- **移动端订阅地址显示不全**：原来「标签 + 地址 + 复制地址按钮」挤在一行，长 URL 被挤成
  每行三四个字符。现在标签独占一行、地址独占一行、复制按钮只留图标（成功后变绿色对勾 1.2 秒），
  `mihomo provider` 同样处理。

## [0.4.2] — 2026-11-19

### 新增

- **面板改成三 tab**（原来是长长一片，滚半天找不到东西）：
  - **运行管理**：运行状态（启停 / 出口国家 / 出口 IP）+ 节点列表
  - **账户设置**：添加账号 + 账号池（策略、配额、删除）
  - **系统设置**：端口 + 「在浏览器/系统里把代理设为」+ 订阅/导出 + 版本与更新
  - 切换只改 `hidden`，不重建 DOM，轮询和输入框内容都不受影响；当前 tab 记在 URL 的
    `#run / #acct / #sys` 上，刷新不会跳回第一页
- **`curl` 一行更新**（`deploy/linux/update.sh`）：
  `curl -fsSL …/deploy/linux/update.sh | bash`，支持 `--check`、`--proxy`、`--repo`。
  它只是取壳，找到 `ctl.env` + `fxproxy-ctl` 后把活交出去；`bootstrap.sh --update` 也直达升级。
- **包内顶层目录固定 `fxproxy`**：版本只体现在压缩包名（`fxproxy-0.4.2-linux.tar.gz`）上，
  升级就是解压覆盖同名目录，不再堆出 `fxproxy-0.4.1` / `fxproxy-0.4.2` 一串并列目录。

### 修复

- **发行包部署的 `fxproxy-ctl update` 是空转**：以前只打印「重跑 install.sh 覆盖安装即可」。
  现在真的更新：查 Release → 下载 tar.gz → 校验结构（缺 `install.sh` 就拒）→ 备份代码到
  `.run/backup-<版本>-<时间>.tar.gz` → `--strip-components=1` 覆盖 → `pip install -e` → 重启。
  `config.json` / `.venv` / `.run` 不动；`--check` 只看不升；同版本不重复下载。

## [0.4.1] — 2026-11-19

### 修复

- **面板加不了第二个账号**：旧逻辑是「已有凭证 → `#loginCard` 永久 `display:none`」，
  于是账号池非空之后「① 添加账号」整块消失，界面上没有任何入口能再追加账号。
  现在「③ 账号池」右侧常驻「+ 添加账号」按钮，点开即用、可随时收起；
  账号池为空时仍自动展开。
- **面板卡在「加载中…」**：`/api/accounts`、`/api/nodes`、`/api/servers` 在机器取不到
  外网时会一直挂着，前端没设超时，界面就永远停在「加载中…」（看起来像面板坏了）。
  列表请求 25–30 秒、探测请求 150 秒超时，超时后显示「加载失败」而不是无限等。
- **Windows 上 `curl -fsSL … | bash` 报「找不到与参数名称 fsSL 匹配的参数」**：
  PowerShell 里裸 `curl` 是 `Invoke-WebRequest` 的别名，没有 `-fsSL`；而且
  `deploy/linux/bootstrap.sh` 是 Linux 安装逻辑。新增 `deploy/windows/bootstrap.ps1`
  与 `bootstrap.bat` 作为 Windows 版在线一键安装（取 Release zip → 校验结构 →
  覆盖代码但保留 `config.json`/`.venv`/`.run` → 调 `install.ps1`），
  直连失败自动挂 `ghproxy.net` / `gh-proxy.com` 重试。
- **Linux bootstrap 在 Windows 的 bash 里硬跑**：识别 MSYS/MINGW/CYGWIN 后直接打印
  Windows 的正确命令并退出，不再一路装到 `systemctl` 才报错
  （`FXPROXY_ALLOW_WIN_BASH=1` 可强制放行）。

## [0.4.0] — 2026-11-19

### 新增

- **面板一键更新**：`② 运行状态` 底部多了版本行 + 「检查更新 / 一键更新」。
  - 查 GitHub 最新 Release，按当前系统挑制品：Windows 取 `-win.zip`，
    Linux/macOS 取 `-linux.tar.gz`；制品名不对时会列出可用附件而不是干瞪眼
  - 更新过程脱离当前进程跑（先等 HTTP 响应落地 → 停服务 → 备份 → 覆盖 →
    重装依赖 → 重启），面板自己不会被"覆盖自己"卡死
  - 只覆盖代码目录，`config.json`（凭证）、`.venv`、`.run` 一律不动；
    顺手留一份 `backup-<版本>.tar.gz` 可回滚
  - 面板里可填 GitHub 代理（`update_proxy`），国内直连不上时用它
  - 部署方式自适应：git 仓库走 `git fetch + reset --hard`，发行包走 tar 覆盖
- **`fxproxy update` 命令**：无头环境一条命令升级，`--check` 只看不升，
  `-y` 免确认，`--proxy` 指定代理。
- **`curl -fsSL … | bash` 菜单式部署**（`deploy/linux/bootstrap.sh`）：
  - 管道执行时先把脚本体落盘再带 tty 重跑 —— 否则 `read` 读到的是管道尾巴，
    菜单直接跳过
  - 菜单：交互式安装 / 快速安装 / 只下载制品 / 升级 / 查状态 / 卸载
  - 交互项覆盖安装目录、多账号 token、出口国家、三个端口、监听地址、
    订阅公网地址、pip 镜像、是否 systemd 开机自启、是否立即启动
  - 制品自动挑：Release tar.gz → 分支源码包 → git clone，三级回退；
    `FXPROXY_GH_PROXY=https://ghproxy.net/` 走镜像
  - 无参数进菜单，带参数直接装（适合批量）；未知参数立刻报错，不会先下载几十 MB

### 修复

- **`.venv` 有 python 没 pip 时反复重试镜像**（Windows `install.ps1`）：旧逻辑
  只看 `Scripts\python.exe` 存在就复用，撞上被精简或装了一半的 venv 会一路
  `No module named pip`，还提示"换镜像重试"，把真实原因埋掉。现在复用前先用
  `python -m pip --version` 验一遍，缺 pip 先 `ensurepip` 补、补不上就删掉重建；
  新建的 venv 也验，仍没有就给出换完整版 Python 的具体命令。Linux `install.sh`
  同样处理（`python3-venv` 没装全的情况）。
- 制品校验不再用文件大小当质量判据（内容重复度高的包压缩后可能只有几十字节），
  改为只挡空响应，有效性交给归档结构解析（顶层唯一目录 + `pyproject.toml`）。
- 发行包按平台区分 `muxserver`：Linux tar.gz 带编译好的二进制（0755），
  Windows zip 只带 Go 源码 + `go.mod`（ELF 在 Windows 上跑不了，白占 8MB）。

## [0.3.2] — 2026-11-19

### 新增

- **Linux 一键部署**：`deploy/linux/install.sh` 一条命令装完（venv + 依赖 + 配置 +
  systemd 开机自启），覆盖 apt / dnf / yum / pacman / apk / zypper，x86_64 与 aarch64。
  - 自动装 Python 3.10+（低版本给明确结论而不是装出个跑不起来的）
  - 端口被占自动顺延，结果打印出来
  - 有 systemd 装 `fxproxy.service` + `fxproxy-web.service`（分开：面板挂了不拖累代理），
    用户级安装自动 `loginctl enable-linger`；没 systemd 降级 nohup
  - `fxproxy-ctl start|stop|restart|status|logs|update|url|uninstall`，
    `status` 顺带打一次 `/api/state` 显示账号数与出口
  - `--dry-run` 只打印步骤，`--uninstall --purge` 连凭证一起清
- **Linux 发行包**：`tools/build_release.py --target linux` 出 `dist/fxproxy-<ver>-linux.tar.gz`，
  `.sh` 保留 0755（zip 存不住执行位）。
- **Windows 一键启动**：`install.ps1 -Shortcut` 在桌面建「fxproxy 启动」快捷方式，
  双击等价于 `start.bat`（起服务 + 开面板）。

### 修复

- `.gitattributes` 补 `deploy/linux/* text eol=lf`：之前只有 `deploy/*.sh`，
  子目录脚本在 Windows checkout 会变 CRLF，Linux 上 `bash install.sh` 直接
  `bad interpreter`。发行包校验也加了这条检查（zip 与 tar 都查）。

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

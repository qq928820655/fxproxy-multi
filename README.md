# fxproxy — Firefox IP Protection 独立落地代理

> **版本**：`fxproxy 0.3.0` · Chrome 扩展 `1.1.0` · 变更记录见 [CHANGELOG.md](CHANGELOG.md)
> 运行时确认：`fxproxy --version`

把 Firefox 内置的 **IP Protection**（Mozilla 给每个账号每月免费 **50GB** 的代理流量）
做成一个可独立运行的落地代理：本地起 **SOCKS5 + HTTP** 代理，出口走 Mozilla 的
Fastly 节点（29 个国家可选）。

> ⚠️ 说明
> - 这是 **代理/落地节点**，不是翻墙工具。落地节点是 Mozilla 的 Fastly 出口（美/英/德等），
>   **中国大陆连不上、也不用于绕过审查**。典型用途：当一个干净的海外落地出口。
> - 依赖一个 **已登录的 Firefox 账号**。账号首次需要「入组」（enroll）到 IP Protection，
>   本工具的 `login` 会自动完成。
> - 仅供个人学习/自用，遵守 Mozilla 服务条款。

## 原理

```
refresh_token ──/oauth/token──▶ access_token(~24h)
access_token  ──GET /api/v1/fpn/token──▶ proxy pass JWT(~10min)
本地 SOCKS5/HTTP ──TLS──▶ <node>.m1.fastly-masque.net:2499
                          └─ CONNECT <target> + Proxy-Authorization: Bearer <JWT>
```

- **凭证链**：持久凭证是 apps/vpn 作用域的 `refresh_token`。运行期只用它换 access_token，
  再换短时效（约 10 分钟）的 proxy pass JWT，全程纯 HTTPS，**机房 IP 无墙、无需再登录**。
- **落地**：proxy pass JWT 作为 `Proxy-Authorization: Bearer` 发给 Fastly 节点的
  标准 HTTPS `CONNECT` 代理（端口 2499）。
- **服务器列表**：公开于 Remote Settings 的 `vpn-serverlist`，按国家挑节点。
- proxy pass 会在过期前自动续期。

## 多账号池（本分支改动）

原版配置只有单个 `refresh_token`，一个账号每月 50GB。本分支把账号做成池，
多个账号共用一个本地代理入口，总量叠加（N 个账号 ≈ N×50GB/月）。

配置（`config.json`）：

```json
{
  "refresh_tokens": ["TOKEN_1", "TOKEN_2", "TOKEN_3"],
  "pool_strategy": "round-robin",
  "cooldown_seconds": 300,
  "quota_cache_ttl": 60,
  "country": "",
  "host": "127.0.0.1",
  "http_port": 8080,
  "socks_port": 1080,
  "failover": 3
}
```

- `refresh_tokens`：账号列表。老的 `refresh_token` 单字段仍兼容，load 时自动转成列表第一项；save 时两个字段都写。
- `pool_strategy`：
  - `round-robin`（默认）——每条连接按顺序轮换账号，均匀分摊。
  - `quota`——按剩余流量加权，剩余多的账号被选中概率更高（配额快照 TTL 缓存，不阻塞热路径）。
  - `failover`——固定用第一个账号，只有失败/冷却才切下一个（主备）。
- `cooldown_seconds`：账号鉴权失败（401/403）或取凭证失败后冷却秒数，冷却期内不被选中；全部账号都在冷却时忽略冷却，避免全停。
- `quota_cache_ttl`：配额快照缓存秒数。

命令：

```bash
fxproxy login --email a@example.com --append   # 追加账号到池（不加 --append 则覆盖）
fxproxy accounts                               # 列出每个账号 uid / 配额 / 冷却状态
fxproxy status                                 # 聚合配额
fxproxy token -q                               # 每行输出一个 token，顺序对应账号 #1..#N
fxproxy run                                    # 单入口，池内按策略调度
```

面板（`fxproxy web`）里可以追加账号、删除账号、切换策略、刷新配额、看每个账号的用量条和总剩余。

两级故障切换：每条连接先由池按策略排出账号顺序，每个账号用自己的 proxy pass 试 `failover` 个节点。
节点网络失败 → 换节点；账号鉴权失败（401/403）→ 冷却该账号并换下一个账号。

环境变量（免改文件，适合服务器/容器）：

```bash
FXPROXY_REFRESH_TOKENS="token1,token2,token3"   # 批量（逗号/换行/分号分隔）
FXPROXY_REFRESH_TOKEN="token1"                  # 单个，优先级最高，置顶
```

### Chrome 插件一键提取（住宅 IP 登录）

Mozilla 对 `/account/login` 和 `/account/create` 按机房 IP 风控（406），首次登录必须在住宅 IP 做。
`chrome-extension/` 里有一个 Manifest V3 插件，在你**已登录**的 Chrome 里一键取出 FxA 凭证，
复制到剪贴板，粘到面板即可。全程不发外部请求——只读页面自己的 localStorage/cookie。

**A. ZIP —— 唯一能稳定装上的路径（推荐）**

上传通道的行为是从已成功安装的参考包（`mc-cookie-grabber-v1.3.5.zip`）反推出来的：
它按 **zip 内唯一顶层目录名** 建扩展目录，解压到 `extensions/<顶层目录名>/`，
再按 manifest 去那一层找文件。

```
zip 里:  fxproxy-chrome-extension/manifest.json
磁盘上:  ...\VirtualBrowser\User Data\extensions\fxproxy-chrome-extension\manifest.json
```

所以包里**必须有一层目录**。平铺包（`manifest.json` 直接在 zip 根）会让它解出错误的前缀，
接着按 manifest 的 `content_scripts` 到根上去找 `content.js`，就报：

```
ENOENT: no such file or directory, open
'...\User Data\extensions\fxproxy-chrome-extension\content.js'
```

1. 「插件管理 → 安装插件」，来源选本地文件，上传仓库根目录的 `fxproxy-chrome-extension.zip`
2. 包内结构：`fxproxy-chrome-extension/` 这一层里放 `manifest.json`、`content.js`、
   `formats.js`、`popup.html`、`popup.js`、`INSTALL.txt`
3. 装好后点「配置使用环境」，勾选要用它的环境

重新打包 / 自校验（`tools/build_zip.py` 已带结构断言，平铺包会被判失败）：

```bash
python tools/build_zip.py                          # 生成
python tools/build_zip.py --verify fxproxy-chrome-extension.zip
python tests/test_zip.py                           # 结构回归测试
```

**B. CRX —— 不要用**

VirtualBrowser 的「本地CRX文件」通道只认 **CRX3**：CRX2 被拒并报 `not a crx3 file`；
它的 CRX3 解析器对非其商店签发的包又不稳定，自建签名 CRX3 实测报
`Cannot read properties of null (reading 'toString')`。这个通道对自建扩展不可靠，
遇到上面任一报错直接改用 A 的 ZIP。

仓库里的 `fxproxy-chrome-extension.crx` 只留作格式参考（`python tools/build_crx.py` 生成，`--crx3` 出 CRX3）。

**C. 普通 Chrome**：`chrome://extensions/` → 开「开发者模式」→「加载已解压的扩展程序」→
选仓库里的 `chrome-extension/` 目录（或 A 里解压出的 `fxproxy-chrome-extension/` 目录）。

> 打包要点：`manifest.json`、`content.js`、`formats.js`、`popup.html`、`popup.js` 必须在包根，
> 不能套子文件夹——否则指纹浏览器解压后找不到 `content.js`。

用法：

1. 住宅 IP 的 Chrome 打开 `https://accounts.firefox.com/` 并登录（多个账号都登一遍也行）
2. 点插件图标 → 「提取并复制凭证」
3. 到 fxproxy 面板的「添加账号」，方式选 **Cookie / 插件导出**，粘贴 → 添加

抓的是 `__fxa_storage.accounts` 里每个账号的 `sessionToken`（新版 `fxs_` Bearer token 会自动解码成 hex），
再做一次 localStorage/sessionStorage/cookie 全量扫描兜底 key 改名。

**六种导出格式**（插件弹窗的下拉框切换，选完点「提取并复制」或「下载为文件」；切换格式会实时重渲染，不用重新提取）：

| 格式 | 内容 | 用途 |
|------|------|------|
| `json`（默认） | 含 uid/email/sessionToken 的结构化 JSON | 面板粘贴，便于核对账号 |
| `lines` | 每行一个 sessionToken | 面板粘贴 / 文本处理 |
| `cookie` | `sessionToken=<hex>; ...` Cookie 头 | 面板 Cookie 框直接粘 |
| `netscape` | Netscape cookies.txt（tab 分隔） | 浏览器/curl 通用格式 |
| `csv` | 逗号分隔的一行 token | 脚本 / 环境变量 |
| `accounts` | `邮箱 token` 每行一个 | 人工核对哪个 token 属于谁 |

六种都能被面板的 cookie 模式直接导入，多账号一次入池（重复自动跳过）。JSON 示例：

```json
{
  "version": 1,
  "source": "fxproxy-chrome-extension",
  "accounts": [{"uid": "...", "email": "a@example.com", "session_token": "<64hex>"}],
  "session_tokens": ["<64hex>"]
}
```

面板的 cookie 模式另外也吃纯 `refresh_token`（hex，直接验证入池不走 bootstrap）。

命令行等价：

```bash
fxproxy login --cookie '{"session_tokens":["<64hex>"]}'   # 直接贴
fxproxy login --cookie-file exported.json                 # 插件「下载为文件」的产物，六种格式都能读
```

拿到的是 `refresh_token`，它不受 IP 风控，可以搬到机房服务器用——住宅 IP 只需做这一次提取。

## 安装

```bash
pip install -e .          # 或 pip install -r requirements.txt
```

Python ≥ 3.10。

## 制品包（不想 clone 就下这些）

| 文件 | 用途 |
|---|---|
| `dist/fxproxy-<版本>-win.zip` | Windows 全量包。解压 → 双击 `deploy\windows\install.bat` |
| `dist/fxproxy-<版本>-linux.tar.gz` | Linux 全量包。解压 → `sudo ./deploy/linux/install.sh` |
| `deploy/linux/bootstrap.sh` | 菜单式部署脚本本体，`curl \| bash` 用的就是它 |
| `fxproxy-chrome-extension.zip` | 浏览器扩展，Chromium 内核「加载已解压的扩展程序」选解压目录 |
| `fxproxy-chrome-extension.crx` | 同上的 CRX3 包（部分指纹浏览器只吃 CRX，装不上就用 zip） |

自己重打：

```bash
python tools/build_zip.py                     # 扩展 zip（顶层唯一目录，--verify 校验）
python tools/build_crx.py                     # 扩展 CRX3，复用 tools/.crx-key.pem 固定 crx_id
python tools/build_release.py                 # 两个平台的包都打
python tools/build_release.py --target linux  # 只打 Linux tar.gz（.sh 保留 0755）
python tools/build_release.py --check         # 只校验已有包结构
```

发行包里刻意不含：`.git` / `.venv` / 真实 `config.json`（含凭证）/ `tools/.crx-key.pem`（签名私钥）。

还有 GitHub Releases 里挂同样的文件，直接从网页下载。

## 快速开始

### Windows —— 一键部署（推荐）

双击 `deploy\windows\install.bat`。它建 venv、装依赖、写 `config.json`、起代理和面板，
完事自动打开 `http://127.0.0.1:8765`。不需要管理员权限，全部写在项目目录内。

```bat
install.bat -Country US -Autostart              :: 指定国家 + 开机自启
install.bat -Country US -Shortcut               :: 加一个桌面「fxproxy 启动」快捷方式
install.bat -CookieFile D:\export.json          :: 直接导入插件导出文件
install.bat -NoStart                            :: 只装不起
install.bat -Uninstall                          :: 停进程 + 删 venv/.run/计划任务
```

起停与状态：

```bat
deploy\windows\start.bat     :: 启动并打开面板
deploy\windows\stop.bat      :: 全部停掉
deploy\windows\status.bat    :: 进程 / 端口 / 额度 / 日志末尾
```

三个脚本都能加 `-q` 结尾，跳过「请按任意键继续」，方便放进别的批处理里串起来。

端口、凭证导入、日志位置、故障对照表见 [deploy/windows/README.md](deploy/windows/README.md)。

### Windows —— 手动安装（不想用脚本时）

```powershell
# 1) 克隆并进目录
git clone https://github.com/qq928820655/fxproxy-multi.git
cd fxproxy-multi

# 2) 安装（含依赖 aiohttp）
pip install -e .          # 若提示 pip 不是命令：python -m pip install -e .

# 3) 打开可视化面板（自动开浏览器 http://127.0.0.1:8765）
fxproxy web               # 或 python -m fxproxy web
```

**首次没有凭证**：面板顶部会有「登录 / 入组」表单——填 Firefox 邮箱+密码（或 sessionToken）
点「登录并保存」即可，凭证会自动写进 `config.json`，以后不用再登录。

> 已经有 `refresh_token`（比如别人给你）？也可以手动写配置跳过登录：
> ```powershell
> $dir = "$HOME\.config\fxproxy"
> New-Item -ItemType Directory -Force -Path $dir | Out-Null
> @'
> { "refresh_token": "PASTE_YOUR_REFRESH_TOKEN", "country": "US",
>   "host": "127.0.0.1", "http_port": 8080, "socks_port": 1080, "failover": 3 }
> '@ | Set-Content -Encoding utf8 "$dir\config.json"
> ```

### Linux —— 一键部署（推荐）

最省事的一种，连包都不用自己下（脚本会挑当前平台该用的制品）：

```bash
curl -fsSL https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/linux/bootstrap.sh | bash
```

进去是个菜单：交互式安装 / 快速安装 / 只下载制品 / 升级 / 查状态 / 卸载。
交互项包括安装目录、多个 refresh_token、出口国家、HTTP / SOCKS5 / 面板三个端口、
监听地址、订阅里用的公网地址、pip 镜像、是否注册 systemd 开机自启、是否装完就启动。

不想交互就带参数（适合批量）：

```bash
# 三台机器同一套配置
curl -fsSL <上面的地址> | bash -s -- --yes --country US --token A,B,C --host 0.0.0.0

# 内网慢，走镜像
curl -fsSL <地址> | FXPROXY_GH_PROXY=https://ghproxy.net/ bash

bash bootstrap.sh --help                      # 全部参数
```

它自己会按 Release tar.gz → 分支源码包 → git clone 的顺序取制品，都不用你管。

已经下好包的话：

```bash
tar xzf fxproxy-0.4.2-linux.tar.gz && cd fxproxy
sudo ./deploy/linux/install.sh --token 你的refresh_token --country US
```

包内顶层目录固定叫 `fxproxy`（版本只在压缩包名上），所以升级就是解压覆盖同名目录。

一条命令做完：装系统包与 Python 依赖 → 建 venv → 写 `config.json`（600）→ 装两个 systemd
单元（`fxproxy` + `fxproxy-web`，开机自启）→ 启动并打印面板地址。端口被占自动顺延；
没有 systemd（容器 / WSL1）降级 nohup 后台。

```bash
sudo ./deploy/linux/install.sh --token T1,T2 --country US         # 机房落地
sudo ./deploy/linux/install.sh --cookie-file ./export.json        # 插件导出文件
sudo ./deploy/linux/install.sh --host 0.0.0.0 --web-host 0.0.0.0 \
     --sub-server 203.0.113.9                                     # 对外提供服务
./deploy/linux/install.sh --user --token T1                       # 普通用户装到 ~/.local
./deploy/linux/install.sh --dry-run                               # 先看会做什么
sudo ./deploy/linux/install.sh --uninstall [--purge]              # 卸载
```

装完统一用 `fxproxy-ctl` 管：

```bash
fxproxy-ctl status      # 进程 / 端口 / 账号数 / 当前出口（顺带打一次 /api/state）
fxproxy-ctl start | stop | restart | logs | update | url
```

支持 Debian/Ubuntu（apt）、RHEL/Rocky/Alma/Fedora（dnf/yum）、Arch（pacman）、
Alpine（apk）、openSUSE（zypper），x86_64 与 aarch64 都行；Python 需 ≥ 3.10。
细节与故障排查见 [deploy/linux/README.md](deploy/linux/README.md)。

### 升级（四种方式，任选）

| 场景 | 做法 |
|---|---|
| **一行命令**（curl，和部署同款） | `curl -fsSL <raw>/deploy/linux/update.sh \| bash` |
| 有面板 | `② 运行状态` → 系统设置 → 「检查更新 / 一键更新」 |
| 无头 / SSH | `fxproxy update`（`--check` 只看，`-y` 免确认） |
| 脚本化 | `fxproxy-ctl update`（git 部署走 pull，发行包部署下载制品覆盖） |

一行更新（Linux / macOS，只想看有没有新版加 `--check`）：

```bash
curl -fsSL https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/linux/update.sh | bash
curl -fsSL .../deploy/linux/update.sh | bash -s -- --check
curl -fsSL .../deploy/linux/update.sh | bash -s -- --proxy https://ghproxy.net/
```

它只是取壳：找到 `ctl.env` + `fxproxy-ctl` 后把活交出去，更新逻辑只有一份
（`deploy/linux/fxproxy-ctl.sh` 的 `cmd_update`）——一行命令、面板一键更新、
`fxproxy update` 三条入口走同一条路。发行包部署的更新是：查 Release → 下载 tar.gz
→ 校验结构 → 备份代码到 `.run/backup-<版本>-<时间>.tar.gz` → `--strip-components=1`
盖到安装目录 → 重装依赖 → 重启；`config.json` / `.venv` / `.run` 全程不动。

Windows 没有 curl 管道，重跑 `bootstrap.ps1`（或 `bootstrap.bat`）就是更新：
它覆盖代码目录、保留 `config.json`，再交给 `install.ps1` 复用 `.venv`。

面板更新会先查 GitHub Release，按当前系统取制品（Windows → `-win.zip`，Linux/macOS →
`-linux.tar.gz`），下载校验后生成一个脱离面板进程的升级脚本：等 3 秒让页面收到响应 →
停服务 → 备份代码 → 覆盖 → 重装依赖 → 重启。`config.json`、`.venv`、`.run` 全程不动，
`.run/backup-<版本>.tar.gz` 可回滚，过程日志在 `.run/update.log`。

国内直连 `api.github.com` 常被 reset，面板版本行下面有个「GitHub 代理」输入框，
填 `http://127.0.0.1:7890` 这类地址即可（会存进配置的 `update_proxy`）。
命令行等价物：`fxproxy update --proxy http://127.0.0.1:7890`，或
`export FXPROXY_GH_PROXY=https://ghproxy.net/` 给下载地址套镜像前缀。

### macOS / Linux（手动）

```bash
git clone https://github.com/qq928820655/fxproxy-multi.git && cd fxproxy-multi
pip install -e .
fxproxy login --email you@example.com      # 首次：换取并保存 refresh_token
fxproxy web                                # 打开控制面板
```

面板运行后，把浏览器/系统代理指到 `127.0.0.1:8080`(HTTP) 或 `127.0.0.1:1080`(SOCKS5) 即可。

### 端口怎么改（8080 被占时）

三个端口全都能改，四种方式任选：

1. **面板里改（推荐，运行中也能改）**：面板「② 运行状态 → 端口」填新端口点「应用」。
   运行中的代理会按新端口自动重启，`/clash.yaml` 订阅里的端口同步跟着变。
2. **命令行参数**：`fxproxy run --http-port 18080 --socks-port 11080`；面板端口 `fxproxy web --web-port 18765`。
3. **配置文件**：`config.json` 里的 `http_port` / `socks_port`。
4. **环境变量**（容器 / 无头）：`FXPROXY_HTTP_PORT` / `FXPROXY_SOCKS_PORT` / `FXPROXY_WEB_PORT`，
   对外映射用 `FXPROXY_SUB_HTTP_PORT` / `FXPROXY_SUB_SOCKS_PORT`。

端口被占不会静默失败：启动直接报「端口 8080 已被其它程序占用」并给出改法；
Windows 的 `install.bat` 重跑时若发现旧端口被抢，会自动顺延到下一个空闲端口。

> 容器改了监听端口，`docker-compose.yml` 的 `ports:` 映射要同步改；只改对外端口就动 `FXPROXY_SUB_*`。

查是谁占着 8080：

```powershell
netstat -ano | findstr :8080          # Windows
```
```bash
lsof -iTCP:8080 -sTCP:LISTEN          # macOS / Linux
```


### 订阅地址（Clash / Mihomo）

不出机场式远程节点订阅——fxproxy 本身是本地落地代理，账号轮换和节点切换都在它内部做。
但它会生成一份指向本地监听端口的 Clash/Mihomo 配置，可以当订阅链接导入：

- 面板方式：打开面板，底部那张卡片里有订阅地址 `http://<面板地址>:8765/clash.yaml`，点「复制地址」填进 Clash/Mihomo 的「订阅链接」。
- 命令行方式：不启面板也能导出文件
  ```bash
  fxproxy export-clash                          # 打印到屏幕
  fxproxy export-clash -o fxproxy.yaml          # 写入文件
  fxproxy export-clash --server 192.168.1.50 -o fxproxy.yaml   # 异机导入指定 IP
  ```

订阅内容只有两个节点：`fxproxy-http`（HTTP，`http_port`，默认 8080）和
`fxproxy-socks5`（SOCKS5，`socks_port`，默认 1080），
策略组 `PROXY` 可选节点或 DIRECT，最后一条 `MATCH,PROXY` 全走代理。

#### 接 mihomo 的两种姿势

**姿势一：整份当配置跑**（最省事，但会接管端口）

```bash
curl -o fxproxy.yaml http://127.0.0.1:8765/clash.yaml
mihomo -f fxproxy.yaml
```

这份 YAML 自带 `port: 7890` / `socks-port: 7891` / `mode: rule`，等于让 mihomo 起在
7890/7891。如果你已经有自己的 mihomo 配置，别覆盖——用姿势二。

**姿势二：当 proxy-provider 挂进现有配置**（推荐）

完整配置里那些 `port` / `mode` / `rules` 塞进 provider 会把客户端设置顶掉，所以加 `?provider=1`
只要 `proxies` 段：

```yaml
# 你的 config.yaml
proxy-providers:
  fxproxy:
    type: http
    url: "http://127.0.0.1:8765/clash.yaml?provider=1"
    interval: 3600
    path: ./providers/fxproxy.yaml

proxy-groups:
  - name: PROXY
    type: select
    use: [fxproxy]        # 拉 provider 里的两个节点
    proxies: [DIRECT]

rules:
  - MATCH,PROXY
```

`?provider=1` 返回的文件只有一个顶层键 `proxies:`，可以直接被 `use:` 引用；
也可以 `curl -o providers/fxproxy.yaml "…/clash.yaml?provider=1"` 后改用 `type: file`。

异机/手机导入要注意：订阅里的 server 默认是 `127.0.0.1`，那指向客户端自己，连不上。两种解法：

1. 面板监听 `0.0.0.0`（`fxproxy web --web-host 0.0.0.0`）后用该机器的局域网 IP 访问面板，`/clash.yaml` 会自动把 server 填成访问用的 IP；
2. 配置里写 `sub_server`，或导出时 `--server <可达 IP/域名>` 覆盖。

代理服务本身也要 `--host 0.0.0.0` 才会监听外网口（面板只管订阅生成，不管代理监听）：
```
fxproxy run --host 0.0.0.0
```
对外端口无鉴权，自己加防火墙白名单或反向代理。

导入订阅只是让客户端连上本机端口，**必须先 `fxproxy run` 把代理跑起来**，否则节点不通。

## 使用

### 0. 可视化控制面板（最省事）

```bash
fxproxy web        # 启动后自动打开 http://127.0.0.1:8765
```

**新用户没有凭证时**，面板顶部会显示「登录 / 入组」表单，拿凭证有三种方式：

- **浏览器登录（推荐新用户）**：点「浏览器登录」按钮，工具弹出一个真实浏览器窗口，
  你在里面正常登录（或注册）Firefox 账号——真实浏览器能过 Mozilla 的人机验证/风控；
  登录完成后工具自动抓取凭证、入组并保存。需要先装一次浏览器组件：
  ```bash
  pip install playwright
  python -m playwright install chromium
  ```
- **直接粘贴 refresh_token**：已经有凭证就选这个，贴进去即可，任意网络都能用。
- **邮箱+密码 / sessionToken**：脚本直连登录接口，**常被 Mozilla 风控拦（406/非 JSON）**，不推荐。

> **部署到服务器**：登录/注册接口被 Mozilla 按 IP 封（机房/VPS → 406），服务器上无法直接登录/注册
> （换新邮箱也没用，封的是 IP 不是账号）。正确做法是先在**住宅 IP**（家里宽带/手机）上登录一次拿到
> `refresh_token`，它是**便携**的——换 token / VPN 接口都不封机房 IP。用 `fxproxy token` 打印出来，
> 复制到服务器的 `~/.config/fxproxy/config.json` 即可，服务器端无需再登录：
>
> ```bash
> fxproxy token        # 打印 refresh_token + 服务器写入命令；纯脚本用 `fxproxy token -q`
> ```

保存成功后接着下拉选出口国家、一键**启动/停止**代理、实时看**额度使用**、点「测试出口 IP」验证落地。
（不想自动开浏览器加 `--no-open`，改端口加 `--web-port 9000`。）

面板的出口国家下拉按**洲域分组**、显示中文国名与节点数（亚洲/欧洲/北美洲/南美洲/非洲/大洋洲），
每 60 秒自动刷新一次；列表来自 Mozilla 公开的 `vpn-serverlist`，无需登录就能拉取，登录后立即重新加载。

「② 运行状态」下方是**节点列表**卡片：页面加载即列出全部节点（国家中文 / 主机 / 端口，
不探测），点「测试全部节点延迟」触发 `GET /api/nodes?probe=1`——16 线程并发 TCP connect、
4s 超时，给每个节点附 `latency_ms` 与 `ok`，延迟按 <100ms 绿 / <250ms 黄 / ≥250ms 橙 /
超时红四档着色（34 节点约 5.7s 完成）。无账号时也能列节点和测延迟。

额度列显示「已用 X / 月度总额」：额度数字来自 Guardian 的 `X-Quota-Limit` / `X-Quota-Remaining`
响应头，月度总额取自 status 的 `maxBytes`（每账号 50GiB）。只有 status 明确 `limited_bandwidth=false`
且响应头确认 unlimited 时才显示「不限量」——不会把还没取到用量的 50GB 账号误判成无限。

也可以用交互式命令行菜单——直接运行、不带参数即进入，按数字选指令：

```bash
fxproxy            # 1 登录 / 2 配额 / 3 国家 / 4 起代理 / 5 web 面板 / 6 退出
```

登录时可选「邮箱+密码」或直接「粘贴 sessionToken」。也可用下面的子命令非交互运行。

### 1. 首次引导凭证（`login`）

`login` 把「邮箱+密码」或「sessionToken」换成持久 `refresh_token` 并自动入组，
结果写入 `~/.config/fxproxy/config.json`（0600 权限）。

```bash
# 方式 A：邮箱 + 密码（注意：FxA 登录接口对机房 IP 有风控 406，
#          请在住宅网络/本机跑，或改用方式 B）
fxproxy login --email you@example.com          # 会交互式提示输入密码
fxproxy login --email you@example.com --password 'xxxx' --country US

# 方式 B：从真实 Firefox 里导出的 sessionToken（任何 IP 都能用）
fxproxy login --session-token <hex-sessionToken> --country US
```

> **为什么支持 sessionToken**：Mozilla 对数据中心/VPS 的 IP 封了 `/account/login`（返回 406），
> 但 OAuth 与 `/api/v1/fpn/*` 不封。所以在 VPS 上用方式 B 最稳：在本机 Firefox 登录后，
> 从「浏览器工具箱 → 存储/网络」里取出 sessionToken 交给工具即可。

### 2. 看配额 / 看可用国家

```bash
fxproxy status      # uid、每月配额(50GiB)、剩余流量/已用/重置时间、是否订阅
fxproxy servers     # 列出所有可选国家
```

### 3. 起代理

```bash
fxproxy run --country US            # 默认监听 127.0.0.1:8080(HTTP) / 1080(SOCKS5)
fxproxy run --country GB --http-port 3128 --socks-port 1081
fxproxy run                         # country 留空 = 任意国家随机
fxproxy run --country US --failover 5   # 每条连接最多试 5 个节点
```

**多节点故障切换**：每条连接会在所选国家的节点池里随机挑 `failover` 个节点依次尝试，
某个 Fastly 节点连不上/CONNECT 失败时自动切下一个（默认 3，可用 `--failover` 或配置项调整）。

使用：

```bash
curl -x http://127.0.0.1:8080 https://api.ipify.org        # HTTP(CONNECT)
curl --socks5-hostname 127.0.0.1:1080 https://api.ipify.org # SOCKS5
```

### 4. 在 Telegram 里用（已实测可用）

Telegram 支持 SOCKS5 代理，直接指到本工具即可让 TG 走海外出口。以桌面版为例：
**设置 → 高级 → 连接类型 → 使用自定义代理 → 添加 SOCKS5**，服务器 `127.0.0.1`、端口 `1080`
（不填账号密码），保存启用后显示「已连接」即成功——聊天/图片/文件都会走 Mozilla 的海外 IP。

<img src="docs/img/telegram-socks5.png" alt="Telegram 使用 SOCKS5 127.0.0.1:1080 已连接" width="360">

> - 只走 **TCP**：TG 的**语音/视频通话是 UDP，不走代理**（收发消息、文件正常）。
> - 手机上的 TG 连不到电脑的 `127.0.0.1`，需把工具部署到**服务器**（config 里 `host` 设 `0.0.0.0`、
>   开放端口），手机 TG 再填 `服务器IP:1080`；对外开放建议加防火墙/鉴权。

## 配置文件

`~/.config/fxproxy/config.json`（可用 `-c/--config` 或环境变量 `FXPROXY_CONFIG` 指定路径）：

```json
{
  "refresh_token": "…",
  "country": "US",
  "host": "127.0.0.1",
  "http_port": 8080,
  "socks_port": 1080
}
```

也可用环境变量 `FXPROXY_REFRESH_TOKEN` 覆盖 `refresh_token`（不落盘）。
`config.json` 含真实凭证，已在 `.gitignore`，不会被提交。

## 后台常驻（systemd）

仓库自带模板 `deploy/fxproxy@.service`（模板单元，`%i` = 运行用户）：

```bash
sudo cp deploy/fxproxy@.service /etc/systemd/system/
# 按需编辑 ExecStart 里的 --country、或用 FXPROXY_REFRESH_TOKEN 注入凭证
sudo systemctl daemon-reload
sudo systemctl enable --now fxproxy@$USER    # 开机自启并立即运行
sudo systemctl status fxproxy@$USER
journalctl -u fxproxy@$USER -f               # 看日志
```

凭证可放在该用户的 `~/.config/fxproxy/config.json`，或在 service 里用
`Environment=FXPROXY_REFRESH_TOKEN=…` 注入（不落盘）。

> 若只想临时后台跑：`nohup fxproxy run --country US >/tmp/fxproxy.log 2>&1 &`。

## Linux 无头 / 机房 IP 部署

结论：**运行代理完全支持，首次登录必须在住宅 IP 做。**

运行链路（refresh → access → proxy pass → CONNECT）全程 HTTPS，
不需要浏览器、不需要显示服务器、机房 IP 不被拦。`fxproxy run` 是纯命令行，
无头直接跑。

Mozilla 只对 FxA 登录/注册接口（`/account/login`、`/account/create`）按机房 IP
风控（HTTP 406）。所以凭证要在家里的宽带/手机网络上拿一次，

```bash
# === 在住宅 IP 的机器上（本机）做一次 ===
pip install '.[login]'                       # pyfxa + curl_cffi，只在登录时用
fxproxy login --email you@example.com        # 或面板"浏览器登录"（需 pip install playwright + playwright install chromium）
fxproxy token -q                             # 输出 refresh_token，每行一个，拷走
```

refresh_token 是便携的，换网络不失效。搬到机房：

```bash
# === 机房无头 VPS ===
python3 -m venv .venv && source .venv/bin/activate
pip install .                                # 只需 requests + aiohttp，不装登录库
```

凭证三种注入方式（任选）：

```bash
# 1) 环境变量（推荐，不落盘）—— 单账号 / 多账号
export FXPROXY_REFRESH_TOKEN="TOKEN_1"
export FXPROXY_REFRESH_TOKENS="TOKEN_1,TOKEN_2,TOKEN_3"

# 2) 配置文件（注意权限）
mkdir -p ~/.config/fxproxy
cat > ~/.config/fxproxy/config.json <<'EOF'
{
  "refresh_tokens": ["TOKEN_1", "TOKEN_2"],
  "pool_strategy": "round-robin",
  "cooldown_seconds": 300,
  "country": "US",
  "host": "0.0.0.0",
  "http_port": 8080,
  "socks_port": 1080,
  "failover": 3
}
EOF
chmod 600 ~/.config/fxproxy/config.json

# 3) systemd 里写 Environment=FXPROXY_REFRESH_TOKENS=... （见上一节模板）
```

启动：

```bash
fxproxy accounts                             # 确认账号已加载、配额可见
fxproxy run --country US                     # 前台
# 或面板（无头加 --no-open，不开浏览器；web 服务本身不需要显示器）
fxproxy web --no-open --web-host 0.0.0.0 --web-port 8765
```

开机自启：

```bash
sudo cp deploy/fxproxy@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fxproxy@$USER
journalctl -u fxproxy@$USER -f
```

无头注意事项：

- `fxproxy run` / `accounts` / `status` / `servers` 不 import 登录库，机房只需 `requests` + `aiohttp`。
- `fxproxy web` 在无头下加 `--no-open`，否则 `webbrowser.open()` 会静默失败（不致命）。
- "浏览器登录"按钮在无头机房不可用（无显示服务器 + 机房 IP 风控）。凭证走住宅 IP 拿好后粘贴或环境变量注入。
- 若要对外开放代理端口，`host` 设 `0.0.0.0` 并配防火墙/鉴权——本工具自身不做认证。
- 代理只走 TCP；UDP 不转发。

## 免费容器 / 云主机部署 + 公网订阅

先说结论：**GitHub Codespaces / GitHub Actions 这类"git 免费容器"做不了稳定的代理出口**。原因不是配额，是协议：

- Codespaces 的端口转发是 HTTP L7 反向代理，靠 `Host` 头路由，只吃 HTTP；本项目的代理是 HTTP CONNECT + SOCKS5 裸 TCP，转发层直接拒。
- 空闲 30 分钟自动休眠，最长生命周期 12 小时，转发域名每次重启都变——订阅地址今天有效明天就废。
- Free 账户每月 120 core-hours（2 核机约 60 小时），跑满即停。
- Actions 是短任务容器，跑完销毁，不是常驻服务。

能真正免费落地、且能生成固定订阅地址的有两条路，都要求**有公网 IPv4 + 常驻进程**：

| 路径 | 免费额度 | 公网 IPv4 | 适合 |
|------|----------|-----------|------|
| Oracle Cloud Always Free | ARM 4C/24G 或 AMD 1C/1G，永久免费 | 有 | 长期常驻，最省 |
| 任意 VPS / 容器平台（给 TCP 端口的） | 看平台 | 有 | 已有机器直接用 |

Cloudflare Workers / Render 免费层 / Zeabur 等只有 HTTP 入口或会休眠，同样过不了 CONNECT，不在此列。

### 凭证从哪来

机房 IP 拿不到 Mozilla 的登录凭证（`/account/login` 对机房 IP 返回 406）。流程固定是：

1. **住宅 IP**（家里宽带/手机）上用插件或 `fxproxy login` 取 `refresh_token`
2. token 本身不绑 IP，搬到机房/容器照常用

### 路径 A：Oracle Always Free 一键脚本

在 Oracle 控制台建一台 Ubuntu 24.04 ARM 实例（Always Free），安全组 ingress 放行 `8080/1080/8765`，SSH 上去：

```bash
# 住宅 IP 上先把 token 取好，复制到这里
export FXPROXY_REFRESH_TOKENS="TOKEN1,TOKEN2"

# 需 root 写 /etc + systemd；-E 保留环境变量
sudo -E bash deploy/oracle-free-bootstrap.sh --country US
```

脚本自动：装依赖 → clone 仓库 → 建 venv → 探测公网 IP → 写 `/etc/fxproxy.env`(600) → 注册 `fxproxy` + `fxproxy-web` 两个 systemd unit → 放行系统防火墙 → 打印订阅地址。

完成后输出：

```
订阅地址:  http://<公网IP>:8765/clash.yaml
```

**别忘控制台安全组也要放行那三个端口**，只改系统防火墙外网仍连不上。

```bash
systemctl status fxproxy fxproxy-web     # 状态
journalctl -u fxproxy -f                 # 日志
```

### 路径 B：Docker / docker-compose

有 Docker 的机器上，凭证和公网地址全走环境变量，镜像里不含任何凭证：

```bash
export FXPROXY_REFRESH_TOKENS="TOKEN1,TOKEN2"
export FXPROXY_SUB_SERVER="1.2.3.4"     # 你的公网 IP 或域名

docker compose up -d --build            # 起代理(8080/1080) + 面板(8765)
docker compose logs -f
```

`docker-compose.yml` 里 `ports` 左边是宿主机端口。改左边时，**必须同步改 `FXPROXY_SUB_HTTP_PORT` / `FXPROXY_SUB_SOCKS_PORT`**，否则订阅里的端口指向错误。

### 拿订阅地址

两条路径都一样：

- 面板底部「订阅地址」：`http://<公网IP>:8765/clash.yaml`，填进 Clash/Mihomo 的订阅链接
- 或命令行直接导出：`fxproxy export-clash -o fxproxy.yaml`

关键环境变量（容器内也走这套，无需 config.json）：

```bash
FXPROXY_REFRESH_TOKENS="t1,t2"   # 必填，多 token 逗号分隔
FXPROXY_HOST=0.0.0.0             # 对外监听，默认 127.0.0.1
FXPROXY_SUB_SERVER="1.2.3.4"     # 订阅里的 server：公网 IP/域名
FXPROXY_SUB_HTTP_PORT=8080       # 订阅里的 HTTP 端口（对外映射端口）
FXPROXY_SUB_SOCKS_PORT=1080      # 订阅里的 SOCKS5 端口
FXPROXY_COUNTRY=US               # 出口国家
FXPROXY_POOL_STRATEGY=round-robin
```

不设 `FXPROXY_SUB_*` 时，`/clash.yaml` 用请求 Host 头推导 server（本地/局域网直连可用）；公网部署必须设，否则订阅里的 `127.0.0.1` 指向客户端自己。

订阅内容仍是两个本地节点（HTTP/SOCKS5），账号轮换和节点故障切换都在 fxproxy 内部做，客户端只看到这两个入口。

## 安全提示

- 默认只监听 `127.0.0.1`；若要对外/当公共落地，请自行加认证或防火墙（本工具不做鉴权）。

## 模块

| 文件 | 作用 |
|------|------|
| `fxproxy/guardian.py`    | Guardian API 客户端：refresh→access→proxy pass、配额、服务器列表 |
| `fxproxy/fxa_auth.py`    | 一次性凭证引导：登录/换 token、自助入组（enroll） |
| `fxproxy/proxyserver.py` | 本地 HTTP CONNECT + SOCKS5 服务，上游 TLS+CONNECT 到 Fastly 节点 |
| `fxproxy/config.py`      | 配置读写 |
| `fxproxy/cookieutil.py`  | 解析插件导出 JSON / Cookie 头 / 多个 token，统一提取凭证 |
| `fxproxy/clashconf.py`   | 生成 Clash/Mihomo 订阅 YAML（指向本地 HTTP/SOCKS5 监听） |
| `fxproxy/webui.py`       | 本地可视化控制面板（aiohttp）：选国家/启停/配额/测出口、cookie 导入、节点列表与延迟探测（`/api/nodes`） |
| `fxproxy/cli.py`         | 命令行入口（login / accounts / status / token / servers / run / web） |
| `fxproxy/geo.py`         | 国家代码 → 中文名/洲域映射，节点下拉分组与 CLI servers 用 |
| `Dockerfile` / `docker-compose.yml` | 容器部署：env 注入凭证，起代理 + 面板，公网订阅 |
| `deploy/container_entry.py` | 容器入口：无 supervisord 依赖同时托管代理与面板 |
| `deploy/oracle-free-bootstrap.sh` | Oracle Always Free 一键部署（venv + systemd + 防火墙 + 订阅） |
| `chrome-extension/`      | Chrome MV3 插件：住宅 IP 登录账号页后一键提取 sessionToken，六种格式导出 |
| `chrome-extension/formats.js` | 六种导出格式（JSON/lines/cookie/netscape/csv/accounts）的纯函数实现 |
| `fxproxy-chrome-extension.zip` | unpacked zip，**内含 `fxproxy-chrome-extension/` 顶层目录**（平铺会 ENOENT），指纹浏览器用这个 |
| `fxproxy-chrome-extension.crx` | 签名的 CRX2，仅作格式参考（该通道不可靠） |
| `tools/build_zip.py`       | 从 `chrome-extension/` 打包 zip 并校验结构（顶层目录唯一 / manifest 在其中 / 引用文件齐全） |
| `tools/build_crx.py`       | 从 `chrome-extension/` 生成/校验 CRX2（默认）或 CRX3（`--crx3`），保证根目录平铺 |

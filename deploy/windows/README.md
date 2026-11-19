# Windows 一键部署

双击 `install.bat` 即可。装完自动起代理和面板，浏览器会打开面板页。

## 做了什么

1. 找 Python（>= 3.10）；没有就试 `winget install Python.Python.3.12`
2. 在项目根建 `.venv`
3. `pip install -e .`（代理只要 `requests` + `aiohttp`）
4. 可选：装 `pyfxa` + `curl_cffi`（只有「用 sessionToken 换 refresh_token」才需要，失败不影响用）
5. 写 `config.json`（端口、国家、账号）
6. 起两个后台进程：`fxproxy run`（代理）+ `fxproxy web`（面板），PID 写进 `.run\`

## 参数

```bat
install.bat                                   :: 全交互
install.bat -Country US                       :: 指定出口国家（留空=任意）
install.bat -Country JP -HttpPort 8081 -SocksPort 1081 -WebPort 8766
install.bat -CookieFile D:\export.json        :: 直接吃插件导出文件（不交互问凭证）
install.bat -SkipLogin                        :: 先不导凭证，稍后在面板加
install.bat -NoStart                          :: 只装不起
install.bat -Autostart                        :: 注册登录自启（schtasks，用户级，无需管理员）
install.bat -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple   :: 走国内镜像
install.bat -Proxy http://127.0.0.1:7890          :: pip 走指定代理（系统代理配错时用）
install.bat -NoProxy                              :: 忽略系统代理直连（配合镜像用）
install.bat -Uninstall                        :: 停进程 + 删 venv/.run/计划任务（config.json 保留）
```

**Python 版本**：脚本优先选 3.10–3.13（`curl_cffi` / `pyfxa` 有预编译 wheel），
只有这些都没有时才用 3.14+，并提示登录依赖可能装不上。已有的 `.venv` 版本不符会自动重建。

## 起停

```bat
start.bat      :: 启动并打开面板
stop.bat       :: 全部停掉
status.bat     :: 进程 / 端口 / 额度 / 日志末尾
```

对应的 PowerShell（想接进别的脚本时用）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 start
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 stop
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 status
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 restart
```

## 装完得到什么

| 用途 | 地址 |
|---|---|
| 面板 | `http://127.0.0.1:8765` |
| HTTP 代理 | `127.0.0.1:8080` |
| SOCKS5 | `127.0.0.1:1080` |
| Clash/Mihomo 订阅 | `http://127.0.0.1:8765/clash.yaml` |

订阅给本机客户端用；局域网里别的机器要连，得把 `config.json` 的 `host` 改成 `0.0.0.0`，
并把 `sub_server` 填成本机局域网 IP（否则订阅里是 `127.0.0.1`，客户端连自己）。

## 凭证怎么来

必须先在**住宅 IP** 的浏览器里登录 `accounts.firefox.com`，用 Chrome 扩展
（`fxproxy-chrome-extension.zip`）导出，再让安装脚本或面板导入。机房/代理 IP 上登录会被风控。

导入的三种方式：`install.bat -CookieFile <文件>` / 面板「添加账号」粘贴 / 直接往
`config.json` 的 `refresh_tokens` 里写 refresh_token。

## 出问题看哪里

```bat
type .run\proxy.err.log
type .run\web.err.log
type .run\proxy-YYYYMMDD.log
```

| 现象 | 原因 |
|---|---|
| 端口被占 | 换 `-HttpPort` / `-WebPort`，或 `netstat -ano \| findstr :8080` 找占用者 |
| pip 卡着不动 / `Connection interrupted` | 系统代理指向已关闭的端口。用 `-Proxy http://127.0.0.1:<你的端口>` 或 `-NoProxy -IndexUrl <镜像>` |
| `python` 找不到 | 装 Python 时没勾 "Add python.exe to PATH"，重装或勾上再跑 |
| 登录依赖装不上 | Python 3.14 没有 `curl_cffi` wheel；只用 refresh_token 的话不影响，脚本会继续 |
| 面板打得开但没节点 | 节点列表来自 Mozilla 公开 serverlist，需要能访问外网 |
| 启动即退出 | 看 `.run\proxy.err.log`，多半是 `config.json` 里没有可用 token |

## 注意

- 脚本不需要管理员权限，全部写项目目录内（`.venv` / `.run` / `config.json`）。
- `config.json` 含 refresh_token，**别提交**（已在 `.gitignore` 里）。
- 面板只监听 `127.0.0.1`，默认不给局域网访问。

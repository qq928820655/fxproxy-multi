# Windows 一键部署

双击 `install.bat` 即可。装完自动起代理和面板，浏览器会打开面板页。

## 在线一行装（手上没有 zip 时）

PowerShell 窗口里粘这一行，等价于 Linux 那条 `curl ... | bash`：

```powershell
irm https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/windows/bootstrap.ps1 -OutFile $env:TEMP\fx-boot.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\fx-boot.ps1
```

带参数往后接（参数和 `install.bat` 一致，另加 `-Dir / -Version / -GhProxy`）：

```powershell
& $env:TEMP\fx-boot.ps1 -Country US -NoStart
& $env:TEMP\fx-boot.ps1 -Dir D:\fxproxy -GhProxy https://ghproxy.net/
```

它做的事：取最新 Release 的 `-win.zip` → 校验结构 → 覆盖到安装目录（`config.json` / `.venv` / `.run` 不动）→ 调 `install.ps1`。
默认装到 `%LOCALAPPDATA%\fxproxy`，直连失败会自动试 `ghproxy.net`、`gh-proxy.com`。
raw 都拿不到时用 `bootstrap.bat`（先下 ps1 再跑，双击也行）。

## 为什么 `curl -fsSL ... | bash` 在 Windows 报错

```
Invoke-WebRequest : 找不到与参数名称"fsSL"匹配的参数。
```

两件事叠在一起：

1. PowerShell 里裸 `curl` 是 `Invoke-WebRequest` 的别名，没有 `-fsSL` 这套参数——真 curl 得写 `curl.exe`，
   别名的参数是 `-Uri / -OutFile`。所以这行在下载之前就挂了，跟网络无关。
2. `deploy/linux/bootstrap.sh` 是 Linux 安装逻辑（systemd、`/opt`、apt/dnf）。就算用 `curl.exe` 把它下下来喂给
   bash，在 Windows 上也装不成；脚本现在会识别 MSYS/MINGW 并直接给出 Windows 的正确命令。

Windows 用上面的 `bootstrap.ps1` 或 `install.bat`。

## 做了什么

1. 找 Python（>= 3.10）；没有就试 `winget install Python.Python.3.12`
2. 在项目根建 `.venv`
3. `pip install -e .`（代理只要 `requests` + `aiohttp`）
4. 可选：装 `pyfxa` + `curl_cffi`（只有「用 sessionToken 换 refresh_token」才需要，失败不影响用）
5. 写 `config.json`（端口、国家、账号）
6. 起两个后台进程：`fxproxy run`（代理）+ `fxproxy web`（面板），PID 写进 `.run\`；
   都用 `pythonw.exe -m fxproxy` 拉起来，**不带控制台窗口**（详见下面「开机自启」）

## 参数

```bat
bootstrap.bat                                 :: 在线装（自动下最新 Release）
bootstrap.bat -Dir D://fxproxy -Country US      :: 指定目录与国家
install.bat                                   :: 全交互
install.bat -Country US                       :: 指定出口国家（留空=任意）
install.bat -Country JP -HttpPort 8081 -SocksPort 1081 -WebPort 8766
install.bat -CookieFile D:\export.json        :: 直接吃插件导出文件（不交互问凭证）
install.bat -SkipLogin                        :: 先不导凭证，稍后在面板加
install.bat -NoStart                          :: 只装不起
install.bat -Autostart                        :: 注册开机/登录自启（静默，无命令行窗口）
install.bat -Shortcut                         :: 桌面建「fxproxy 启动」快捷方式（双击=起服务+开面板）
install.bat -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple   :: 走国内镜像
install.bat -Proxy http://127.0.0.1:7890          :: pip 走指定代理（系统代理配错时用）
install.bat -NoProxy                              :: 忽略系统代理直连（配合镜像用）
install.bat -Uninstall                        :: 停进程 + 删 venv/.run/计划任务（config.json 保留）
```

**Python 版本**：脚本优先选 3.10–3.13（`curl_cffi` / `pyfxa` 有预编译 wheel），
只有这些都没有时才用 3.14+，并提示登录依赖可能装不上。已有的 `.venv` 版本不符会自动重建。

## 起停

装完项目根目录会出现三个双击即用的脚本（`deploy\windows` 下也有一份）：

```bat
一键启动.bat    :: 起代理 + 面板，然后打开面板；跑完按任意键关窗口，服务留在后台
一键停止.bat    :: 两个进程全停；跑完按任意键关窗口
一键状态.bat    :: 常驻刷新（每 3 秒）：进程 / 端口 / 面板地址 / 日志末尾
```

- 启动和停止是**一次性**动作，按任意键就关窗口，**关了不影响已经起来的服务**。
- 状态是**只读**的实时视图，放着不管就一直刷；关窗口或 `Ctrl+C` 都不会影响后台服务
  （它只查进程和端口）。
- 服务本身跑在 `pythonw.exe` 上（GUI 子系统），**不会留下任何命令行窗口**，
  任务栏也看不到黑框；想确认在跑就开「一键状态」。

对应的 PowerShell（想接进别的脚本时用）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 start
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 stop
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 status
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 status -Watch -Interval 3
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\ctl.ps1 restart
```

`status -Deep` 才会联网查账号额度（慢），`-Watch` 模式下默认不查，避免每 3 秒打一次接口。

## 开机自启（无窗口）

`install.bat -Autostart` 会做两件事：

1. 生成 `deploy\windows\silent-start.vbs` —— 用 `WScript.Shell.Run(..., 0, False)`
   拉起 `ctl.ps1 start`，窗口样式 0 = 隐藏，所以登录时不会闪黑框；
2. 先试 `schtasks` 注册计划任务 `fxproxy-autostart`（任务计划程序里能看到、能禁用）；
   被组策略或安全软件拦下时，自动把同一个 vbs 复制进
   `shell:startup`（`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`），效果一样。

取消自启：任务计划程序里删掉 `fxproxy-autostart`，或删掉启动文件夹里的
`fxproxy-autostart.vbs`。

## 装完得到什么

| 用途 | 地址 |
|---|---|
| 面板 | `http://127.0.0.1:8765` |
| HTTP 代理 | `127.0.0.1:8080` |
| SOCKS5 | `127.0.0.1:1080` |
| Clash/Mihomo 订阅 | `http://127.0.0.1:8765/clash.yaml` |

端口都能换：装的时候用 `-HttpPort / -SocksPort / -WebPort`，装完也可以直接在面板
「系统设置 → 端口」里改（运行中会按新端口自动重启，订阅同步跟着变）。
本机这一份装的是**面板 5566 / HTTP 8760 / SOCKS5 1008**，对应的订阅是
`http://127.0.0.1:5566/clash.yaml`。

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
| 端口被占 | 面板「运行状态 → 端口」直接改；或重跑 `install.bat -HttpPort 18080 -SocksPort 11080`；查占用 `netstat -ano \| findstr :8080` |
| pip 卡着不动 / `Connection interrupted` | 系统代理指向已关闭的端口。用 `-Proxy http://127.0.0.1:<你的端口>` 或 `-NoProxy -IndexUrl <镜像>` |
| `python` 找不到 | 装 Python 时没勾 "Add python.exe to PATH"，重装或勾上再跑 |
| 登录依赖装不上 | Python 3.14 没有 `curl_cffi` wheel；只用 refresh_token 的话不影响，脚本会继续 |
| 面板打得开但没节点 | 节点列表来自 Mozilla 公开 serverlist，需要能访问外网 |
| 启动即退出 | 看 `.run\proxy.err.log`，多半是 `config.json` 里没有可用 token |

## 注意

- 脚本不需要管理员权限，全部写项目目录内（`.venv` / `.run` / `config.json`）。
- `config.json` 含 refresh_token，**别提交**（已在 `.gitignore` 里）。
- 面板只监听 `127.0.0.1`，默认不给局域网访问。

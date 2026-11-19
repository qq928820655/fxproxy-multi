# Linux 一键部署

## 一条命令

```bash
# 菜单式：连包都不用下，脚本自己挑当前平台的制品
curl -fsSL https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/linux/bootstrap.sh | bash
```

进去是菜单：

```
  1) 交互式安装        逐项配置端口/凭证/国家
  2) 快速安装          默认端口，凭证稍后在面板加
  3) 只下载制品        存到当前目录，拿去别的机器装
  4) 升级已安装        拉最新版覆盖 + 重启
  5) 查看状态          账号数、端口、出口
  6) 卸载
  0) 退出
```

交互项：安装目录、多个 `refresh_token`、出口国家、HTTP / SOCKS5 / 面板三个端口、
监听地址、订阅公网地址、pip 镜像、是否注册 systemd 开机自启、是否装完就启动。
端口填了但被占用时会提示「安装时自动顺延」，不会直接失败。

不想交互就带参数，参数会原样交给 `install.sh`：

```bash
curl -fsSL <上面的地址> | bash -s -- --yes --country US --token A,B,C
curl -fsSL <地址> | FXPROXY_GH_PROXY=https://ghproxy.net/ bash   # 走镜像
bash bootstrap.sh --help                                        # 全部参数
bash bootstrap.sh --uninstall [--purge]                         # 卸载
```

制品按「Release tar.gz → 分支源码包（codeload）→ git clone」三级回退取，
哪一步通就用哪一步。

> `curl | bash` 时脚本的 stdin 是管道，`read` 会读到管道尾部直接 EOF，菜单
> 会一闪而过。所以 bootstrap 检测到脚本来自 stdin 时，会先把脚本体落到临时
> 文件，再带 `/dev/tty` 重跑一遍。自己 `bash bootstrap.sh` 跑则没这层。

## 从发行包装

```bash
tar xzf fxproxy-<ver>-linux.tar.gz && cd fxproxy-<ver>
sudo ./deploy/linux/install.sh --token 你的refresh_token
```

跑完会打印面板地址和 `fxproxy-ctl` 用法。凭证不给也行，装完在面板里加。

## 支持的系统

| 发行版 | 包管理器 | 备注 |
|---|---|---|
| Debian 12 / Ubuntu 22.04+ | apt | 自带 Python 3.10+ |
| Rocky / Alma / Fedora / RHEL 9 | dnf / yum | 自带 3.9+，脚本会自动补 |
| Arch / Manjaro | pacman | |
| Alpine 3.19+ | apk | 容器场景，装了 gcc/musl-dev 供 wheel 缺失时编译 |
| openSUSE | zypper | |

x86_64 与 aarch64（Oracle A1、树莓派）都适用。Python 需要 ≥ 3.10；Ubuntu 20.04 自带 3.8，
脚本会明确报错让你换系统或用 pyenv，不会装出个跑不起来的版本。

## 常用姿势

```bash
# 机房落地（凭证已在住宅 IP 上取好）
sudo ./deploy/linux/install.sh --token T1,T2 --country US --no-start

# 面板要给别人访问 + 订阅指向公网
sudo ./deploy/linux/install.sh --token T1 --host 0.0.0.0 --web-host 0.0.0.0 \
     --sub-server 203.0.113.9

# 端口冲突（8080 被占自动顺延，结果会打印出来）
sudo ./deploy/linux/install.sh --http-port 18080 --socks-port 11080 --web-port 18765

# 插件导出的文件直接导入
sudo ./deploy/linux/install.sh --cookie-file ./export.json

# 普通用户装到 ~/.local/fxproxy（不需要 root）
./deploy/linux/install.sh --user --token T1

# 只看看会做什么
./deploy/linux/install.sh --dry-run --token T1

# 卸载
sudo ./deploy/linux/install.sh --uninstall          # 保留 config 与凭证
sudo ./deploy/linux/install.sh --uninstall --purge   # 连目录和凭证一起删
```

## 装完得到什么

| 项 | 位置 |
|---|---|
| 安装目录 | `/opt/fxproxy`（root）/ `~/.local/fxproxy`（普通用户） |
| venv | `<安装目录>/.venv` |
| 配置 | `<安装目录>/config.json`（chmod 600） |
| 管理命令 | `/usr/local/bin/fxproxy-ctl`（或 `~/.local/bin/fxproxy-ctl`） |
| ctl 配置 | `/etc/fxproxy/ctl.env`（或 `~/.config/fxproxy/ctl.env`） |
| systemd | `fxproxy.service` + `fxproxy-web.service`，已 enable（开机自启） |

两个单元分开：面板挂了不影响代理，代理重启也不用动面板。

## 管理

```bash
fxproxy-ctl start | stop | restart
fxproxy-ctl status          # 进程 / 端口 / 账号数 / 当前出口
fxproxy-ctl logs 200        # journalctl 或 .run/*.log
fxproxy-ctl update          # git pull + 重装依赖 + 重启
fxproxy-ctl url             # 面板、订阅、provider、HTTP、SOCKS5 地址
fxproxy-ctl uninstall [--purge]
```

`status` 会顺手打一次 `/api/state`，直接看到账号数和是否在跑，不用开浏览器。

## 升级

```bash
fxproxy update               # 查最新 Release，按平台取制品，覆盖后重启
fxproxy update --check       # 只看当前版本和平台，不联网
fxproxy update -y            # 不确认
fxproxy update --proxy http://127.0.0.1:7890   # GitHub 连不上时走代理
```

和面板上的「一键更新」是同一套逻辑：下载 → 校验结构 → 生成脱离进程的升级脚本 →
停服务 → 备份（`.run/backup-<版本>.tar.gz`）→ 覆盖 → `pip install -e` → 重启。
`config.json` / `.venv` / `.run` 不动，日志写在 `.run/update.log`。

git 部署的机器走 `git fetch + reset --hard`，发行包部署的走 tar 覆盖，脚本自己判断。

## 端口

三个端口都能改，四种方式：

1. 面板「② 运行状态 → 端口」改完点应用（运行中自动重启，订阅同步）
2. 安装时 `--http-port / --socks-port / --web-port`
3. `config.json` 里的 `http_port` / `socks_port`
4. 环境变量 `FXPROXY_HTTP_PORT` / `FXPROXY_SOCKS_PORT` / `FXPROXY_WEB_PORT`

装的时候端口被占会自动顺延到下一个空闲端口，并在输出里说明改了哪个。

## 无 systemd 的环境（容器 / WSL1）

脚本检测到没有 systemd 就降级成 nohup 后台，pid 和日志在 `<安装目录>/.run/`。
这种模式重启机器不会自动起来，需要手动 `fxproxy-ctl start`；想让容器里也自动跑，
直接用仓库的 `Dockerfile` + `docker-compose.yml` 更合适。

## 故障排查

| 现象 | 处理 |
|---|---|
| `command not found: fxproxy-ctl` | `~/.local/bin` 不在 PATH，用绝对路径或 `export PATH="$HOME/.local/bin:$PATH"` |
| 面板打不开 | `fxproxy-ctl status` 看端口；`--web-host 0.0.0.0` 才允许外部访问 |
| 代理起来但没网 | 账号配额用完或凭证失效：`fxproxy-ctl status` 看账号数，面板里「刷新配额」 |
| 开机不自启（用户级） | 需要 linger：`loginctl enable-linger $USER`（脚本已尝试开启） |
| 订阅里是 127.0.0.1 | 客户端在别的机器：加 `--sub-server <本机可达IP>` 重装，或改 config.json |
| pip 装不动 | 加 `--index-url https://pypi.tuna.tsinghua.edu.cn/simple`，或 `--proxy http://127.0.0.1:7890` |

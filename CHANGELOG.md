# Changelog

本项目从 0.5.0 起维护本文件。版本号规则：新增功能升次版本号（0.x.0），
修复/调整升修订号（0.x.y）；每次改动都要同步更新本文件。

## [0.5.0] - 2026-11-20

### Added
- 账号导出：下拉 8 项，`refresh_token` / `Cookie` 各配「下载」与「复制」，
  支持勾选多个、全选；导出内容可被对应导入模式直接读回。
- 节点禁用：面板按节点禁用/启用，禁用节点从订阅、slug 路由与本地选择池排除。
- 出口强制 IPv4：`force_ipv4_egress` 配置 + 面板开关 + 环境变量。
- 控制面 Firefox UA 伪装：`runtime_user_agent` 配置 + 面板开关。
- 本地流量计量：按隧道字节累计并持久化；订阅响应带 `subscription-userinfo` 配额头。
- 全部消耗额度的路径纳入本地统计：vless 订阅、mux、HTTP/SOCKS5 端口、旧 `/vless`
  桥、测速、出口 IP 探测。
- 油猴脚本 `fxproxy-userscript/`。
- 部署：启动按端口就绪判定；新增 `tools/sync_ext.py` 扩展同步脚本。

### Changed
- 同账号重新导入按 `uid` 更新而非新增，避免重复账号。
- 订阅恢复聚合组 `fxproxy-auto`（load-balance）与 `fxproxy-fast`（url-test）。
- 登录依赖 `pyfxa`、`curl_cffi` 并入主依赖，标准安装即带导入能力。
- Chrome 扩展导出格式收敛到面板对应的 3 项，不再混入历史残留。
- 面板版本号旁显示当前 git 提交号，便于确认实际运行的代码。

### Fixed
- vless 服务端：`idna` 解码失败、双向泵死锁、零长度写异常。
- 账号状态 401 自愈：丢弃失效的缓存 bearer 后重试一次。
- 开启 Firefox UA 时 `vpn.mozilla.org` 返回 406 导致额度取不到。
- Cookie / sessionToken 批量导入合并解析。

## [0.4.4]

- 变更日志从 0.5.0 起维护，此前版本不再追溯。

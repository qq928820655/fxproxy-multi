# fxproxy 油猴脚本

在 `accounts.firefox.com` 页面右下角点 **FX**，提取登录态的 sessionToken，导出成面板能吃的格式，或者直接推送到本地面板。

## 安装

1. 装一个用户脚本管理器：Chrome/Edge 用 [Tampermonkey](https://www.tampermonkey.net/)，Firefox 用 Tampermonkey 或 Violentmonkey。
2. 管理器里新建脚本 → 把 `fxproxy.user.js` 全文粘进去 → 保存。
   或者把 `fxproxy.user.js` 直接拖进浏览器窗口（管理器会接管）。
3. 打开 `https://accounts.firefox.com/` 并登录，右下角出现紫色 **FX** 圆球。

## 两种用法

**推送（推荐）**：面板在跑，地址填 `http://127.0.0.1:5566`，点「推送到面板」，脚本自己 POST 到 `/api/login`，面板账号池直接多出账号。走的是 `GM_xmlhttpRequest`，不受页面 CSP / CORS 限制。

**复制粘贴**：面板没跑，或者要导到别的机器 —— 选格式点「复制」或「下载」，粘到面板「添加账号 → Cookie / 插件导出」。

## 导出格式

| 格式 | 用途 |
|---|---|
| JSON（含邮箱/uid，推荐） | 面板直接吃，账号列表能显示邮箱 |
| 每行一个 sessionToken | 批量粘贴、脚本处理 |
| Cookie 头（`sessionToken=…`） | 当 cookie 文本导入 |
| Netscape cookies.txt | `curl -b`、其它工具 |
| 逗号分隔 | 环境变量、CI |
| 账号+邮箱 | 人工核对哪个 token 是谁 |

## 和 Chrome 插件的关系

数据源完全一样，都是 `localStorage["__fxa_storage.accounts"]`：

```json
{ "<uid>": { "email": "...", "sessionToken": "<64 hex>" } }
```

FxA 网页端把登录态放在这里，httpOnly 的 sessionToken cookie 只是另一份副本，所以油猴脚本不需要 `chrome.cookies` 那类特权 API 也能拿到全部账号。`fxs_` 开头的 Bearer 形态会被解码回 64 位 hex，面板的 bootstrap 只认 hex。

## 注意

- 脚本只在 `accounts.firefox.com` / `accounts.firefox.com.cn` 注入，不碰别的站。
- 凭证只走本机，脚本没有远端上报。
- 面板那边要先装登录依赖，否则 sessionToken 换不成 refresh_token，会报「缺少登录依赖 curl_cffi / pyfxa」。部署包跑 `install.bat` 会自动装；手工装：

```
"E:\fxproxy\.venv\Scripts\python.exe" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "curl_cffi>=0.7" "pyfxa>=0.8"
```

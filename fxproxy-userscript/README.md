# fxproxy 油猴脚本

在 `accounts.firefox.com` 页面右下角点 **FX**，提取登录态的 sessionToken，导出成面板能吃的格式，或者直接推送到本地面板。

## 安装

1. 装一个用户脚本管理器：Chrome/Edge 用 [Tampermonkey](https://www.tampermonkey.net/)，Firefox 用 Tampermonkey 或 Violentmonkey。
2. 管理器里新建脚本 → 把 `fxproxy.user.js` 全文粘进去 → 保存。
   或者把 `fxproxy.user.js` 直接拖进浏览器窗口（管理器会接管）。
3. 打开 `https://accounts.firefox.com/` 并登录，右下角出现紫色 **FX** 圆球。

## 两种用法

**推送（推荐）**：面板在跑，地址填 `http://127.0.0.1:5566`，选好格式点「直接推送到面板」，脚本 POST 到 `/api/login`，面板账号池直接多出账号。走的是 `GM_xmlhttpRequest`，不受页面 CSP / CORS 限制。邮箱格式推不了（没有 token），选它时按钮会灰掉。

**复制粘贴**：面板没跑，或者要导到别的机器 —— 选格式点「复制给面板」或「下载」，粘到面板「添加账号」，选同一项。

## 导出格式

只有两种，因为插件真正能拿到的只有两样东西：

| 格式 | 面板对应入口 | 说明 |
|---|---|---|
| sessionToken（默认） | Cookie / 插件导出 或 sessionToken | 每行一个，两个入口都认 |
| 邮箱 | 邮箱 + 密码 | 只有邮箱，密码要自己填 |

没有第三种「Cookie 头」：它就是把 sessionToken 前面加 `sessionToken=`，字符和裸 token 完全一样，面板也是拿同一段文本解析，摆两个选项没有意义。

没有 refresh_token：FxA 的 `/api/v1/oauth/token` 要 Hawk 签名，浏览器端用 Bearer 调只会拿到 401，refresh_token 由面板拿 sessionToken 换。

## 为什么一个账号只出一个 token

FxA 会在 localStorage 里留下历次登录的 sessionToken 残留。脚本以前把 `__fxa_storage.accounts`（当前会话）+ 全量扫描（历史残留）叠加，一个账号能导出 4 个 token，但只有第 1 个能用，其余送 Mozilla 全是 `Missing authentication`。

现在两者互斥：accounts 里解析到就只用它，一个都没有才走兜底扫描。

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

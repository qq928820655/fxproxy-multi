"""Local web control panel for fxproxy (multi-account).

Run ``fxproxy web`` to open a dashboard at http://127.0.0.1:8765 where you can
add/remove accounts, switch the balancing strategy, start/stop the proxy,
switch egress country, and watch aggregate quota — no command line needed.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
import webbrowser

from aiohttp import web

from . import config as cfgmod
from .guardian import GuardianClient, NotEnrolledError, TokenPool
from .proxyserver import NodePicker, ProxyServer

# fxa_auth（curl_cffi + pyfxa）只在登录时才用，延迟导入：
# 机房无头环境只做代理调度不该被登录依赖拖累。
def _fxa():
    from . import fxa_auth
    return fxa_auth

PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>fxproxy 控制面板</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, "Segoe UI", "PingFang SC", sans-serif;
         background:#0e1116; color:#e6edf3; }
  .wrap { max-width:760px; margin:0 auto; padding:24px 18px 60px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#8b949e; font-size:13px; margin-bottom:20px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:12px;
          padding:18px; margin-bottom:16px; }
  .card h2 { font-size:14px; margin:0 0 12px; color:#e6edf3; }
  .row { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  label { font-size:13px; color:#8b949e; }
  select, button, input { font-size:14px; border-radius:8px; border:1px solid #30363d;
          padding:9px 12px; background:#0d1117; color:#e6edf3; }
  input { min-width:180px; }
  input::placeholder { color:#6e7681; }
  button { cursor:pointer; border-color:#2ea043; }
  button.primary { background:#238636; border-color:#2ea043; color:#fff; font-weight:600; }
  button.stop { background:#da3633; border-color:#f85149; color:#fff; font-weight:600; }
  button.danger { background:#3d1b1b; border-color:#8b3a3a; color:#ff9b9b; }
  button.sm { padding:5px 10px; font-size:12px; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .pill { display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; }
  .on { background:#238636; color:#fff; }
  .off { background:#484f58; color:#fff; }
  .cool { background:#9e6a03; color:#fff; }
  .kv { display:flex; justify-content:space-between; padding:6px 0;
        border-bottom:1px dashed #21262d; font-size:14px; }
  .kv:last-child { border-bottom:0; }
  .kv b { color:#e6edf3; font-weight:600; }
  .bar { height:8px; background:#21262d; border-radius:999px; overflow:hidden; margin-top:6px; }
  .bar > i { display:block; height:100%; background:#2ea043; }
  code { background:#0d1117; padding:2px 6px; border-radius:6px; font-size:13px; }
  .muted { color:#8b949e; font-size:12px; margin-top:8px; }
  .egress { font-size:15px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 6px; border-bottom:1px solid #21262d; }
  th { color:#8b949e; font-weight:600; font-size:12px; }
  td .bar { margin-top:4px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>fxproxy 控制面板</h1>
  <div class="sub">Firefox IP Protection · 多账号池 · 每账号每月 50GB 落地代理</div>

  <div class="card" id="loginCard" style="display:none;">
    <h2>① 添加账号</h2>
    <div class="row">
      <label>方式</label>
      <select id="loginMode">
        <option value="token">直接粘贴 refresh_token（最稳）</option>
        <option value="cookie">Cookie / 插件导出（推荐）</option>
        <option value="password">邮箱 + 密码</option>
        <option value="session">sessionToken</option>
      </select>
    </div>
    <div id="rtFields" class="row" style="margin-top:10px;">
      <input id="refreshToken" placeholder="粘贴 refresh_token (hex)" style="width:100%;">
    </div>
    <div id="pwFields" class="row" style="margin-top:10px; display:none;">
      <input id="email" type="email" placeholder="FxA 邮箱">
      <input id="password" type="password" placeholder="FxA 密码">
    </div>
    <div id="stFields" class="row" style="margin-top:10px; display:none;">
      <input id="sessionToken" placeholder="粘贴 sessionToken (hex)" style="width:100%;">
    </div>
    <div id="ckFields" style="margin-top:10px; display:none;">
      <textarea id="cookieData" rows="5" style="width:100%;box-sizing:border-box;font-family:ui-monospace,Consolas,monospace;font-size:12px;" placeholder="粘贴 Chrome 插件导出的 JSON，或 sessionToken=xxx Cookie 头，可一次贴多个"></textarea>
      <div class="muted" style="font-size:12px;margin-top:4px;">住宅 IP 登录 → 点插件「提取并复制」→ 粘贴到这里。支持插件 JSON / Cookie 头 / 多个 token，批量自动入池。</div>
    </div>
    <div class="row" style="margin-top:10px;">
      <button id="loginBtn" class="primary">添加账号</button>
      <button id="browserBtn">浏览器登录（推荐新用户）</button>
      <span id="loginMsg" class="muted"></span>
    </div>
    <div class="muted">新账号追加进池；重复账号自动忽略。邮箱密码登录常被 Mozilla 风控拦（406），最省事是粘贴 refresh_token。</div>
  </div>

  <div class="card">
    <h2>② 运行状态</h2>
    <div class="row">
      <span>状态：</span><span id="state" class="pill off">未运行</span>
      <span id="listen" class="muted"></span>
    </div>
    <div class="row" style="margin-top:14px;">
      <label>出口国家</label>
      <select id="country"><option value="">任意</option></select>
      <button id="startBtn" class="primary">启动</button>
      <button id="stopBtn" class="stop" disabled>停止</button>
      <button id="testBtn">测试出口 IP</button>
    </div>
    <div id="nodeWarn" style="display:none;margin-top:8px;color:#f0b400;font-size:13px;"></div>
    <div id="egress" class="egress muted"></div>
  </div>

  <div class="card">
    <h2>③ 账号池 <span id="acctCount" class="muted"></span></h2>
    <div class="row" style="margin-bottom:12px;">
      <label>调度策略</label>
      <select id="strategy">
        <option value="round-robin">轮询（均匀分摊）</option>
        <option value="quota">配额优先（剩余多的优先）</option>
        <option value="failover">主备（失败才切下一个）</option>
      </select>
      <button id="strategyBtn" class="sm">应用策略</button>
      <button id="refreshQuotaBtn" class="sm">刷新配额</button>
    </div>
    <table>
      <thead><tr><th>#</th><th>uid</th><th>状态</th><th>已用 / 月度</th><th></th></tr></thead>
      <tbody id="acctBody"><tr><td colspan="5" class="muted">加载中…</td></tr></tbody>
    </table>
    <div class="kv" style="margin-top:10px;"><span>总剩余</span><b id="totalRemain">—</b></div>
  </div>

  <div class="card">
    <div class="muted">在浏览器/系统里把代理设为：</div>
    <div style="margin-top:6px;">HTTP：<code id="httpAddr">127.0.0.1:8080</code>　
        SOCKS5：<code id="socksAddr">127.0.0.1:1080</code></div>
    <div class="row" style="margin-top:10px;">
      <label>Clash / Mihomo 订阅</label>
      <code id="subUrl" style="flex:1;word-break:break-all;">—</code>
      <button id="copySubBtn" class="sm">复制地址</button>
    </div>
    <div class="muted" style="font-size:12px;margin-top:4px;">
      把上面的 URL 填进 Clash/Mihomo 的「订阅链接」导入。异机导入需让面板监听 0.0.0.0
      并配置 <code>sub_server</code> 为本机可达 IP，否则订阅里的 127.0.0.1 指向客户端自己。
    </div>
    <div class="row" style="margin-top:14px;">
      <button id="copyCfgBtn">复制配置（config.json）</button>
      <span id="copyMsg" class="muted"></span>
    </div>
    <div class="muted" style="margin-top:6px;font-size:12px;">
      含全部 refresh_token，可粘到服务器的 <code>~/.config/fxproxy/config.json</code>（对外用记得把 host 改成 0.0.0.0）。——凭证是私密信息，别外传。
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
function fmt(b){ if(b==null) return "—"; const u=["B","KiB","MiB","GiB","TiB"]; let i=0,v=b;
  while(v>=1024&&i<u.length-1){v/=1024;i++;} return v.toFixed(v<10&&i>0?2:0)+u[i]; }

let NODE_COUNTS = {};
let NODE_LABELS = {};
async function loadServers(){
  try{
    const r = await fetch("/api/servers"); const d = await r.json();
    const sel = $("country");
    const cur = sel.value;
    sel.innerHTML = '<option value="">任意（全球节点）</option>';
    NODE_COUNTS = {}; NODE_LABELS = {};
    const groups = {};
    (d.countries||[]).forEach(c => {
      NODE_COUNTS[c.code] = c.nodes;
      NODE_LABELS[c.code] = c.name;
      (groups[c.continent||"其他"] ||= []).push(c);
    });
    const order = ["亚洲","欧洲","北美洲","南美洲","非洲","大洋洲","特殊","其他"];
    order.filter(k => groups[k]).forEach(cont => {
      const og = document.createElement("optgroup"); og.label = cont;
      groups[cont].forEach(c => {
        const o=document.createElement("option");
        o.value=c.code; o.textContent=c.name+" ("+c.code+") · "+c.nodes+" 节点";
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
    if(cur && $(`country option[value="${cur}"]`)) sel.value = cur;
    if(d.error){ $("nodeWarn").style.display="block";
      $("nodeWarn").textContent="⚠ 节点列表加载失败："+d.error; }
    updateNodeWarn();
  }catch(e){
    $("nodeWarn").style.display="block";
    $("nodeWarn").textContent="⚠ 节点列表加载失败："+e;
  }
}
function updateNodeWarn(){
  const code = $("country").value, n = NODE_COUNTS[code];
  if(code && n === 1){
    $("nodeWarn").style.display = "block";
    const nm = NODE_LABELS[code]||code;
    $("nodeWarn").textContent = `⚠ ${nm} 仅 1 个节点，偶发超时无法自动切换，建议改用美国 / 英国 / 德国。`;
  } else { $("nodeWarn").style.display = "none"; }
}
let AUTHED = false, userPicked = false;
async function loadState(){
  const r = await fetch("/api/state"); const s = await r.json();
  AUTHED = !!s.authed;
  $("loginCard").style.display = AUTHED ? "none" : "block";
  $("state").className = "pill "+(s.running?"on":"off");
  $("state").textContent = s.running ? "运行中" : (AUTHED ? "未运行" : "未登录");
  $("startBtn").disabled = !AUTHED; $("stopBtn").disabled = !s.running;
  $("startBtn").textContent = s.running ? "切换/重启" : "启动";
  $("testBtn").disabled = !AUTHED;
  const shownHost = (s.host==="0.0.0.0" || s.host==="::") ? location.hostname : s.host;
  $("httpAddr").textContent = shownHost+":"+s.http_port;
  $("socksAddr").textContent = shownHost+":"+s.socks_port;
  $("subUrl").textContent = location.origin + "/clash.yaml";
  $("listen").textContent = s.running ? ("出口："+(s.country||"任意")) : "";
  $("strategy").value = s.strategy || "round-robin";
  $("acctCount").textContent = "("+(s.account_count||0)+" 个)";
  if(!userPicked && s.running && s.country){ $("country").value = s.country; updateNodeWarn(); }
}
async function loadAccounts(){
  try{
    const r = await fetch("/api/accounts"); const d = await r.json();
    const rows = (d.accounts||[]).map((a,i)=>{
      let st = a.ok ? '<span class="pill on">可用</span>'
                    : (a.cooling ? '<span class="pill cool">冷却</span>'
                                 : '<span class="pill off">异常</span>');
      // 进度条按"剩余占比"画；只有额度已知才有意义
      const pct = (a.remaining_bytes!=null && a.max_bytes)
        ? Math.max(0,Math.min(100,100*a.remaining_bytes/a.max_bytes)) : 0;
      // 额度列：优先展示 已用 / 月度总额；剩余未知但有总额时显示 已用?/总额
      let rem;
      if(a.unlimited){ rem = "不限量"; }
      else if(a.used_bytes!=null && a.max_bytes){
        rem = "已用 "+fmt(a.used_bytes)+" / "+fmt(a.max_bytes);
      } else if(a.remaining_bytes!=null && a.max_bytes){
        rem = "剩 "+fmt(a.remaining_bytes)+" / "+fmt(a.max_bytes);
      } else if(a.max_bytes){
        rem = "月度 "+fmt(a.max_bytes)+" · 用量待取";
      } else { rem = (a.error||"—").slice(0,32); }
      return `<tr><td>#${i+1}</td><td>${a.uid||"—"}</td><td>${st}</td>
        <td>${rem}<div class="bar"><i style="width:${pct}%"></i></div></td>
        <td><button class="danger sm" onclick="removeAcct(${i})">删除</button></td></tr>`;
    });
    $("acctBody").innerHTML = rows.join("") || '<tr><td colspan="5" class="muted">无账号</td></tr>';
    const remTxt = d.total_remaining_bytes!=null ? "剩 "+fmt(d.total_remaining_bytes) : null;
    $("totalRemain").textContent = d.unlimited ? "不限量"
      : ((d.total_used_bytes!=null && d.total_max_bytes!=null)
         ? "已用 "+fmt(d.total_used_bytes)+" / "+fmt(d.total_max_bytes)
         : (remTxt || (d.total_max_bytes!=null ? "月度 "+fmt(d.total_max_bytes)+" · 用量待取" : "部分未知")));
  }catch(e){ $("acctBody").innerHTML = '<tr><td colspan="5" class="muted">加载失败</td></tr>'; }
}
$("loginMode").onchange = () => {
  const m = $("loginMode").value;
  $("rtFields").style.display = m === "token" ? "flex" : "none";
  $("pwFields").style.display = m === "password" ? "flex" : "none";
  $("stFields").style.display = m === "session" ? "flex" : "none";
  $("ckFields").style.display = m === "cookie" ? "block" : "none";
};
async function doLogin(body, btn){
  btn.disabled = true; $("loginMsg").textContent = "处理中，请稍候…";
  const r = await fetch("/api/login",{method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const d = await r.json(); btn.disabled = false;
  $("loginMsg").textContent = d.error ? ("失败："+d.error)
    : ("成功，现有 "+(d.account_count||1)+" 个账号");
  $("password").value = ""; $("sessionToken").value = ""; $("refreshToken").value = ""; $("cookieData").value = "";
  await loadServers(); await loadState(); await loadAccounts();
}
$("loginBtn").onclick = () => doLogin({ mode:$("loginMode").value,
    email:$("email").value, password:$("password").value,
    session_token:$("sessionToken").value, refresh_token:$("refreshToken").value,
    cookie:$("cookieData").value }, $("loginBtn"));
$("browserBtn").onclick = () => {
  $("loginMsg").textContent = "已请求打开浏览器，请在弹出的窗口里登录（或注册），完成后自动追加 …";
  doLogin({ mode:"browser" }, $("browserBtn"));
};
async function removeAcct(i){
  if(!confirm("删除账号 #"+(i+1)+"？该操作立即生效。")) return;
  const r = await fetch("/api/account/remove",{method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({index:i})});
  const d = await r.json();
  if(d.error) alert("删除失败："+d.error);
  await loadState(); await loadAccounts();
}
$("strategyBtn").onclick = async () => {
  const r = await fetch("/api/strategy",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({strategy:$("strategy").value})});
  const d = await r.json();
  if(d.error) alert("切换失败："+d.error);
  await loadState();
};
$("refreshQuotaBtn").onclick = async () => {
  $("refreshQuotaBtn").disabled = true;
  await fetch("/api/accounts/refresh",{method:"POST"});
  await loadAccounts(); $("refreshQuotaBtn").disabled = false;
};
$("country").onchange = () => { userPicked = true; updateNodeWarn(); };
$("startBtn").onclick = async () => {
  $("startBtn").disabled=true; $("startBtn").textContent="启动中…";
  const r = await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({country:$("country").value})});
  const d = await r.json();
  if(d.error) alert("启动失败："+d.error);
  userPicked = false; await loadState();
};
$("stopBtn").onclick = async () => { await fetch("/api/stop",{method:"POST"}); await loadState(); };
$("copyCfgBtn").onclick = async () => {
  const r = await fetch("/api/config"); const d = await r.json();
  if(d.error){ $("copyMsg").textContent = "失败："+d.error; return; }
  try {
    await navigator.clipboard.writeText(d.config);
    $("copyMsg").textContent = "已复制到剪贴板✓";
  } catch(e) {
    const ta = document.createElement("textarea");
    ta.value = d.config; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); $("copyMsg").textContent = "已复制到剪贴板✓"; }
    catch(_) { $("copyMsg").textContent = "复制失败，请手动选中："+d.config; }
    ta.remove();
  }
};
$("copySubBtn").onclick = async () => {
  const u = $("subUrl").textContent;
  try { await navigator.clipboard.writeText(u); $("copyMsg").textContent = "订阅地址已复制✓"; }
  catch(e) { $("copyMsg").textContent = "订阅地址：" + u; }
};
$("testBtn").onclick = async () => {
  $("egress").textContent="测试中…";
  const r = await fetch("/api/egress"); const d = await r.json();
  $("egress").textContent = d.ip ? ("当前出口 IP："+d.ip) : ("测试失败："+(d.error||"")); };
(async()=>{ await loadServers(); await loadState(); await loadAccounts();
  setInterval(loadAccounts, 15000); setInterval(loadState, 5000);
  setInterval(loadServers, 60000); })();
</script>
</body>
</html>"""


class Panel:
    def __init__(self, config_path=None):
        self.config_path = config_path
        self.cfg = cfgmod.load(config_path)
        self.pool = self._make_pool()
        self.srv = None

    def _make_pool(self) -> TokenPool:
        tokens = self.cfg.get("refresh_tokens") or []
        if not tokens:
            return None
        return TokenPool(
            tokens,
            strategy=self.cfg.get("pool_strategy", "round-robin"),
            cooldown=self.cfg.get("cooldown_seconds", 300),
            quota_cache_ttl=self.cfg.get("quota_cache_ttl", 60),
        )

    def _persist(self):
        """Sync the live pool back into config and save."""
        if self.pool is not None:
            self.cfg["refresh_tokens"] = self.pool.tokens
            self.cfg["pool_strategy"] = self.pool.strategy
        return cfgmod.save(self.cfg, self.config_path)

    # ---- handlers ----
    async def index(self, req):
        return web.Response(text=PAGE, content_type="text/html")

    async def api_state(self, req):
        running = self.srv is not None
        return web.json_response({
            "authed": self.pool is not None and len(self.pool.clients) > 0,
            "running": running,
            "account_count": (len(self.pool.clients) if self.pool else 0),
            "strategy": (self.pool.strategy if self.pool else
                         self.cfg.get("pool_strategy", "round-robin")),
            "country": (self.srv.picker.country if running else self.cfg.get("country") or ""),
            "host": self.cfg.get("host", "127.0.0.1"),
            "http_port": self.cfg.get("http_port", 8080),
            "socks_port": self.cfg.get("socks_port", 1080),
        })

    def _account_rows(self):
        """Build per-account rows from cached quota + live cooldown state."""
        import time as _t
        if self.pool is None:
            return []
        snaps = self.pool.quota_snapshot()
        now = _t.monotonic()
        rows = []
        for i, (c, q) in enumerate(zip(self.pool.clients, snaps)):
            row = dict(q)
            row["cooling"] = self.pool._cool_until.get(i, 0) > now
            rows.append(row)
        return rows

    async def api_accounts(self, req):
        if self.pool is None:
            return web.json_response({"accounts": [], "total_remaining_bytes": None,
                                      "unlimited": False})
        s = self.pool.summary()
        rows = self._account_rows()
        return web.json_response({
            "accounts": rows,
            "total_remaining_bytes": s["total_remaining_bytes"],
            "total_used_bytes": s.get("total_used_bytes"),
            "total_max_bytes": s.get("total_max_bytes"),
            "unlimited": s["unlimited"],
            "strategy": s["strategy"],
        })

    async def api_accounts_refresh(self, req):
        if self.pool is None:
            return web.json_response({"ok": False})
        await asyncio.to_thread(self.pool.refresh_quotas)
        return web.json_response({"ok": True})

    async def api_account_remove(self, req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        try:
            idx = int(body.get("index", -1))
        except (TypeError, ValueError):
            return web.json_response({"error": "index 无效"})
        if self.pool is None:
            return web.json_response({"error": "无账号"})
        try:
            await asyncio.to_thread(self.pool.remove, idx)
            await asyncio.to_thread(self._persist)
            return web.json_response({"ok": True, "account_count": len(self.pool.clients)})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_strategy(self, req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        strategy = body.get("strategy", "")
        if self.pool is None:
            return web.json_response({"error": "无账号"})
        try:
            self.pool.set_strategy(strategy)
            await asyncio.to_thread(self._persist)
            return web.json_response({"ok": True, "strategy": strategy})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_servers(self, req):
        # 服务器列表是 Mozilla 公开接口，不需要账号凭证；无账号时也要能列节点。
        # 用临时 client 拉取，避免依赖 self.pool 存在。
        from . import geo
        try:
            probe = GuardianClient("")
            picker = NodePicker(probe, None)
            await asyncio.to_thread(picker.refresh)
            merged: dict[str, dict] = {}
            for code, meta in picker._by_country.items():
                zh, continent = geo.label(code, meta.get("name"))
                if code not in merged:
                    merged[code] = {"code": code, "name": zh, "name_en": meta.get("name", code),
                                    "continent": continent, "nodes": 0}
                merged[code]["nodes"] += len(meta["nodes"])
            ordered = sorted(merged.items(), key=geo.sort_key)
            return web.json_response({
                "countries": [v for _, v in ordered],
                "error": None,
            })
        except Exception as e:
            return web.json_response({"countries": [], "error": str(e)})

    async def api_start(self, req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        country = (body.get("country") or "").upper() or None
        if self.srv is not None:
            if self.srv.picker.country == country:
                return web.json_response({"ok": True, "already": True})
            try:
                await self.srv.switch_country(country)
                return web.json_response({"ok": True, "switched": True})
            except Exception as e:
                return web.json_response({"error": str(e)})
        if self.pool is None:
            return web.json_response({"error": "无账号，先添加"})
        try:
            picker = NodePicker(self.pool.clients[0], country)
            srv = ProxyServer(
                self.pool, picker,
                host=self.cfg.get("host", "127.0.0.1"),
                http_port=self.cfg.get("http_port", 8080),
                socks_port=self.cfg.get("socks_port", 1080),
                failover=self.cfg.get("failover", 3),
            )
            await srv.start()
            self.srv = srv
            return web.json_response({"ok": True, "account_count": len(self.pool.clients)})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_stop(self, req):
        if self.srv is not None:
            await self.srv.stop()
            self.srv = None
        return web.json_response({"ok": True})

    async def api_login(self, req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        mode = body.get("mode") or "token"
        try:
            if mode == "token":
                rt = (body.get("refresh_token") or "").strip()
                if not rt:
                    return web.json_response({"error": "请粘贴 refresh_token"})
                gc = GuardianClient(rt)
                await asyncio.to_thread(gc.quota)  # 先验证可用
                res = {"refresh_token": rt}
            elif mode == "cookie":
                # 插件导出 / Cookie 头 / 多个 token —— 先全部验证，再一次入池。
                # 关键：首次导入时账号池为空，绝不能提前构造空 TokenPool。
                from . import cookieutil
                raw = body.get("cookie") or ""
                if not str(raw).strip():
                    return web.json_response({"error": "请粘贴凭证文本"})
                try:
                    parsed = cookieutil.parse(raw)
                except Exception as e:
                    return web.json_response({"error": f"凭证格式无法识别：{e}"})

                existing = set(self.pool.tokens) if self.pool is not None else set()
                new_tokens: list[str] = []
                added, skipped, errors = 0, 0, []

                def _verify(rt):
                    # 在 worker 线程内同步验证，不阻塞事件循环
                    GuardianClient(rt).quota()
                    return rt

                async def _take(rt, label):
                    nonlocal added, skipped
                    if rt in existing or rt in new_tokens:
                        skipped += 1
                        return
                    try:
                        await asyncio.to_thread(_verify, rt)
                    except Exception as e:
                        errors.append(f"{label} {str(rt)[:8]}…: {e}")
                        return
                    new_tokens.append(rt)
                    added += 1

                # 直接给的 refresh_token：验证后入池
                for rt in parsed["refresh_tokens"]:
                    await _take(rt, "refresh_token")

                # sessionToken → bootstrap → refresh_token → 入池
                if parsed["session_tokens"]:
                    try:
                        fxa = _fxa()
                    except Exception as e:
                        errors.append(f"登录依赖未安装（需先 pip install '.[login]'）：{e}")
                    else:
                        for st in parsed["session_tokens"]:
                            try:
                                tok = await asyncio.to_thread(
                                    fxa.bootstrap, session_token=st)
                                await _take(tok["refresh_token"], "sessionToken")
                            except Exception as e:
                                errors.append(f"sessionToken {st[:8]}…: {e}")

                if not added and not skipped:
                    msg = "没有成功导入任何账号"
                    if errors:
                        msg += "：" + "; ".join(errors[:3])
                    return web.json_response({"error": msg})

                if self.pool is None:
                    # 全部验证通过后才建池，避免用空 token 列表触发 ValueError
                    self.pool = TokenPool(
                        new_tokens,
                        strategy=self.cfg.get("pool_strategy", "round-robin"),
                        cooldown=self.cfg.get("cooldown_seconds", 300),
                        quota_cache_ttl=self.cfg.get("quota_cache_ttl", 60),
                    )
                else:
                    for rt in new_tokens:
                        try:
                            self.pool.add(rt)
                        except ValueError:
                            pass  # 并发下重复：幂等忽略
                await asyncio.to_thread(self._persist)
                out = {"ok": True, "added": added, "skipped": skipped,
                       "account_count": len(self.pool.clients)}
                if errors:
                    out["warnings"] = errors[:5]
                return web.json_response(out)
            elif mode in ("browser", "session", "password"):
                try:
                    fxa = _fxa()  # 只在这三种登录模式才拉起重型依赖
                except Exception as e:
                    return web.json_response({
                        "error": f"登录依赖未安装（需先 pip install '.[login]'）：{e}"})
                if mode == "browser":
                    st = await asyncio.to_thread(fxa.browser_login)
                    res = await asyncio.to_thread(fxa.bootstrap, session_token=st)
                elif mode == "session":
                    st = (body.get("session_token") or "").strip()
                    if not st:
                        return web.json_response({"error": "请粘贴 sessionToken"})
                    res = await asyncio.to_thread(fxa.bootstrap, session_token=st)
                else:
                    email = (body.get("email") or "").strip()
                    password = body.get("password") or ""
                    if not (email and password):
                        return web.json_response({"error": "请填邮箱和密码"})
                    res = await asyncio.to_thread(
                        fxa.bootstrap, email=email, password=password)
            else:
                return web.json_response({"error": f"未知登录方式: {mode}"})

            rt = res["refresh_token"]
            if self.pool is None:
                self.pool = self._make_pool() or TokenPool(
                    [rt],
                    cooldown=self.cfg.get("cooldown_seconds", 300),
                    quota_cache_ttl=self.cfg.get("quota_cache_ttl", 60),
                )
            try:
                await asyncio.to_thread(self.pool.add, rt)
            except ValueError:
                pass  # 重复账号：幂等，直接忽略
            await asyncio.to_thread(self._persist)
            return web.json_response({"ok": True, "account_count": len(self.pool.clients)})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_clash(self, req):
        """Clash/Mihomo subscription served at /clash.yaml.

        The proxy host defaults to the Host header used to reach this panel
        (LAN IP / VPS domain), so importing from another device just works.
        A configured ``sub_server`` overrides it for split-horizon setups.
        """
        from . import clashconf
        host = req.host.split(":", 1)[0]  # strip panel port
        # 0.0.0.0 is a bind address, not a reachable client address
        if host in ("0.0.0.0", "::", "*"):
            host = "127.0.0.1"
        server = self.cfg.get("sub_server") or host
        text = clashconf.build_clash_yaml(
            server=server,
            http_port=self.cfg.get("sub_http_port") or self.cfg.get("http_port", 8080),
            socks_port=self.cfg.get("sub_socks_port") or self.cfg.get("socks_port", 1080),
        )
        return web.Response(
            text=text,
            content_type="text/yaml",
            charset="utf-8",
            headers={"Content-Disposition": 'attachment; filename="fxproxy.yaml"'},
        )

    async def api_config(self, req):
        if self.pool is None or not self.pool.tokens:
            return web.json_response({"error": "尚未登录/无凭证"})
        cfg = {
            "refresh_tokens": self.pool.tokens,
            "refresh_token": self.pool.tokens[0],   # 兼容老客户端
            "pool_strategy": self.pool.strategy,
            "cooldown_seconds": self.pool.cooldown,
            "quota_cache_ttl": self.pool.quota_cache_ttl,
            "country": (self.srv.picker.country if self.srv is not None
                        else self.cfg.get("country") or "US"),
            "host": self.cfg.get("host", "127.0.0.1"),
            "http_port": self.cfg.get("http_port", 8080),
            "socks_port": self.cfg.get("socks_port", 1080),
            "failover": self.cfg.get("failover", 3),
        }
        return web.json_response({"config": json.dumps(cfg, indent=2, ensure_ascii=False)})

    async def api_egress(self, req):
        if self.srv is None:
            return web.json_response({"error": "代理未运行"})
        host = self.cfg.get("host", "127.0.0.1")
        port = self.cfg.get("http_port", 8080)

        def _probe():
            proxy = f"http://{host}:{port}"
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            return op.open("https://api.ipify.org", timeout=20).read().decode()

        try:
            ip = await asyncio.to_thread(_probe)
            return web.json_response({"ip": ip.strip()})
        except Exception as e:
            return web.json_response({"error": str(e)})


def run_web(config_path=None, web_host="127.0.0.1", web_port=8765, open_browser=True):
    panel = Panel(config_path)
    app = web.Application()
    app.add_routes([
        web.get("/", panel.index),
        web.get("/api/state", panel.api_state),
        web.get("/api/accounts", panel.api_accounts),
        web.post("/api/accounts/refresh", panel.api_accounts_refresh),
        web.post("/api/account/remove", panel.api_account_remove),
        web.post("/api/strategy", panel.api_strategy),
        web.get("/api/servers", panel.api_servers),
        web.post("/api/start", panel.api_start),
        web.post("/api/stop", panel.api_stop),
        web.post("/api/login", panel.api_login),
        web.get("/api/egress", panel.api_egress),
        web.get("/api/config", panel.api_config),
        web.get("/clash.yaml", panel.api_clash),
    ])
    url = f"http://{web_host}:{web_port}"
    print(f"[fxproxy] 控制面板: {url}  (Ctrl+C 退出)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    web.run_app(app, host=web_host, port=web_port, print=None)

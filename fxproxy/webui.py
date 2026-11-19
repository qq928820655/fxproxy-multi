"""Local web control panel for fxproxy (multi-account).

Run ``fxproxy web`` to open a dashboard at http://127.0.0.1:8765 where you can
add/remove accounts, switch the balancing strategy, start/stop the proxy,
switch egress country, and watch aggregate quota — no command line needed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import webbrowser
from pathlib import Path

from aiohttp import web

from . import config as cfgmod
from .guardian import GuardianClient, NotEnrolledError, TokenPool
from .proxyserver import NodePicker, ProxyServer

# fxa_auth（curl_cffi + pyfxa）只在登录时才用，延迟导入：
# 机房无头环境只做代理调度不该被登录依赖拖累。
def _fxa():
    from . import fxa_auth
    return fxa_auth


def _updater():
    from . import updater
    return updater


def _port_free(host: str, port: int) -> bool:
    """端口能不能被 bind。先 connect 探测监听者，再试 bind。

    只 bind 不 connect 会在 Windows 上误判（SO_REUSEADDR 语义不同），
    只 connect 又会漏掉「被非监听 socket 占住」的情况，两步都做。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        if s.connect_ex((host, port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _pick_free_ports(start: int = 18095, span: int = 200) -> tuple[int, int]:
    """给临时测速实例挑一对相邻空闲端口。

    原来写死 8095/8096，撞上用户自己的服务时测速直接失败，还不容易看出原因。
    """
    port = start
    for _ in range(span):
        if _port_free("127.0.0.1", port) and _port_free("127.0.0.1", port + 1):
            return port, port + 1
        port += 2
    raise RuntimeError(f"从 {start} 起找不到一对空闲端口，测速跳过")

PAGE = r"""<!doctype html>
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
  .nodescroll { max-height:320px; overflow:auto; margin-top:10px; }
  .nodescroll thead th { position:sticky; top:0; background:#161b22; z-index:1; }
  td.lat-fast { color:#3fb950; font-weight:600; }
  td.lat-mid  { color:#d29922; font-weight:600; }
  td.lat-slow { color:#e3853c; font-weight:600; }
  td.lat-fail { color:#f85149; font-weight:600; }
  th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
  th.sortable:hover { color:#c9d1d9; }
  th.sortable .arrow { color:#484f58; margin-left:3px; font-size:10px; }
  th.sortable.on .arrow { color:#58a6ff; }
  /* ---- 主 tab ---- */
  .tabs { display:flex; gap:6px; margin:20px 0 0; border-bottom:1px solid #21262d; flex-wrap:wrap; }
  .tab { background:transparent; border:1px solid transparent; border-bottom:none; color:#8b949e;
         padding:9px 18px; border-radius:8px 8px 0 0; cursor:pointer; font-size:14px; line-height:1.2; }
  .tab:hover { color:#c9d1d9; background:#161b22; }
  .tab.on { color:#58a6ff; background:#0d1117; border-color:#21262d; position:relative; top:1px; font-weight:600; }
  /* 面板块隐藏用 !important：#loginCard 自身还有内联 display，不能让它压过 tab 切换 */
  [data-pane][hidden] { display:none !important; }
  @media (max-width:600px) {
    .tab { padding:8px 12px; font-size:13px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>fxproxy 控制面板</h1>
  <div class="sub">Firefox IP Protection · 多账号池 · 每账号每月 50GB 落地代理</div>

  <div class="tabs" role="tablist">
    <button class="tab on" data-tab="run" role="tab">运行管理</button>
    <button class="tab" data-tab="acct" role="tab">账户设置</button>
    <button class="tab" data-tab="sys" role="tab">系统设置</button>
  </div>

  <div class="card" id="loginCard" data-pane="acct" style="display:none;">
    <h2>添加账号 <button id="addClose" class="sm" style="float:right;font-weight:400;">收起</button></h2>
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

  <div class="card" data-pane="run">
    <h2>运行状态</h2>
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

  <div class="card" data-pane="sys">
    <h2>端口</h2>
    <div class="row">
      <label>端口</label>
      HTTP <input id="httpPort" type="number" min="1" max="65535" inputmode="numeric"
                  style="width:96px;min-width:0;padding:6px 8px;">
      SOCKS5 <input id="socksPort" type="number" min="1" max="65535" inputmode="numeric"
                    style="width:96px;min-width:0;padding:6px 8px;">
      <button id="portBtn" class="sm">应用</button>
      <span id="portMsg" class="muted" style="margin-top:0;"></span>
    </div>
    <div class="muted" style="font-size:12px;margin-top:2px;">
      8080 / 1080 常见冲突（Tomcat、Jenkins、各类 dev server）。改完点「应用」，
      运行中的代理会按新端口自动重启；订阅里的端口一并跟着变。
    </div>
    <div style="border-top:1px solid #21262d;margin-top:14px;padding-top:12px;">
      <div class="muted">在浏览器 / 系统里把代理设为：</div>
      <div style="margin-top:6px;">HTTP：<code id="httpAddr">127.0.0.1:8080</code>　
          SOCKS5：<code id="socksAddr">127.0.0.1:1080</code></div>
      <div class="muted" style="font-size:12px;margin-top:4px;">
        两个是同一后端的两个入口，任选其一：浏览器 / 系统代理 / 指纹浏览器填
        <b>HTTP <span id="httpPortTxt">8080</span></b>；
        只认 SOCKS5 的客户端（Telegram、部分爬虫库、<code>curl --socks5</code>）填
        <b>SOCKS5 <span id="socksPortTxt">1080</span></b>。
      </div>
    </div>
  </div>

  <div class="card" data-pane="sys">
    <h2>版本与更新</h2>
    <div>
      <div class="row">
        <label>版本</label>
        <span id="verNow" class="pill off">—</span>
        <span id="verLatest" class="muted" style="margin-top:0;"></span>
        <button id="verCheck" class="sm">检查更新</button>
        <button id="verUpdate" class="primary sm" disabled>一键更新</button>
      </div>
      <div class="row" style="margin-top:8px;">
        <label>GitHub 代理</label>
        <input id="ghProxy" placeholder="http://127.0.0.1:7890（留空直连）"
               style="min-width:0;flex:1;padding:6px 8px;font-size:13px;">
      </div>
      <div id="verMsg" class="muted" style="font-size:12px;margin-top:4px;">
        更新只替换代码目录，<code>config.json</code>、<code>.venv</code>、<code>.run</code> 都不动。
      </div>
      <pre id="verLog" style="display:none;max-height:160px;overflow:auto;background:#0d1117;
           border:1px solid #21262d;border-radius:8px;padding:8px;font-size:11px;line-height:1.5;
           color:#8b949e;margin-top:8px;white-space:pre-wrap;word-break:break-all;"></pre>
    </div>
  </div>

  <div class="card" data-pane="run">
    <h2>节点列表 <span id="nodeCount" class="muted"></span></h2>
    <div class="row">
      <button id="probeBtn" class="sm">测试全部节点延迟</button>
      <button id="egressBtn" class="sm">探测出口 IP</button>
      <span id="probeMsg" class="muted"></span>
    </div>
    <div class="nodescroll">
      <table>
        <thead><tr>
          <th class="sortable" data-key="name" onclick="sortNodes('name')">国家<span class="arrow"></span></th>
          <th class="sortable" data-key="host" onclick="sortNodes('host')">节点主机<span class="arrow"></span></th>
          <th class="sortable" data-key="port" onclick="sortNodes('port')">端口<span class="arrow"></span></th>
          <th class="sortable" data-key="latency" onclick="sortNodes('latency')">延迟<span class="arrow"></span></th>
          <th class="sortable" data-key="bandwidth" onclick="sortNodes('bandwidth')">带宽<span class="arrow"></span></th>
          <th></th>
        </tr></thead>
        <tbody id="nodeBody"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
      </table>
    </div>
    <div class="muted" style="font-size:12px;margin-top:6px;">
      「设为出口」固定走该节点；「测速」经该节点下载 8MB 计带宽（约 3-20 秒）。
    </div>
  </div>

  <div class="card" data-pane="acct">
    <h2>账号池 <span id="acctCount" class="muted"></span></h2>
    <div class="row" style="margin-bottom:12px;">
      <label>调度策略</label>
      <select id="strategy">
        <option value="round-robin">轮询（均匀分摊）</option>
        <option value="quota">配额优先（剩余多的优先）</option>
        <option value="failover">主备（失败才切下一个）</option>
      </select>
      <button id="strategyBtn" class="sm">应用策略</button>
      <button id="refreshQuotaBtn" class="sm">刷新配额</button>
      <button id="addToggle" class="primary sm">+ 添加账号</button>
    </div>
    <table>
      <thead><tr><th>#</th><th>邮箱</th><th>uid</th><th>状态</th><th>已用 / 月度</th><th></th></tr></thead>
      <tbody id="acctBody"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
    </table>
    <div class="kv" style="margin-top:10px;"><span>总剩余</span><b id="totalRemain">—</b></div>
  </div>

  <div class="card" data-pane="sys">
    <h2>订阅与导出</h2>
    <div class="row">
      <label>Clash / Mihomo 订阅</label>
      <code id="subUrl" style="flex:1;word-break:break-all;">—</code>
      <button id="copySubBtn" class="sm">复制地址</button>
    </div>
    <div class="row" style="margin-top:8px;">
      <label>mihomo provider</label>
      <code id="provUrl" style="flex:1;word-break:break-all;">—</code>
      <button id="copyProvBtn" class="sm">复制地址</button>
    </div>
    <div class="muted" style="font-size:12px;margin-top:4px;">
      上面的 URL 填进 Clash/Mihomo 的「订阅链接」导入。异机导入需让面板监听 0.0.0.0
      并配置 <code>sub_server</code> 为本机可达 IP，否则订阅里的 127.0.0.1 指向客户端自己。<br>
      不想让 mihomo 覆盖你自己的 <code>port</code>/<code>rules</code>，就把 provider 那个地址填进
      <code>proxy-providers</code> 的 <code>url</code>，它只返回 <code>proxies</code> 段。
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

// ---------------------------------------------------------------- 主 tab
// 单页拆三块：运行管理 / 账户设置 / 系统设置。DOM 里各卡片用 data-pane 归属，
// 切换只改 hidden —— 元素不销毁，正在跑的轮询和输入内容都不受打扰。
const TAB_NAMES = ["run", "acct", "sys"];
function switchTab(name, silent){
  if(TAB_NAMES.indexOf(name) < 0) name = "run";
  TAB_NAMES.forEach(t => {
    const on = (t === name);
    document.querySelectorAll('[data-pane="' + t + '"]').forEach(el => { el.hidden = !on; });
    const btn = document.querySelector('.tab[data-tab="' + t + '"]');
    if(btn) btn.classList.toggle("on", on);
  });
  // 刷新后停在同一个 tab 上，不用每次手动点回去
  try { history.replaceState(null, "", "#" + name); } catch(e) {}
  if(!silent && name === "acct") loadAccounts();
}
document.querySelectorAll('.tab[data-tab]').forEach(b => {
  b.onclick = () => switchTab(b.getAttribute("data-tab"));
});

let NODE_COUNTS = {};
let NODE_LABELS = {};
async function loadServers(){
  try{
    const r = await timeoutFetch("/api/servers", null, 25000); const d = await r.json();
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
function latClass(n){
  if(n.ok === false || n.latency_ms == null) return "lat-fail";
  if(n.latency_ms < 100) return "lat-fast";
  if(n.latency_ms < 250) return "lat-mid";
  return "lat-slow";
}
function latText(n){
  if(n.ok === false || n.latency_ms == null) return "超时";
  return n.latency_ms + " ms";
}
let NODE_DATA = [], NODE_PROBE = false;
let SORT = {key:null, dir:1};
function _sortVal(n,key){
  if(key==="port") return Number(n.port)||0;
  if(key==="latency") return (n.ok===false||n.latency_ms==null)?0:n.latency_ms;
  if(key==="bandwidth") return (n.mbps==null)?0:n.mbps;
  if(key==="name") return n.name||"";
  if(key==="host") return n.host||"";
  return 0;
}
function _sortUnknown(n,key){
  if(key==="latency") return n.ok===false||n.latency_ms==null;
  if(key==="bandwidth") return n.mbps==null;
  return false;
}
function renderNodeRows(){
  const body = $("nodeBody");
  let arr = NODE_DATA.slice();
  if(SORT.key){
    arr.sort((a,b)=>{
      const ua=_sortUnknown(a,SORT.key), ub=_sortUnknown(b,SORT.key);
      if(ua!==ub) return ua?1:-1;
      const va=_sortVal(a,SORT.key), vb=_sortVal(b,SORT.key);
      let c = (typeof va==="string") ? va.localeCompare(vb,"zh-Hans")
                                     : (va<vb?-1:va>vb?1:0);
      return c*SORT.dir;
    });
  }
  const probe = NODE_PROBE;
  const rows = arr.map(n => {
    const lat = `<td class="${latClass(n)}">${probe?latText(n):"—"}</td>`;
    const mb = n.mbps;
    let bw = `<td id="bw-${n.host}" class="muted">—</td>`;
    if(mb != null){
      bw = mb >= 5
        ? `<td><span class="${mb>=15?'pill on':''}" style="${mb>=15||mb<5?'':'color:#d29922'}" id="bw-${n.host}">${mb} Mbps</span></td>`
        : `<td><span class="pill off" id="bw-${n.host}">${mb} Mbps</span></td>`;
    }
    const cur = (n.host === CURRENT_NODE);
    const ipLine = n.egress_ip ? `<div class="muted" style="font-size:11px;">出口 ${n.egress_ip}</div>` : '';
    return `<tr${cur?' style="background:#123329;"':''}><td>${n.name}${cur?' <span class="pill on">当前</span>':''}</td><td>${n.host}${ipLine}</td><td>${n.port}</td>${lat}${bw}`+
      `<td><button class="sm primary" onclick="useNode('${n.host}')">设为出口</button> `+
      `<button class="sm" id="st-${n.host}" onclick="testNode('${n.host}')">测速</button></td></tr>`;
  });
  body.innerHTML = rows.join("") || '<tr><td colspan="6" class="muted">无节点</td></tr>';
  document.querySelectorAll("th.sortable").forEach(th=>{
    const on = th.dataset.key===SORT.key;
    th.classList.toggle("on", on);
    const a = th.querySelector(".arrow");
    if(a) a.textContent = on ? (SORT.dir>0?"▲":"▼") : "";
  });
}
function sortNodes(key){
  if(SORT.key===key) SORT.dir = -SORT.dir;
  else { SORT.key = key; SORT.dir = 1; }
  renderNodeRows();
}
async function loadNodes(probe, egress){
  const btn = $("probeBtn"), ebtn = $("egressBtn"), body = $("nodeBody");
  if(probe){ btn.disabled = true; btn.textContent = "探测中…";
    $("probeMsg").textContent = "并发 TCP 探测中（16 线程，4s 超时）…"; }
  if(egress){ ebtn.disabled = true; ebtn.textContent = "探测中…";
    $("probeMsg").textContent = "逐节点实测出口 IP（16 并发，约 30-60 秒）…"; }
  const qs = (probe?"?probe=1":"") + (egress?(probe?"&":"?")+"egress=1":"");
  try{
    // 探测类请求本身就要 30-60 秒，超时给宽一点；纯列表 30 秒
    const r = await timeoutFetch("/api/nodes"+qs, null, (probe||egress)?150000:30000);
    const d = await r.json();
    if(d.error){ body.innerHTML = '<tr><td colspan="6" class="muted">加载失败：'+d.error+'</td></tr>'; return; }
    NODE_DATA = d.nodes||[]; NODE_PROBE = !!probe;
    renderNodeRows();
    $("nodeCount").textContent = "("+NODE_DATA.length+" 个)";
    if(probe){
      const ok = NODE_DATA.filter(n=>n.ok).length;
      $("probeMsg").textContent = "可达 "+ok+"/"+NODE_DATA.length;
    }
    if(egress){
      const got = NODE_DATA.filter(n=>n.egress_ip).length;
      $("probeMsg").textContent = "出口 IP 已探测 "+got+"/"+NODE_DATA.length;
    }
  }catch(e){
    body.innerHTML = '<tr><td colspan="6" class="muted">加载失败：'+e+'</td></tr>';
  }finally{
    if(probe){ btn.disabled = false; btn.textContent = "测试全部节点延迟"; }
    if(egress){ ebtn.disabled = false; ebtn.textContent = "探测出口 IP"; }
  }
}
$("probeBtn").onclick = () => loadNodes(true);
$("egressBtn").onclick = () => loadNodes(false, true);
let CURRENT_NODE = "";
async function useNode(host){
  $("probeMsg").textContent = "切换出口到 "+host+" …";
  const r = await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({node:host})});
  const d = await r.json();
  $("probeMsg").textContent = d.error ? ("失败："+d.error) : ("出口已固定到 "+host);
  userPicked = false; await loadState(); await loadNodes(false);
}
async function testNode(host){
  const btn = $("st-"+host); const bw = $("bw-"+host);
  btn.disabled = true; bw.className="muted"; bw.style.color=""; bw.textContent="测速中…";
  const r = await fetch("/api/speedtest",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({node:host})});
  const d = await r.json(); btn.disabled = false;
  if(d.error){ bw.className="pill off"; bw.textContent = "失败"; $("probeMsg").textContent = d.error; }
  else {
    const cls = d.mbps>=15 ? "pill on" : (d.mbps>=5 ? "" : "pill off");
    bw.className = cls; bw.style.color = d.mbps>=15||d.mbps<5 ? "" : "#d29922";
    bw.textContent = d.mbps+" Mbps";
    const row = NODE_DATA.find(n=>n.host===host);
    if(row){ row.mbps = d.mbps; }
  }
}

let AUTHED = false, userPicked = false;
// 添加账号卡片：没账号时自动展开；用户手动开/收过之后就不再自动跟随。
// 旧逻辑写成「有账号 → 永久 display:none」，等于把加第二个账号的入口藏死了。
let ADD_PINNED = false;
function setAddOpen(v, auto){
  $("loginCard").style.display = v ? "block" : "none";
  $("addToggle").textContent = v ? "收起添加框" : "+ 添加账号";
  $("addToggle").className = v ? "sm" : "primary sm";
  if(!auto) ADD_PINNED = true;
}
// 后端在取不到外网时（拿 Mozilla 节点表 / 查配额）会卡住，请求不设超时
// 前端就永远停在「加载中…」，看起来像面板坏了
function timeoutFetch(url, opts, ms){
  const c = new AbortController(), t = setTimeout(()=>c.abort(), ms||15000);
  return fetch(url, Object.assign({}, opts||{}, {signal:c.signal}))
    .finally(()=>clearTimeout(t));
}
async function loadState(){
  const r = await timeoutFetch("/api/state"); const s = await r.json();
  AUTHED = !!s.authed;
  if(!ADD_PINNED) setAddOpen(!AUTHED, true);
  $("state").className = "pill "+(s.running?"on":"off");
  $("state").textContent = s.running ? "运行中" : (AUTHED ? "未运行" : "未登录");
  $("startBtn").disabled = !AUTHED; $("stopBtn").disabled = !s.running;
  $("startBtn").textContent = s.running ? "切换/重启" : "启动";
  $("testBtn").disabled = !AUTHED;
  const shownHost = (s.host==="0.0.0.0" || s.host==="::") ? location.hostname : s.host;
  $("httpAddr").textContent = shownHost+":"+s.http_port;
  $("socksAddr").textContent = shownHost+":"+s.socks_port;
  $("httpPortTxt").textContent = s.http_port;
  $("socksPortTxt").textContent = s.socks_port;
  // 输入框正在编辑时不要被 5s 轮询刷回旧值
  const ae = document.activeElement;
  if (ae !== $("httpPort")) $("httpPort").value = s.http_port;
  if (ae !== $("socksPort")) $("socksPort").value = s.socks_port;
  $("subUrl").textContent = location.origin + "/clash.yaml";
  $("provUrl").textContent = location.origin + "/clash.yaml?provider=1";
  CURRENT_NODE = s.node || "";
  let egress;
  if (s.node) egress = "出口：固定节点 "+s.node+(s.egress_country?(" ("+s.egress_country+")"):"");
  else if (s.egress_country) egress = "出口：任意 · 最近 "+s.egress_country;
  else egress = s.running ? ("出口："+(s.country||"任意")) : "";
  $("listen").textContent = s.running ? egress : "";
  $("strategy").value = s.strategy || "round-robin";
  $("acctCount").textContent = "("+(s.account_count||0)+" 个)";
  if(s.version){ $("verNow").textContent = "v"+s.version; $("verNow").className = "pill on"; }
  if(!userPicked && s.running && s.country){ $("country").value = s.country; updateNodeWarn(); }
}
async function loadAccounts(){
  try{
    const r = await timeoutFetch("/api/accounts", null, 25000); const d = await r.json();
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
      return `<tr><td>#${i+1}</td><td style="font-size:12px;">${a.email||'<span class="muted">—</span>'}</td><td>${a.uid||"—"}</td><td>${st}</td>
        <td>${rem}<div class="bar"><i style="width:${pct}%"></i></div></td>
        <td><button class="danger sm" onclick="removeAcct(${i})">删除</button></td></tr>`;
    });
    $("acctBody").innerHTML = rows.join("") || '<tr><td colspan="6" class="muted">无账号</td></tr>';
    const remTxt = d.total_remaining_bytes!=null ? "剩 "+fmt(d.total_remaining_bytes) : null;
    $("totalRemain").textContent = d.unlimited ? "不限量"
      : ((d.total_used_bytes!=null && d.total_max_bytes!=null)
         ? "已用 "+fmt(d.total_used_bytes)+" / "+fmt(d.total_max_bytes)
         : (remTxt || (d.total_max_bytes!=null ? "月度 "+fmt(d.total_max_bytes)+" · 用量待取" : "部分未知")));
  }catch(e){ $("acctBody").innerHTML = '<tr><td colspan="6" class="muted">加载失败</td></tr>'; }
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
// 账号池右上角的入口：任何时候都能打开添加框，加完再收起
$("addToggle").onclick = () => setAddOpen($("loginCard").style.display === "none");
$("addClose").onclick = () => setAddOpen(false);
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
$("portBtn").onclick = async () => {
  const btn = $("portBtn"), msg = $("portMsg");
  btn.disabled = true; msg.style.color = ""; msg.textContent = "应用中…";
  try{
    const r = await fetch("/api/ports",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({http_port:$("httpPort").value.trim(),
                           socks_port:$("socksPort").value.trim()})});
    const d = await r.json();
    if (d.error){ msg.style.color = "#f85149"; msg.textContent = d.error; }
    else if (d.unchanged){ msg.style.color = ""; msg.textContent = "端口没变"; }
    else {
      msg.style.color = "#3fb950";
      msg.textContent = "已切到 HTTP "+d.http_port+" / SOCKS5 "+d.socks_port+
        (d.restarted ? "，代理已重启" : (d.stopped ? "，代理保持停止" : "")) +
        "；订阅地址已同步";
      await loadState(); loadNodes(false);
    }
  }catch(e){ msg.style.color = "#f85149"; msg.textContent = "请求失败："+e; }
  finally{ btn.disabled = false; }
};
/* ---------- 版本 / 一键更新 ---------- */
let VER = {latest:"", has_update:false, platform:""};
async function verInit(){
  try{
    const r = await fetch("/api/version"); const d = await r.json();
    VER.platform = d.platform_label || d.platform || "";
    if(d.update_proxy) $("ghProxy").value = d.update_proxy;
    $("verNow").textContent = "v"+d.current;
    const kind = (d.install && d.install.kind) || "unknown";
    if(kind === "unknown"){
      $("verMsg").textContent = "当前是 pip 装进 site-packages 的形态，面板没法就地替换代码；用安装脚本或发行包重装一次之后这个按钮才可用。";
      $("verUpdate").disabled = true;
    }else{
      $("verMsg").textContent = "平台 "+VER.platform+" · "+
        (kind==="git" ? "git 仓库" : "发行包")+" · "+d.install.root;
    }
  }catch(e){ $("verMsg").textContent = "读版本失败："+e; }
}
async function verCheck(){
  const btn=$("verCheck"), msg=$("verMsg"), up=$("verUpdate");
  btn.disabled=true; up.disabled=true; msg.style.color=""; msg.textContent="检查中…";
  try{
    const p = encodeURIComponent($("ghProxy").value.trim());
    const r = await fetch("/api/version?check=1&proxy="+p); const d = await r.json();
    if(d.error){ msg.style.color="#f85149"; msg.textContent=d.error; return; }
    VER.latest=d.latest; VER.has_update=!!d.has_update;
    if(d.has_update){
      $("verLatest").textContent = "→ 最新 v"+d.latest;
      msg.textContent = "发现新版 v"+d.latest+"（"+(d.asset?d.asset.name:"?")+"）";
      up.disabled=false;
    }else{
      $("verLatest").textContent = "已是最新";
      msg.textContent = "已是最新 v"+d.latest;
    }
  }catch(e){ msg.style.color="#f85149"; msg.textContent="检查失败："+e; }
  finally{ btn.disabled=false; }
}
async function verPoll(n){
  const log=$("verLog"), msg=$("verMsg");
  if(n > 150){ msg.style.color="#f85149"; msg.textContent="等太久了，手动刷新页面看状态（日志见 .run/update.log）"; return; }
  try{
    const r = await fetch("/api/update/status",{cache:"no-store"}); const d = await r.json();
    if(d.log && d.log.length){ log.textContent = d.log.join("\n"); log.scrollTop = log.scrollHeight; }
    if(d.state === "ok"){
      msg.style.color="#3fb950"; msg.textContent="已升级到 v"+d.to+"，刷新中…";
      setTimeout(()=>location.reload(), 1500); return;
    }
    if(d.state === "fail"){
      msg.style.color="#f85149"; msg.textContent="升级失败："+d.message;
      $("verCheck").disabled=false; return;
    }
  }catch(e){ msg.style.color="#d29922"; msg.textContent="面板重启中…（等待重连）"; }
  setTimeout(()=>verPoll(n+1), 1200);
}
$("verCheck").onclick = verCheck;
$("verUpdate").onclick = async () => {
  if(!VER.has_update) return;
  if(!confirm("确认升级到 v"+VER.latest+"？\n\n流程：下载制品 → 停服务 → 覆盖代码 → 重装依赖 → 重启。\n面板会断开 10~30 秒，config.json 和 .venv 不动。")) return;
  const up=$("verUpdate"), msg=$("verMsg");
  up.disabled=true; $("verCheck").disabled=true;
  msg.style.color=""; msg.textContent="下载制品并准备升级…";
  try{
    const r = await fetch("/api/update",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({confirm:true, proxy:$("ghProxy").value.trim()})});
    const d = await r.json();
    if(d.error){
      msg.style.color="#f85149"; msg.textContent="升级失败："+d.error;
      up.disabled=false; $("verCheck").disabled=false; return;
    }
    if(d.up_to_date){ msg.textContent="已经是最新 v"+d.latest; $("verCheck").disabled=false; return; }
    $("verLog").style.display="block";
    msg.style.color="#d29922";
    msg.textContent="已开始：v"+d.from+" → v"+d.to+"（"+d.asset+"）";
    verPoll(0);
  }catch(e){
    msg.style.color="#f85149"; msg.textContent="升级失败："+e;
    up.disabled=false; $("verCheck").disabled=false;
  }
};
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
$("copyProvBtn").onclick = async () => {
  const u = $("provUrl").textContent;
  try { await navigator.clipboard.writeText(u); $("copyMsg").textContent = "provider 地址已复制✓"; }
  catch(e) { $("copyMsg").textContent = "provider 地址：" + u; }
};
$("testBtn").onclick = async () => {
  $("egress").textContent="测试中…";
  const r = await fetch("/api/egress"); const d = await r.json();
  $("egress").textContent = d.ip ? ("当前出口 IP："+d.ip) : ("测试失败："+(d.error||"")); };
(async()=>{ switchTab((location.hash||"#run").slice(1), true);
  await loadServers(); loadNodes(false); await loadState(); await loadAccounts();
  verInit();
  setInterval(loadAccounts, 15000); setInterval(loadState, 5000);
  setInterval(loadServers, 60000); })();
</script>
</body>
</html>"""


class Panel:
    def __init__(self, config_path=None, web_host="127.0.0.1", web_port=8765):
        self.config_path = config_path
        self.cfg = cfgmod.load(config_path)
        self.pool = self._make_pool()
        self.srv = None
        self._st_srv = None
        self._st_lock = asyncio.Lock()
        self._bench = None
        self._preconn = None
        # 面板自身监听端口：改代理端口时要把面板端口排除掉，否则自己把自己顶掉
        self.web_host = web_host
        self.web_port = int(web_port or 8765)
        # 一键更新：同一时间只允许一个，重启进程后自然复位
        self._updating = False

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

    def _egress_country(self) -> str:
        """Chinese country label of the node that last carried traffic."""
        if self.srv is None:
            return ""
        host = getattr(self.srv.picker, "last_host", None)
        if not host:
            return ""
        from . import geo
        code = self.srv.picker.country_of(host)
        if not code:
            return ""
        return geo.label(code)[0]

    async def api_state(self, req):
        running = self.srv is not None
        return web.json_response({
            "authed": self.pool is not None and len(self.pool.clients) > 0,
            "running": running,
            "account_count": (len(self.pool.clients) if self.pool else 0),
            "strategy": (self.pool.strategy if self.pool else
                         self.cfg.get("pool_strategy", "round-robin")),
            "country": (self.srv.picker.country if running else self.cfg.get("country") or ""),
            "node": (getattr(self.srv.picker, "node", None) or "" if running else ""),
            "egress_country": (self._egress_country() if running else ""),
            "host": self.cfg.get("host", "127.0.0.1"),
            "http_port": self.cfg.get("http_port", 8080),
            "socks_port": self.cfg.get("socks_port", 1080),
            "web_port": self.web_port,
            "version": _updater().VERSION,
            "platform": _updater().platform_tag(),
            "updating": bool(self._updating),
        })

    # ---- 版本 / 一键更新 ----
    def _upd_proxy(self, given: str | None = None) -> str | None:
        """GitHub 访问用的代理：请求里带的 > 配置里的 > 环境变量。"""
        for v in (given, self.cfg.get("update_proxy"),
                  os.environ.get("FXPROXY_UPDATE_PROXY")):
            s = (v or "").strip()
            if s:
                return s
        return None

    async def api_version(self, req):
        upd = _updater()
        plat = upd.platform_tag()
        inst = upd.detect_install()
        out = {
            "current": upd.VERSION,
            "platform": plat,
            "platform_label": upd.platform_label(plat),
            "install": inst,
            "update_proxy": self.cfg.get("update_proxy", ""),
            "repo": upd.REPO,
            "doc_url": f"https://github.com/{upd.REPO}/releases",
        }
        if req.query.get("check") in ("1", "true", "yes"):
            proxy = self._upd_proxy(req.query.get("proxy"))
            try:
                rel = await upd.fetch_latest(proxy=proxy)
                asset = upd.pick_asset(rel["assets"], rel["tag"], plat)
                out.update({
                    "latest": rel["version"],
                    "tag": rel["tag"],
                    "has_update": upd.is_newer(rel["version"], upd.VERSION),
                    "asset": asset,
                    "notes": rel["notes"],
                    "html_url": rel["html_url"],
                    "checked_at": int(time.time()),
                })
                if asset is None:
                    names = ", ".join(a["name"] for a in rel["assets"]) or "无附件"
                    out["error"] = (f"{upd.platform_label(plat)} 制品不在 {rel['tag']} 里"
                                    f"（附件：{names}）")
            except Exception as e:  # 网络原因千奇百怪，别让面板 500
                out["error"] = f"检查失败：{e}"
        return web.json_response(out)

    async def api_update(self, req):
        upd = _updater()
        if self._updating:
            return web.json_response({"error": "已有更新在进行，等它跑完"}, status=409)
        try:
            body = await req.json()
        except Exception:
            body = {}
        if not body.get("confirm"):
            return web.json_response({"error": "需要 confirm:true 才真正执行"}, status=400)

        inst = upd.detect_install()
        if inst["kind"] == "unknown":
            return web.json_response(
                {"error": "找不到可覆盖的源码目录（当前像是 pip 装进 site-packages）。"
                          "用 deploy/linux/install.sh 或发行包重装后再用面板更新。"},
                status=400)
        if not inst["writable"]:
            return web.json_response(
                {"error": f"目录不可写：{inst['root']}。用安装时那个用户（或 root）跑面板。"},
                status=400)
        dest = Path(inst["root"])

        # 面板里填的代理顺手存进配置，下次检查不用再填
        given = (body.get("proxy") or "").strip()
        if given and given != (self.cfg.get("update_proxy") or ""):
            self.cfg["update_proxy"] = given
            try:
                cfgmod.save(self.cfg, self.config_path)
            except Exception:
                pass
        proxy = self._upd_proxy(given)

        self._updating = True
        try:
            rel = await upd.fetch_latest(proxy=proxy)
            # 先比版本再挑制品：已是最新时不该因为「本平台没附件」报错
            if not upd.is_newer(rel["version"], upd.VERSION):
                self._updating = False
                return web.json_response({"ok": True, "up_to_date": True,
                                          "current": upd.VERSION,
                                          "latest": rel["version"]})
            asset = upd.pick_asset(rel["assets"], rel["tag"], upd.platform_tag())
            if asset is None:
                raise RuntimeError(
                    f"{rel['tag']} 里没有 {upd.platform_label()} 制品，"
                    f"可用：{', '.join(a['name'] for a in rel['assets']) or '无'}")
            cfg_path = Path(self.config_path) if self.config_path else cfgmod.DEFAULT_PATH
            info = await upd.run_update(
                dest, cfg_path, rel["tag"], asset, proxy=proxy,
                gh_proxy=(self.cfg.get("update_gh_proxy") or "").strip() or None,
                web_host=self.web_host, web_port=self.web_port,
            )
            return web.json_response(info)
        except Exception as e:
            self._updating = False
            return web.json_response({"error": str(e)}, status=500)

    async def api_update_status(self, req):
        upd = _updater()
        inst = upd.detect_install()
        root = Path(inst["root"]) if inst["root"] else Path.cwd()
        d = upd.read_status(root)
        d["updating"] = bool(self._updating)
        d["current"] = upd.VERSION
        return web.json_response(d)

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
            row["email"] = c.email() if q.get("ok") else None
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

    async def api_nodes(self, req):
        """Flatten the public server list into individual egress nodes.

        No credentials needed — the Mozilla serverlist is public. With
        ``?probe=1`` each node gets a TCP-connect latency measurement so the
        panel can show which endpoints are actually reachable from this host.
        """
        from . import geo
        want_probe = req.query.get("probe") in ("1", "true", "yes")

        def _collect():
            gc = GuardianClient("")
            by_country = gc.nodes_by_country()
            out = []
            for code, meta in by_country.items():
                zh, continent = geo.label(code, meta.get("name"))
                for host, port in meta["nodes"]:
                    out.append({
                        "code": code,
                        "name": zh,
                        "continent": continent,
                        "host": host,
                        "port": int(port),
                    })
            # 按洲域 → 国家 → 主机排序，前端无需再排
            order = {c: i for i, c in enumerate(geo.CONTINENT_ORDER)}
            out.sort(key=lambda n: (order.get(n["continent"], 99), n["name"], n["host"]))
            return out

        try:
            nodes = await asyncio.to_thread(_collect)
        except Exception as e:
            return web.json_response({"nodes": [], "error": str(e)})

        if want_probe and nodes:
            def _probe(node):
                t0 = time.monotonic()
                try:
                    # TCP connect 探测：不发送任何协议数据，只看端口是否可达
                    with socket.create_connection((node["host"], node["port"]), timeout=4):
                        node["latency_ms"] = round((time.monotonic() - t0) * 1000)
                        node["ok"] = True
                except Exception:
                    node["latency_ms"] = None
                    node["ok"] = False
                return node

            # 16 并发：34 节点约 3 批，实测 ~5.7s 内完成
            with ThreadPoolExecutor(max_workers=16) as ex:
                nodes = list(ex.map(_probe, nodes))

        # 出口 IP：?egress=1 时实测探测（较慢，30-60s），否则读缓存
        if req.query.get("egress") in ("1", "true", "yes") and nodes:
            sem = asyncio.Semaphore(16)
            results = await asyncio.gather(
                *(self._probe_egress(n["host"], n["port"], sem) for n in nodes)
            )
            for host, ip in results:
                if ip:
                    Panel._egress_cache[host] = ip
        for n in nodes:
            n["egress_ip"] = Panel._egress_cache.get(n["host"])

        return web.json_response({"nodes": nodes})

    async def api_start(self, req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        country = (body.get("country") or "").upper() or None
        node = (body.get("node") or "").strip() or None
        if self.srv is not None:
            cur_node = getattr(self.srv.picker, "node", None)
            if node is None and cur_node is None and self.srv.picker.country == country:
                return web.json_response({"ok": True, "already": True})
            if node is None:
                # country-level switch reuses the running server
                try:
                    self.srv.picker.node = None
                    await self.srv.switch_country(country)
                    return web.json_response({"ok": True, "switched": True})
                except Exception as e:
                    return web.json_response({"error": str(e)})
            # node-level selection needs a dedicated picker: restart
            try:
                await self.srv.stop()
            except Exception:
                pass
            self.srv = None
        if self.pool is None or not self.pool.tokens:
            # 空池非 None（历史配置 refresh_tokens=[] 或异常路径）时 clients[0]
            # 会裸抛 IndexError；这里一并挡掉。
            return web.json_response({"error": "无账号，先添加"})
        try:
            picker = NodePicker(self.pool.clients[0], country, node)
            srv = ProxyServer(
                self.pool, picker,
                host=self.cfg.get("host", "127.0.0.1"),
                http_port=self.cfg.get("http_port", 8080),
                socks_port=self.cfg.get("socks_port", 1080),
                failover=self.cfg.get("failover", 3),
            )
            await srv.start()
            self.srv = srv
            return web.json_response({"ok": True, "account_count": len(self.pool.clients),
                                      "node": node or ""})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_speedtest(self, req):
        """Download benchmark pinned to one node via a temporary proxy
        instance on a free port pair; reports megabits per second."""
        try:
            body = await req.json()
        except Exception:
            body = {}
        node = (body.get("node") or "").strip() or None
        if self.pool is None or not self.pool.tokens:
            return web.json_response({"error": "无账号，先添加"})
        async with self._st_lock:
            if self._st_srv is not None:
                return web.json_response({"error": "已有测速在进行"})
            srv = None
            try:
                picker = NodePicker(self.pool.clients[0], None, node)
                st_port, _st_socks = _pick_free_ports()
                srv = ProxyServer(
                    self.pool, picker,
                    host="127.0.0.1", http_port=st_port, socks_port=_st_socks,
                    failover=1,
                )
                await srv.start()
                self._st_srv = srv

                def _bench():
                    import requests as _rq
                    t0 = time.monotonic()
                    got = 0
                    proxy = f"http://127.0.0.1:{st_port}"
                    with _rq.get(
                        "https://speed.cloudflare.com/__down?bytes=8000000",
                        proxies={"http": proxy, "https": proxy},
                        stream=True, timeout=(5, 25),
                    ) as r:
                        r.raise_for_status()
                        for chunk in r.iter_content(65536):
                            got += len(chunk)
                    dt = time.monotonic() - t0
                    return {"bytes": got, "seconds": round(dt, 2),
                            "mbps": round(got * 8 / dt / 1e6, 1)}
                res = await asyncio.to_thread(_bench)
                return web.json_response({"ok": True, **res})
            except Exception as e:
                return web.json_response({"error": str(e)})
            finally:
                if srv is not None:
                    try:
                        await srv.stop()
                    except Exception:
                        pass
                self._st_srv = None

    async def api_stop(self, req):
        if self.srv is not None:
            await self.srv.stop()
            self.srv = None
        return web.json_response({"ok": True})

    async def api_ports(self, req):
        """改监听端口：落盘 config，运行中则原地重启。

        8080 在国内机器上基本是挤不进去的（Tomcat/Jenkins/一堆 dev server），
        让用户去改 config.json 再重启进程太重；这里一次请求搞定。
        """
        try:
            body = await req.json()
        except Exception:
            body = {}

        old_http = int(self.cfg.get("http_port", 8080) or 8080)
        old_socks = int(self.cfg.get("socks_port", 1080) or 1080)

        def _parse(field, default):
            raw = body.get(field)
            if raw in (None, ""):
                return default
            try:
                port = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{field} 不是数字：{raw!r}")
            if not (1 <= port <= 65535):
                raise ValueError(f"{field} 超出范围 1-65535：{port}")
            return port

        try:
            http_port = _parse("http_port", old_http)
            socks_port = _parse("socks_port", old_socks)
        except ValueError as e:
            return web.json_response({"error": str(e)})

        if http_port == socks_port:
            return web.json_response({"error": "HTTP 与 SOCKS5 不能用同一个端口"})
        if self.web_port in (http_port, socks_port):
            return web.json_response(
                {"error": f"端口 {self.web_port} 是面板自己在用，换一个"}
            )

        if (http_port, socks_port) == (old_http, old_socks):
            return web.json_response({"ok": True, "unchanged": True,
                                      "http_port": http_port, "socks_port": socks_port})

        # 只检测真正变了的端口：没变的那个此刻可能正被自己监听。
        host = self.cfg.get("host", "127.0.0.1")
        probe_host = host if host not in ("0.0.0.0", "::", "*", "") else "127.0.0.1"
        for port, was in ((http_port, old_http), (socks_port, old_socks)):
            if port != was and not _port_free(probe_host, port):
                return web.json_response(
                    {"error": f"端口 {port} 已被其它程序占用，换一个"}
                )

        was_running = self.srv is not None
        country = self.srv.picker.country if was_running else (self.cfg.get("country") or None)
        node = (getattr(self.srv.picker, "node", None) or None) if was_running else None

        if was_running:
            try:
                await self.srv.stop()
            except Exception:
                pass
            self.srv = None

        self.cfg["http_port"] = http_port
        self.cfg["socks_port"] = socks_port
        self._persist()

        base = {"ok": True, "http_port": http_port, "socks_port": socks_port}
        if not was_running:
            return web.json_response(base)
        if self.pool is None or not self.pool.tokens:
            return web.json_response({**base, "stopped": True,
                                      "note": "端口已保存；没有账号，代理保持停止"})
        try:
            picker = NodePicker(self.pool.clients[0], country, node)
            srv = ProxyServer(
                self.pool, picker,
                host=host, http_port=http_port, socks_port=socks_port,
                failover=self.cfg.get("failover", 3),
            )
            await srv.start()
            self.srv = srv
        except Exception as e:
            # 已停的旧实例回不来，但端口值已经写盘，用户改完再点启动即可
            return web.json_response({**base, "restarted": False,
                                      "error": f"端口已保存，但按新端口重启失败：{e}"})
        return web.json_response({**base, "restarted": True})

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

    def _bridge_uuid(self) -> str:
        """VLESS uuid of the local xray bridge, empty if not deployed."""
        try:
            from pathlib import Path
            p = Path(__file__).resolve().parent.parent / "bridge" / "uuid.txt"
            return p.read_text().strip() if p.exists() else ""
        except Exception:
            return ""

    def _bridge_host(self, req) -> str:
        host = req.host.split(":", 1)[0]
        if host in ("0.0.0.0", "::", "*"):
            host = "127.0.0.1"
        return self.cfg.get("sub_server") or host

    def _bridge_available(self, host: str) -> bool:
        return bool(self._bridge_uuid()) and host != "127.0.0.1"

    async def ws_echo(self, req):
        """Diagnostic: verifies WebSocket pass-through on this deployment."""
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await ws.send_str("echo:" + msg.data)
            elif msg.type == web.WSMsgType.ERROR:
                break
        return ws

    async def ws_bridge(self, req):
        """Reverse-proxy a WebSocket stream to the local xray bridge.

        Lets external clients reach the proxy through the panel's single
        exposed HTTP port: client --wss/gateway--> panel /vless --> xray
        (127.0.0.1:8081) --> fxproxy SOCKS5 --> Mozilla egress.
        """
        import aiohttp as _aio
        downstream = web.WebSocketResponse(autoping=True)
        await downstream.prepare(req)
        upstream_url = "ws://127.0.0.1:8081/vless"
        try:
            async with _aio.ClientSession() as session:
                async with session.ws_connect(
                    upstream_url, autoping=True, heartbeat=None
                ) as upstream:

                    async def _d2u():
                        async for msg in downstream:
                            if msg.type == web.WSMsgType.BINARY:
                                await upstream.send_bytes(msg.data)
                            elif msg.type == web.WSMsgType.TEXT:
                                await upstream.send_str(msg.data)
                            else:
                                break

                    async def _u2d():
                        async for msg in upstream:
                            if msg.type == _aio.WSMsgType.BINARY:
                                await downstream.send_bytes(msg.data)
                            elif msg.type == _aio.WSMsgType.TEXT:
                                await downstream.send_str(msg.data)
                            else:
                                break

                    await asyncio.gather(_d2u(), _u2d())
        except Exception:
            pass
        return downstream

    # ---- built-in vless server: one WS path per Fastly node -----------

    _nodes_cache: dict = {"data": None, "ts": 0.0}
    _egress_cache: dict = {}

    class _PreconnPool:
        """Idle TLS-ready connections to Fastly nodes.

        Each new client connection otherwise pays a full cross-region TLS
        handshake (~300ms) before CONNECT. Keeping a couple of pre-handshaked
        sockets per node turns that into a bare CONNECT round-trip. A tunnel
        can serve exactly one CONNECT, so entries are consumed, not reused;
        the pool is lazily replenished after each take.
        """

        def __init__(self, ssl_ctx, per_node=2, idle_ttl=40):
            self._ssl = ssl_ctx
            self._per = per_node
            self._ttl = idle_ttl
            self._idle: dict = {}
            self._locks: dict = {}

        def _lock(self, host):
            l = self._locks.get(host)
            if l is None:
                l = self._locks[host] = asyncio.Lock()
            return l

        async def _open(self, host, port):
            return await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, ssl=self._ssl, server_hostname=host),
                timeout=6,
            )

        async def acquire(self, host, port):
            async with self._lock(host):
                lst = self._idle.get(host, [])
                now = time.monotonic()
                while lst:
                    r, w, ts = lst.pop()
                    if now - ts < self._ttl and not r.at_eof() and not w.is_closing():
                        asyncio.create_task(self._replenish(host, port))
                        return r, w
                    try:
                        w.close()
                    except Exception:
                        pass
            # 无空闲连接时现场握手。必须在锁外做：mux 会话下多条流会
            # 并发 acquire，锁内握手会把跨区 TLS 串行化（30 流 ≈ 9s+）。
            return await self._open(host, port)

        async def _replenish(self, host, port):
            async with self._lock(host):
                lst = self._idle.setdefault(host, [])
                if len(lst) >= self._per:
                    return
                try:
                    r, w = await self._open(host, port)
                    lst.append((r, w, time.monotonic()))
                except Exception:
                    pass

    async def _probe_egress(self, host, port, sem):
        """Tunnel through one node in plaintext HTTP to ipify; return its
        real egress IP (what target websites actually see)."""
        async with sem:
            try:
                reader, writer, _ = await asyncio.wait_for(
                    self._vless_open_tunnel(host, port, "api.ipify.org", 80),
                    timeout=30,
                )
                writer.write(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\n"
                             b"Connection: close\r\n\r\n")
                await writer.drain()
                data = await asyncio.wait_for(reader.read(4096), timeout=10)
                writer.close()
                body = (data.split(b"\r\n\r\n", 1)[1]
                        if b"\r\n\r\n" in data else b"").decode(errors="replace").strip()
                ip = body.splitlines()[-1].strip() if body else ""
                return host, (ip if ip.count(".") == 3 else None)
            except Exception:
                return host, None

    async def _public_nodes(self):
        """[(code, zh_name, host, port)] with a 300s cache; public list."""
        import time as _t
        c = Panel._nodes_cache
        if c["data"] is not None and _t.monotonic() - c["ts"] < 300:
            return c["data"]
        from . import geo

        def _fetch():
            gc = GuardianClient("")
            out = []
            for code, meta in gc.nodes_by_country().items():
                zh, _ = geo.label(code, meta.get("name"))
                for h, p in meta["nodes"]:
                    out.append((code, zh, h, int(p)))
            out.sort()
            return out
        try:
            c["data"] = await asyncio.to_thread(_fetch)
            c["ts"] = _t.monotonic()
        except Exception:
            c["data"] = c["data"] or []
        return c["data"]

    async def _vless_open_tunnel(self, node_host, node_port,
                                 target_host, target_port):
        """Open the Fastly tunnel pinned to one node, walking the account
        pool on auth failures (same policy as ProxyServer._open_tunnel).

        JWT is taken from the client cache on the hot path (no thread-pool
        hop); only an expired pass pays the to_thread round-trip. A single
        reused ProxyServer supplies the TLS context so bursts of Clash
        health checks don't churn SSLContexts.
        """
        if self.pool is None or not self.pool.tokens:
            raise web.HTTPServiceUnavailable(text="no account in pool")
        if self._bench is None:
            self._bench = ProxyServer(
                self.pool, NodePicker(self.pool.clients[0], None), failover=1)
            self._preconn = Panel._PreconnPool(self._bench._ssl)
        bench = self._bench
        accounts = self.pool.pick_many(len(self.pool.clients))
        last_err = None
        for _idx, gc in accounts:
            try:
                jwt = gc._pass
                if not jwt or time.time() >= gc._pass_exp - 60:
                    jwt = await asyncio.to_thread(gc.proxy_pass)
            except Exception as e:
                last_err = e
                self.pool.report_failure(gc)
                continue
            conn = None
            try:
                conn = await self._preconn.acquire(node_host, node_port)
            except Exception:
                conn = None
            try:
                reader, writer, leftover, _ = await bench._connect_one(
                    node_host, node_port, target_host, target_port, jwt, conn=conn
                )
                return reader, writer, leftover
            except Exception as e:
                last_err = e
                if "401" in str(e) or "403" in str(e):
                    self.pool.report_failure(gc)
                # 预握手连接可能已被 Fastly 关闭：丢弃并现场重试一次
                if conn is not None:
                    try:
                        conn[1].close()
                    except Exception:
                        pass
                    try:
                        reader, writer, leftover, _ = await bench._connect_one(
                            node_host, node_port, target_host, target_port, jwt
                        )
                        return reader, writer, leftover
                    except Exception as e2:
                        last_err = e2
        raise ConnectionError(f"tunnel via {node_host} failed: {last_err!r}")

    async def ws_vless(self, req):
        """Built-in minimal vless-over-ws server, one path per node.

        Path: /vless/<fastly-hostname> — the slug pins the egress node.
        Wire format (first frames, may be split): version(1) uuid(16)
        addonLen(1) addon command(1) port(2, BE) addrType(1) addr, then
        raw payload; everything after handshake is a plain bidirectional
        pipe between the WebSocket and the Fastly TLS tunnel.
        """
        uuid_hex = self._bridge_uuid()
        if not uuid_hex:
            raise web.HTTPNotFound()
        slug = req.match_info.get("slug", "").strip().lower()
        nodes = await self._public_nodes()
        node = next((n for n in nodes if n[2] == slug), None)
        if node is None:
            # unknown slug: only Fastly list members are allowed as egress
            raise web.HTTPNotFound()
        node_host, node_port = node[2], node[3]

        ws = web.WebSocketResponse(autoping=True, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(req)

        buf = b""

        async def _need(n):
            nonlocal buf
            while len(buf) < n:
                msg = await ws.receive()
                if msg.type != web.WSMsgType.BINARY:
                    raise ConnectionError("client closed during handshake")
                buf += msg.data
            out, buf = buf[:n], buf[n:]
            return out

        try:
            expected = bytes.fromhex(uuid_hex.replace("-", ""))
            await _need(1)                                    # version
            got = await _need(16)                             # uuid
            if got != expected:
                await ws.close(code=web.WSCloseCode.VIOLATED_POLICY)
                return ws
            addon_len = (await _need(1))[0]
            if addon_len:
                await _need(addon_len)
            cmd = (await _need(1))[0]
            if cmd != 1:                                      # TCP only
                print(f"[vless] 拒绝 cmd={cmd}（非 TCP：多为 UDP/DNS）"
                      f" node={node_host}", flush=True)
                raise ConnectionError(f"unsupported vless command {cmd}")
            port = int.from_bytes(await _need(2), "big")
            atyp = (await _need(1))[0]
            if atyp == 1:
                import socket as _s
                target = _s.inet_ntoa(await _need(4))
            elif atyp == 2:
                ln = (await _need(1))[0]
                # 域名在 vless 线上是 ASCII（punycode 已由客户端转换）。
                # 不能用 decode("idna")：它对含下划线/非标准 label 的真实
                # 域名会抛 UnicodeError，导致大量正常请求被断连。
                target = (await _need(ln)).decode("ascii", "replace")
            elif atyp == 3:
                import socket as _s
                target = _s.inet_ntop(_s.AF_INET6, await _need(16))
            else:
                raise ConnectionError(f"bad addrType {atyp}")

            if "mux.sing-box.arpa" in target:
                # sing-mux 会话：把整条 wss 桥接到本地 Go mux 服务端，
                # 由它解复用；每个流再回到本面板按普通 vless 开 Fastly
                # 隧道。这样浏览器几十条连接只占一条到网关的 wss。
                await self._serve_mux_session(ws, buf, node_host, node_port)
                return ws

            reader, writer, leftover = await self._vless_open_tunnel(
                node_host, node_port, target, port
            )
        except Exception as _e:
            print(f"[vless] 握手失败 target={locals().get('target','?')}:"
                  f"{locals().get('port','?')} {_e!r}", flush=True)
            try:
                await ws.close(code=web.WSCloseCode.ABNORMAL_CLOSURE)
            except Exception:
                pass
            return ws

        # vless response header: version 0, no addons
        try:
            await ws.send_bytes(b"\x00\x00")
        except Exception:
            try:
                writer.close()
            except Exception:
                pass
            return ws

        _stat = {"up": 0, "down": 0}

        async def _w2t():
            nonlocal buf
            if buf:
                writer.write(buf)
                await writer.drain()
                _stat["up"] += len(buf)
                buf = b""
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                    _stat["up"] += len(msg.data)
                else:
                    break

        async def _t2w():
            try:
                if leftover:
                    await ws.send_bytes(leftover)
                    _stat["down"] += len(leftover)
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send_bytes(data)
                    _stat["down"] += len(data)
            except Exception:
                pass

        # 任一方向结束就收尾：目标站常用 Connection: close 主动断开，
        # 若此时仍等 _w2t 的 WS 消息会永久挂住，客户端拿不到关闭信号而
        # 卡死（测速短连接不暴露，真实浏览大量触发）。
        t_up = asyncio.create_task(_w2t())
        t_down = asyncio.create_task(_t2w())
        try:
            await asyncio.wait({t_up, t_down},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t_up, t_down):
                if not t.done():
                    t.cancel()
            try:
                writer.close()
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass
        return ws

    async def _serve_mux_session(self, ws, buf, node_host, node_port):
        """Bridge a sing-mux wss session to the local Go mux server.

        The Go process demultiplexes the session and forwards each stream to
        a per-session ephemeral local port as ``socks5-addr + payload``. The
        per-stream handler below opens the pinned Fastly tunnel and pipes it.
        """
        mux_port = 19998
        session = {"w": None}

        async def on_stream(reader, writer):
            up_w = None
            try:
                atyp = (await reader.readexactly(1))[0]
                import socket as _s
                if atyp == 1:
                    th = _s.inet_ntoa(await reader.readexactly(4))
                elif atyp == 3:
                    ln = (await reader.readexactly(1))[0]
                    th = (await reader.readexactly(ln)).decode("ascii", "replace")
                elif atyp == 4:
                    th = _s.inet_ntop(_s.AF_INET6, await reader.readexactly(16))
                else:
                    return
                tp = int.from_bytes(await reader.readexactly(2), "big")
                r, up_w, leftover = await self._vless_open_tunnel(
                    node_host, node_port, th, tp)
                if leftover:
                    writer.write(leftover)
                    await writer.drain()

                async def c2t():
                    try:
                        while True:
                            d = await reader.read(65536)
                            if not d:
                                break
                            up_w.write(d)
                            await up_w.drain()
                    except Exception:
                        pass

                async def t2c():
                    try:
                        while True:
                            d = await r.read(65536)
                            if not d:
                                break
                            writer.write(d)
                            await writer.drain()
                    except Exception:
                        pass

                t1 = asyncio.create_task(c2t())
                t2 = asyncio.create_task(t2c())
                try:
                    await asyncio.wait({t1, t2},
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in (t1, t2):
                        if not t.done():
                            t.cancel()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass
                if up_w is not None:
                    try:
                        up_w.close()
                    except Exception:
                        pass

        try:
            server = await asyncio.start_server(on_stream, "127.0.0.1", 0)
        except Exception:
            await ws.close()
            return
        try:
            port = server.sockets[0].getsockname()[1]
            try:
                gr, gw = await asyncio.open_connection("127.0.0.1", mux_port)
            except Exception:
                print(f"[mux] 无法连接本地 mux 服务端 {mux_port}",
                      flush=True)
                await ws.close()
                return
            session["w"] = gw
            gw.write(port.to_bytes(2, "big"))
            await gw.drain()
            # vless 响应头，客户端跳过 2 字节后开始读 mux 帧
            try:
                await ws.send_bytes(b"\x00\x00")
            except Exception:
                return
            if buf:
                gw.write(buf)
                await gw.drain()

            async def ws2go():
                try:
                    async for msg in ws:
                        if msg.type == web.WSMsgType.BINARY:
                            gw.write(msg.data)
                            await gw.drain()
                        else:
                            break
                except Exception:
                    pass

            async def go2ws():
                try:
                    while True:
                        d = await gr.read(65536)
                        if not d:
                            break
                        await ws.send_bytes(d)
                except Exception:
                    pass

            t1 = asyncio.create_task(ws2go())
            t2 = asyncio.create_task(go2ws())
            try:
                await asyncio.wait({t1, t2},
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (t1, t2):
                    if not t.done():
                        t.cancel()
        finally:
            try:
                server.close()
            except Exception:
                pass
            w = session.get("w")
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
            try:
                await ws.close()
            except Exception:
                pass

    async def api_sub(self, req):
        """vless:// share links (base64 wrapped) for v2rayN / sing-box.

        When the bridge is deployed and reached via a public host, one
        entry per Fastly node is emitted so clients can hand-pick the
        egress; without bridge only the generic link is returned.
        """
        server = self._bridge_host(req)
        if not self._bridge_available(server):
            return web.json_response({"error": "vless bridge not deployed"})
        uuid = self._bridge_uuid()
        nodes = await self._public_nodes()
        lines = []
        if nodes:
            for code, name, host, port in nodes:
                label = f"{code}-{name}"
                lines.append(
                    f"vless://{uuid}@{server}:443"
                    f"?encryption=none&security=tls&sni={server}"
                    f"&type=ws&host={server}"
                    f"&path=%2Fvless%2F{host}"
                    f"#{label}"
                )
        else:
            lines.append(
                f"vless://{uuid}@{server}:443"
                f"?encryption=none&security=tls&sni={server}"
                f"&type=ws&host={server}&path=%2Fvless#fxproxy-bridge")
        b64 = base64.b64encode(("\n".join(lines) + "\n").encode()).decode()
        return web.Response(
            text=b64,
            content_type="text/plain",
            headers={"Content-Disposition": 'attachment; filename="fxproxy-sub.txt"'},
        )

    async def api_clash(self, req):
        """Clash/Mihomo subscription served at /clash.yaml.

        The proxy host defaults to the Host header used to reach this panel
        (LAN IP / VPS domain), so importing from another device just works.
        A configured ``sub_server`` overrides it for split-horizon setups.
        When the vless bridge is deployed over a public host, one
        ``fxproxy-<code>`` node per Fastly location is appended so clients
        can hand-pick the egress node from the subscription itself.
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
        if self._bridge_available(server):
            nodes = await self._public_nodes()
            per_node = []
            if nodes:
                for code, name, nh, nport in nodes:
                    per_node.append(
                        f"  - name: \"fxproxy-{code}-{name}\"\n"
                        "    type: vless\n"
                        f"    server: \"{server}\"\n"
                        "    port: 443\n"
                        f"    uuid: {self._bridge_uuid()}\n"
                        "    tls: true\n"
                        f"    servername: \"{server}\"\n"
                        "    network: ws\n"
                        "    ws-opts:\n"
                        f"      path: /vless/{nh}\n"
                        "    udp: false\n"
                        "    ip-version: ipv4\n"
                    )
            else:
                per_node.append(
                    "  - name: \"fxproxy-vless-ws\"\n"
                    "    type: vless\n"
                    f"    server: \"{server}\"\n"
                    "    port: 443\n"
                    f"    uuid: {self._bridge_uuid()}\n"
                    "    tls: true\n"
                    f"    servername: \"{server}\"\n"
                    "    network: ws\n"
                    "    ws-opts:\n"
                    "      path: /vless\n"
                    "    udp: false\n"
                    "    ip-version: ipv4\n"
                )
            block = "".join(per_node) + "\n"
            marker = "proxy-groups:"
            if marker in text:
                head, tail = text.split(marker, 1)
                if nodes:
                    names = "".join(f"      - fxproxy-{c}-{n}\n"
                                    for c, n, _, _ in nodes)
                    # 提供两个聚合组：
                    #  - fxproxy-auto：load-balance 轮询全部节点（多节点/多连接
                    #    叠加带宽），但每条新连接会换节点、重建 wss，链路差时
                    #    开销大，故不设为默认。
                    #  - fxproxy-fast：url-test 自动选最快节点并固定使用，只在
                    #    失效时切换，最稳，作为 PROXY 默认。
                    lb_group = (
                        "\n  - name: \"fxproxy-auto\"\n"
                        "    type: load-balance\n"
                        "    url: https://www.gstatic.com/generate_204\n"
                        "    interval: 600\n"
                        "    lazy: true\n"
                        "    strategy: round-robin\n"
                        "    proxies:\n"
                        + names
                        + "\n  - name: \"fxproxy-fast\"\n"
                        "    type: url-test\n"
                        "    url: https://www.gstatic.com/generate_204\n"
                        "    interval: 600\n"
                        "    tolerance: 80\n"
                        "    lazy: true\n"
                        "    proxies:\n"
                        + names
                    )
                    new_group = ("      - fxproxy-fast\n"
                                 "      - fxproxy-auto\n"
                                 + names)
                else:
                    lb_group = ""
                    new_group = ('      - fxproxy-vless-ws\n'
                                 '      - "fxproxy-http"\n')
                # per-node 节点进 proxies 列表；负载均衡组进 proxy-groups
                text = (head.rstrip("\n") + "\n" + block
                        + marker + lb_group
                        + tail.replace('      - "fxproxy-http"\n',
                                       new_group, 1))
                # 有内置 vless 节点时，彻底移除指向网关裸端口的
                # http/socks5 代理。它们只在本地直连场景可用，走公网
                # 网关时 TCP 能握手但代理语义无响应，客户端选中就是
                # ERR_EMPTY_RESPONSE。provider 模式也会导出它们，属陷阱。
                import re as _re
                for _dead in ("fxproxy-http", "fxproxy-socks5"):
                    text = _re.sub(
                        r'  - name: "' + _re.escape(_dead) + r'"\n(?:    .*\n)*',
                        "", text)
                    # 同时清掉组内对这些已删除代理的引用，避免悬空引用
                    text = text.replace(f'      - "{_dead}"\n', "")

        # ?provider=1：只留 proxies 段，给 mihomo 当 proxy-provider 用。
        # 完整配置（含 port/mode/rules）塞进 provider 会把客户端的端口和规则顶掉，
        # 所以这里切出 proxies 块，其它字段一律不带。
        if req.query.get("provider") in ("1", "true", "yes"):
            lines = text.splitlines()
            out, keep = [], False
            for line in lines:
                if line.rstrip() == "proxies:":
                    keep = True
                elif keep and line and not line[0].isspace():
                    break          # 到了 proxy-groups: 之类的顶层键，停
                if keep:
                    out.append(line)
            text = "\n".join(out) + "\n"
            fname = "fxproxy-provider.yaml"
        else:
            fname = "fxproxy.yaml"

        return web.Response(
            text=text,
            content_type="text/yaml",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
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
    panel = Panel(config_path, web_host=web_host, web_port=web_port)
    app = web.Application()

    async def _enlarge_executor(_app):
        # Default asyncio pool is min(32, cpu+4) = 6 on 2-core hosts, which
        # serialises Clash health checks across 34 nodes. Widen it.
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=64))

    app.on_startup.append(_enlarge_executor)

    async def _start_muxserver(_app):
        # 若外部已在 19998 上跑着 muxserver，直接复用。
        try:
            _r, _w = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 19998), timeout=0.5)
            _w.close()
            print("[mux] 复用已在运行的 muxserver :19998")
            return
        except Exception:
            pass
        import os as _os
        import subprocess as _sp
        base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        exe = _os.environ.get("FXPROXY_MUXSERVER") or _os.path.join(
            base, "muxserver", "muxserver")
        if not _os.path.exists(exe):
            print("[mux] 未找到 muxserver，多路复用不可用（普通 vless 仍工作）")
            return
        try:
            proc = _sp.Popen([exe, "127.0.0.1:19998"],
                             stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            panel._mux_proc = proc
            print(f"[mux] muxserver 已启动 pid={proc.pid}")
        except Exception as e:
            print(f"[mux] muxserver 启动失败: {e!r}")

    async def _stop_muxserver(_app):
        proc = getattr(panel, "_mux_proc", None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    app.on_startup.append(_start_muxserver)
    app.on_cleanup.append(_stop_muxserver)
    app.add_routes([
        web.get("/", panel.index),
        web.get("/api/state", panel.api_state),
        web.get("/api/accounts", panel.api_accounts),
        web.post("/api/accounts/refresh", panel.api_accounts_refresh),
        web.post("/api/account/remove", panel.api_account_remove),
        web.post("/api/strategy", panel.api_strategy),
        web.get("/api/servers", panel.api_servers),
        web.get("/api/nodes", panel.api_nodes),
        web.post("/api/speedtest", panel.api_speedtest),
        web.get("/ws-echo", panel.ws_echo),
        web.get("/vless", panel.ws_bridge),
        web.get(r"/vless/{slug:.+}", panel.ws_vless),
        web.get("/sub", panel.api_sub),
        web.post("/api/start", panel.api_start),
        web.post("/api/stop", panel.api_stop),
        web.post("/api/ports", panel.api_ports),
        web.get("/api/version", panel.api_version),
        web.post("/api/update", panel.api_update),
        web.get("/api/update/status", panel.api_update_status),
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

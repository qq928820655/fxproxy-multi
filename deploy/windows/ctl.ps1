<#
.SYNOPSIS
    fxproxy 进程控制：start / stop / restart / status（Windows）。

.DESCRIPTION
    代理和面板各起一个 fxproxy.exe，PID 写进 .run\*.pid，日志写进 .run\*.log。
    被 start.bat / stop.bat / status.bat 调用，也可直接：
        powershell -NoProfile -ExecutionPolicy Bypass -File ctl.ps1 start
#>
[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status",
    [switch]$Open
)

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Venv    = Join-Path $Root ".venv"
$RunDir  = Join-Path $Root ".run"
$CfgPath = Join-Path $Root "config.json"
$Fx      = Join-Path $Venv "Scripts\fxproxy.exe"

function Ok($m)   { Write-Host "  OK   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  !    $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  X    $m" -ForegroundColor Red }

# Windows 的环境块大小写不敏感，但从 Git Bash 之类启动时会同时注入
# HTTPS_PROXY 和 https_proxy 两个键。Start-Process 复制环境时按不敏感处理，
# 两个键就撞成"已添加项"。这里把每个代理变量收敛成一份大写。
function Normalize-ProxyEnv {
    foreach ($n in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")) {
        $v = [Environment]::GetEnvironmentVariable($n, "Process")
        if ([string]::IsNullOrEmpty($v)) { continue }
        [Environment]::SetEnvironmentVariable($n, $null, "Process")   # 清掉（含小写别名）
        [Environment]::SetEnvironmentVariable($n, $v, "Process")      # 只按大写写回一份
    }
}

function Test-PortOpen([int]$Port) {
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $Port); return $true }
    catch { return $false }
    finally { $c.Dispose() }
}

function Read-Cfg {
    if (-not (Test-Path $CfgPath)) { return $null }
    try { return (Get-Content $CfgPath -Raw | ConvertFrom-Json) } catch { return $null }
}

function Get-PidFile([string]$Name) { Join-Path $RunDir "$Name.pid" }

function Get-RunningPid([string]$Name) {
    $f = Get-PidFile $Name
    if (-not (Test-Path $f)) { return 0 }
    $raw = (Get-Content $f -Raw).Trim()
    $pidValue = 0
    if (-not [int]::TryParse($raw, [ref]$pidValue)) { return 0 }
    $p = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($p) { return $pidValue }
    return 0
}

function Get-LogName([string]$Name) {
    $day = (Get-Date).ToString("yyyyMMdd")
    return Join-Path $RunDir "$Name-$day.log"
}

# --------------------------------------------------------------- start
function Start-All {
    # 注意：这里一律用 return 不用 exit —— 本脚本会被 install.ps1 以 & 调用，
    # 脚本内的 exit 会直接终止整个 PowerShell 进程，把安装流程也带走。
    if (-not (Test-Path $Fx)) { Bad "没装好：找不到 $Fx，先跑 install.bat"; return }
    if (-not (Test-Path $CfgPath)) { Bad "缺 config.json，先跑 install.bat"; return }
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir | Out-Null }
    Normalize-ProxyEnv

    $cfg = Read-Cfg
    $httpPort = 8080; $socksPort = 1080; $webPort = 8765
    if ($cfg) {
        if ($cfg.http_port)  { $httpPort  = [int]$cfg.http_port }
        if ($cfg.socks_port) { $socksPort = [int]$cfg.socks_port }
        if ($cfg.web_port)   { $webPort   = [int]$cfg.web_port }
    }

    $tokens = @()
    if ($cfg) { $tokens = @($cfg.refresh_tokens | Where-Object { $_ }) }
    $hasAccount = $tokens.Count -gt 0

    # ---- 代理（没账号就不起：TokenPool 需要至少一个 refresh_token，起了也是秒退）
    if (-not $hasAccount) {
        Warn "config.json 里还没有账号 —— 这次只起面板，代理不起"
        Warn "在面板「添加账号」里导入后，再跑一次 start.bat"
    } else {
        $pp = Get-RunningPid "proxy"
        if ($pp -gt 0) {
            Ok "代理已在运行 (PID $pp)"
        } else {
            if (Test-PortOpen $httpPort) { Bad "端口 $httpPort 被别的程序占着：面板「运行状态 → 端口」改掉，或改 config.json 的 http_port"; return }
            if (Test-PortOpen $socksPort) { Bad "端口 $socksPort 被别的程序占着：面板「运行状态 → 端口」改掉，或改 config.json 的 socks_port"; return }
            $log = Get-LogName "proxy"
            $err = Join-Path $RunDir "proxy.err.log"
            # -c 是顶层全局参数，必须放在子命令（run）前面
            $p = Start-Process -FilePath $Fx -ArgumentList @("-c", $CfgPath, "run") `
                 -WindowStyle Hidden -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
            $p.Id | Out-File -FilePath (Get-PidFile "proxy") -Encoding ascii -NoNewline
            Start-Sleep -Milliseconds 800
            if ($p.HasExited) {
                Bad "代理启动即退出，看 $err"
                if (Test-Path $err) { Get-Content $err -Tail 15 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray } }
                return
            }
            Ok "代理已启动 (PID $($p.Id))  HTTP:$httpPort  SOCKS5:$socksPort"
        }
    }

    # ---- 面板
    $wp = Get-RunningPid "web"
    if ($wp -gt 0) {
        Ok "面板已在运行 (PID $wp)"
    } else {
        if (Test-PortOpen $webPort) { Bad "面板端口 $webPort 被别的程序占着：重跑 install.bat 换 -WebPort，或关掉占用者"; return }
        $log = Get-LogName "web"
        $err = Join-Path $RunDir "web.err.log"
        $p = Start-Process -FilePath $Fx `
             -ArgumentList @("-c", $CfgPath, "web", "--no-open", "--web-port", "$webPort") `
             -WindowStyle Hidden -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
        $p.Id | Out-File -FilePath (Get-PidFile "web") -Encoding ascii -NoNewline
        Start-Sleep -Milliseconds 800
        if ($p.HasExited) {
            Bad "面板启动即退出，看 $err"
            if (Test-Path $err) { Get-Content $err -Tail 15 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray } }
        } else {
            Ok "面板已启动 (PID $($p.Id))  http://127.0.0.1:$webPort"
        }
    }

    # ---- 等端口就绪
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if ($hasAccount) {
            if ((Test-PortOpen $httpPort) -and (Test-PortOpen $webPort)) { break }
        } elseif (Test-PortOpen $webPort) { break }
        Start-Sleep -Milliseconds 400
    }
    if ($hasAccount) {
        if (Test-PortOpen $httpPort) { Ok "HTTP 代理可用 127.0.0.1:$httpPort" } else { Warn "HTTP 端口还没起来，稍等或看日志" }
        if (Test-PortOpen $socksPort) { Ok "SOCKS5 可用 127.0.0.1:$socksPort" } else { Warn "SOCKS5 端口还没起来" }
    }
    if (Test-PortOpen $webPort) { Ok "面板可用 http://127.0.0.1:$webPort" } else { Warn "面板端口还没起来" }

    if ($Open -and (Test-PortOpen $webPort)) {
        Start-Process "http://127.0.0.1:$webPort"
    }
}

# --------------------------------------------------------------- stop
function Stop-All {
    $killed = 0
    foreach ($name in @("proxy", "web")) {
        $tp = Get-RunningPid $name
        if ($tp -gt 0) {
            Stop-Process -Id $tp -Force -ErrorAction SilentlyContinue
            Ok "已停止 $name (PID $tp)"
            $killed++
        }
        Remove-Item (Get-PidFile $name) -ErrorAction SilentlyContinue
    }
    # 兜底：PID 文件丢了，按可执行文件路径收
    $procs = Get-CimInstance Win32_Process -Filter "Name='fxproxy.exe'" -ErrorAction SilentlyContinue
    foreach ($pr in $procs) {
        if ($pr.ExecutablePath -and $pr.ExecutablePath.StartsWith($Venv, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id $pr.ProcessId -Force -ErrorAction SilentlyContinue
            Ok "已清理残留 PID $($pr.ProcessId)"
            $killed++
        }
    }
    if ($killed -eq 0) { Warn "本来就没在跑" }
}

# --------------------------------------------------------------- status
function Show-Status {
    $cfg = Read-Cfg
    if (-not $cfg) { Bad "缺 config.json"; return }
    $httpPort = 8080; $socksPort = 1080; $webPort = 8765
    if ($cfg.http_port)  { $httpPort  = [int]$cfg.http_port }
    if ($cfg.socks_port) { $socksPort = [int]$cfg.socks_port }
    if ($cfg.web_port)   { $webPort   = [int]$cfg.web_port }

    $tokens = @($cfg.refresh_tokens | Where-Object { $_ })
    Write-Host ""
    Write-Host "  fxproxy 状态" -ForegroundColor White
    Write-Host "  账号     $($tokens.Count) 个   国家  $(if ($cfg.country) { $cfg.country } else { '任意' })"

    $pp = Get-RunningPid "proxy"
    if ($pp -gt 0) { Ok "代理进程 PID $pp" } else { Bad "代理未运行" }
    $wp = Get-RunningPid "web"
    if ($wp -gt 0) { Ok "面板进程 PID $wp" } else { Bad "面板未运行" }

    Write-Host "  端口     HTTP $httpPort  $(if (Test-PortOpen $httpPort) { 'LISTEN' } else { 'closed' })   SOCKS5 $socksPort  $(if (Test-PortOpen $socksPort) { 'LISTEN' } else { 'closed' })   面板 $webPort  $(if (Test-PortOpen $webPort) { 'LISTEN' } else { 'closed' })"

    foreach ($name in @("proxy", "web")) {
        $log = Get-LogName $name
        if (Test-Path $log) {
            Write-Host ""
            Write-Host "  --- $name 日志末尾 ---" -ForegroundColor DarkGray
            Get-Content $log -Tail 6 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        }
    }

    if ($pp -gt 0 -and (Test-Path $Fx)) {
        Write-Host ""
        Write-Host "  --- 账号额度（联网查询）---" -ForegroundColor DarkGray
        Push-Location $Root
        try { & $Fx -c $CfgPath status } catch { Warn "查询失败: $($_.Exception.Message)" } finally { Pop-Location }
    }
}

switch ($Action) {
    "start"   { Start-All }
    "stop"    { Stop-All }
    "restart" { Stop-All; Start-Sleep -Milliseconds 500; Start-All }
    "status"  { Show-Status }
}

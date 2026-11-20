<#
.SYNOPSIS
    fxproxy 进程控制：start / stop / restart / status（Windows）。

.DESCRIPTION
    代理和面板各起一个 fxproxy.exe，PID 写进 .run\*.pid，日志写进 .run\*.log。
    被 一键启动.bat / 一键停止.bat / 一键状态.bat 调用，也可直接：
        powershell -NoProfile -ExecutionPolicy Bypass -File ctl.ps1 start
#>
[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status",
    [switch]$Open,
    # status -Watch：常驻刷新，Ctrl+C 或关窗口退出，不影响后台服务
    [switch]$Watch,
    [int]$Interval = 3,
    # status -Deep：额外联网查账号额度（慢，Watch 模式下默认不查）
    [switch]$Deep
)

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Venv    = Join-Path $Root ".venv"
$RunDir  = Join-Path $Root ".run"
$CfgPath = Join-Path $Root "config.json"
$Fx      = Join-Path $Venv "Scripts\fxproxy.exe"
# pythonw.exe 是 GUI 子系统，起起来不带控制台窗口 —— 服务进程要的就是这个。
# fxproxy.exe 是 console 脚本，即使 -WindowStyle Hidden 也会附着一个控制台。
$PyW     = Join-Path $Venv "Scripts\pythonw.exe"
$PyC     = Join-Path $Venv "Scripts\python.exe"

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

# 等端口就绪。比固定 Sleep 800ms 靠谱：低配机器/首次 import 要几秒。
function Wait-Ready([int]$Port, [int]$Seconds = 10) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortOpen $Port) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

function Show-Tail([string]$Name, [int]$Lines = 12) {
    foreach ($f in @((Get-LogName $Name), (Join-Path $RunDir "$Name.err.log"))) {
        if (-not (Test-Path $f)) { continue }
        $t = @(Read-LogLines $f $Lines)
        if ($t.Count -eq 0) { continue }
        Write-Host "      --- $([IO.Path]::GetFileName($f)) ---" -ForegroundColor DarkGray
        $t | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    }
}

# 日志编码不能写死：桌面双击、工具会话、计划任务三条启动路径的 locale 不一样，
# python 按 locale 写重定向日志，同一天里会出现 UTF-8 和 GBK 混着来。
# 严格 UTF-8 解码成功就按 UTF-8，遇到非法字节退回系统 ANSI（中文机是 GBK）。
function Read-LogLines([string]$Path, [int]$Lines) {
    # 日志正被 pythonw 的 stdout 重定向持有，普通 ReadAllBytes 会撞共享冲突。
    $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $ms = New-Object IO.MemoryStream
        try { $fs.CopyTo($ms); $bytes = $ms.ToArray() }
        finally { $ms.Dispose() }
    } finally { $fs.Dispose() }
    $strict = [Text.UTF8Encoding]::new($false, $true)
    try { $text = $strict.GetString($bytes) }
    catch { $text = [Text.Encoding]::Default.GetString($bytes) }
    $all = $text -split "`r?`n"
    if ($all.Count -gt $Lines) { $all = $all[($all.Count - $Lines)..($all.Count - 1)] }
    return @($all | ForEach-Object { $_.TrimEnd("`r") })
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

# venv 里的 pythonw.exe 是个 launcher stub：它自己会再拉起真正的解释器，
# PID 文件记的是外层 stub。外层先退、解释器继续监听，只看 PID 文件就会
# 误报"未运行"（端口明明 LISTEN）。所以判活一律按命令行扫。
# $Kind：run = 代理，web = 面板。
function Get-SvcProcs([string]$Kind) {
    $out = @()
    $procs = Get-CimInstance Win32_Process -Filter "Name='fxproxy.exe' OR Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    foreach ($pr in $procs) {
        if (-not $pr.CommandLine) { continue }
        if ($pr.CommandLine -notmatch '-m\s+fxproxy') { continue }
        if ($pr.CommandLine -notmatch ('\s' + $Kind + '(\s|$)')) { continue }
        # 同机可能有多套部署（E:\fxproxy + 仓库里一份），只认本目录的
        if ($pr.CommandLine -notmatch [regex]::Escape($Root)) { continue }
        $out += $pr
    }
    return $out
}

# --------------------------------------------------------------- start
# 启动一个后台服务进程。用 pythonw.exe -m fxproxy：GUI 子系统，不附着控制台，
# 所以任务栏/桌面上不会出现任何命令行窗口（python.exe 即使 -WindowStyle Hidden
# 也会先创建一个控制台再隐藏，某些场景下仍会闪一下）。
function Start-Svc([string]$Name, [string[]]$ArgList) {
    $log = Get-LogName $Name
    $err = Join-Path $RunDir "$Name.err.log"
    # 把子进程 stdout/stderr 固定成 UTF-8，不管父进程是 cmd / PS / 计划任务。
    # Start-Process 会继承当前进程环境，所以改这里就够了。
    $env:PYTHONIOENCODING = "utf-8"
    if (Test-Path $PyW) {
        $exe  = $PyW
        # -u 关掉 stdout 缓冲。pythonw 没有控制台，stdout 是块缓冲的，
        # 进程一被结束时最后几 KB 日志全丢 —— 出错时只能看到 0 字节空文件。
        $args = @("-u", "-m", "fxproxy") + $ArgList
    } else {
        # 老 venv 里没 pythonw 才退回 console 脚本，此时靠 WindowStyle 隐藏
        $exe  = $Fx
        $args = $ArgList
    }
    # Start-Process 的 -ArgumentList 是数组时会自己拼命令行，但不会给带空格的
    # 元素加引号 —— 装在 "C:\Program Files\..." 下就会被拆成两段。自己拼。
    $argLine = ($args | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join " "
    $p = Start-Process -FilePath $exe -ArgumentList $argLine -WorkingDirectory $Root `
         -WindowStyle Hidden -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
    $p.Id | Out-File -FilePath (Get-PidFile $Name) -Encoding ascii -NoNewline
    return $p
}

function Start-All {
    # 注意：这里一律用 return 不用 exit —— 本脚本会被 install.ps1 以 & 调用，
    # 脚本内的 exit 会直接终止整个 PowerShell 进程，把安装流程也带走。
    if (-not (Test-Path $PyW) -and -not (Test-Path $Fx)) { Bad "没装好：找不到 $Venv\Scripts\pythonw.exe，先跑 install.bat"; return }
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
        Warn "在面板「添加账号」里导入后，再点一次 一键启动.bat"
    } else {
        $live = @(Get-SvcProcs "run")
        if ($live.Count -gt 0) {
            Ok "代理已在运行 (PID $((($live | ForEach-Object { $_.ProcessId }) -join ',')))"
        } else {
            if (Test-PortOpen $httpPort) { Bad "端口 $httpPort 被别的程序占着：面板「运行状态 → 端口」改掉，或改 config.json 的 http_port"; return }
            if (Test-PortOpen $socksPort) { Bad "端口 $socksPort 被别的程序占着：面板「运行状态 → 端口」改掉，或改 config.json 的 socks_port"; return }
            # -c 是顶层全局参数，必须放在子命令（run）前面
            $p = Start-Svc "proxy" @("-c", $CfgPath, "run")
            if (Wait-Ready $httpPort 10) {
                Ok "代理已启动  HTTP:$httpPort  SOCKS5:$socksPort"
            } elseif ($p.HasExited) {
                # 端口没起来 + 进程已经退了 = 配置/依赖错，比单纯"超时"好定位
                Bad "代理启动即退出 (exit $($p.ExitCode))，日志如下"
                Show-Tail "proxy" 15
                return
            } else {
                Bad "代理 10 秒内没起来，日志如下"
                Show-Tail "proxy" 15
                return
            }
        }
    }

    # ---- 面板
    $liveWeb = @(Get-SvcProcs "web")
    if ($liveWeb.Count -gt 0) {
        Ok "面板已在运行 (PID $((($liveWeb | ForEach-Object { $_.ProcessId }) -join ',')))"
    } else {
        if (Test-PortOpen $webPort) { Bad "面板端口 $webPort 被别的程序占着：重跑 install.bat 换 -WebPort，或关掉占用者"; return }
        $p = Start-Svc "web" @("-c", $CfgPath, "web", "--no-open", "--web-port", "$webPort")
        if (Wait-Ready $webPort 10) {
            Ok "面板已启动  http://127.0.0.1:$webPort"
        } elseif ($p.HasExited) {
            Bad "面板启动即退出 (exit $($p.ExitCode))，日志如下"
            Show-Tail "web" 15
        } else {
            Bad "面板 10 秒内没起来，日志如下"
            Show-Tail "web" 15
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
        $pf = Get-PidFile $name
        if (Test-Path -LiteralPath $pf) {
            Remove-Item -LiteralPath $pf -Force -ErrorAction SilentlyContinue
        }
    }
    # pythonw 是 launcher stub，PID 文件记的是外层；外层退了真正的解释器
    # 还在监听。杀掉之后的残留必须按命令行扫，扫两轮（stub 可能又把子进程拉起来）。
    foreach ($round in 1..2) {
        Start-Sleep -Milliseconds 400
        $n = 0
        foreach ($kind in @("run", "web")) {
            foreach ($pr in @(Get-SvcProcs $kind)) {
                Stop-Process -Id $pr.ProcessId -Force -ErrorAction SilentlyContinue
                Ok "清理残留 $kind PID $($pr.ProcessId)"
                $killed++; $n++
            }
        }
        if ($n -eq 0) { break }
    }
    if ($killed -eq 0) { Warn "本来就没在跑" }
}

# --------------------------------------------------------------- status
function Render-Status([bool]$DeepQuery, [string]$Footer) {
    $cfg = Read-Cfg
    if (-not $cfg) { Bad "缺 config.json"; return }
    $httpPort = 8080; $socksPort = 1080; $webPort = 8765
    if ($cfg.http_port)  { $httpPort  = [int]$cfg.http_port }
    if ($cfg.socks_port) { $socksPort = [int]$cfg.socks_port }
    if ($cfg.web_port)   { $webPort   = [int]$cfg.web_port }

    $tokens = @($cfg.refresh_tokens | Where-Object { $_ })
    Write-Host ""
    Write-Host "  fxproxy 状态   $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor White
    Write-Host "  账号     $($tokens.Count) 个   国家  $(if ($cfg.country) { $cfg.country } else { '任意' })"

    # 进程判活按命令行扫，不认 PID 文件：stub 退了进程还在的情况很常见。
    $proxyLive = @(Get-SvcProcs "run")
    $webLive   = @(Get-SvcProcs "web")
    if ($proxyLive.Count -gt 0) {
        Ok "代理进程 PID $((($proxyLive | ForEach-Object { $_.ProcessId }) -join ','))"
    } else { Bad "代理未运行" }
    if ($webLive.Count -gt 0) {
        Ok "面板进程 PID $((($webLive | ForEach-Object { $_.ProcessId }) -join ','))"
    } else { Bad "面板未运行" }

    Write-Host "  端口     HTTP $httpPort  $(if (Test-PortOpen $httpPort) { 'LISTEN' } else { 'closed' })   SOCKS5 $socksPort  $(if (Test-PortOpen $socksPort) { 'LISTEN' } else { 'closed' })   面板 $webPort  $(if (Test-PortOpen $webPort) { 'LISTEN' } else { 'closed' })"
    if (Test-PortOpen $webPort) {
        Write-Host "  面板     http://127.0.0.1:$webPort" -ForegroundColor Cyan
        Write-Host "  订阅     http://127.0.0.1:$webPort/clash.yaml" -ForegroundColor DarkGray
    }

    foreach ($name in @("proxy", "web")) {
        $log = Get-LogName $name
        if (Test-Path $log) {
            $tail = @(Read-LogLines $log 6)
            Write-Host ""
            Write-Host "  --- $name 日志末尾 ---" -ForegroundColor DarkGray
            if ($tail.Count -gt 0) {
                $tail | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
            } else {
                # 服务跑在 pythonw 上（没有控制台），正常运行时 stdout 是空的；
                # 真出问题看 err.log，或者用一键启动.bat 的窗口观察。
                Write-Host "  （暂无输出；异常看 $RunDir\$name.err.log）" -ForegroundColor DarkGray
            }
        }
    }

    if ($DeepQuery -and $proxyLive.Count -gt 0 -and (Test-Path $Fx)) {
        Write-Host ""
        Write-Host "  --- 账号额度（联网查询）---" -ForegroundColor DarkGray
        Push-Location $Root
        try { & $Fx -c $CfgPath status } catch { Warn "查询失败: $($_.Exception.Message)" } finally { Pop-Location }
    }

    if ($Footer) {
        Write-Host ""
        Write-Host "  $Footer" -ForegroundColor DarkGray
    }
}

function Show-Status {
    if (-not $Watch) { Render-Status ([bool]$Deep) ""; return }
    # 常驻刷新：只读进程/端口/日志，不碰服务。关窗口或 Ctrl+C 都不会影响后台服务。
    try {
        while ($true) {
            try { Clear-Host } catch { }
            Render-Status ([bool]$Deep) "每 $Interval 秒刷新 · 关闭本窗口或按 Ctrl+C 退出，不影响后台服务"
            Start-Sleep -Seconds ([Math]::Max(1, $Interval))
        }
    } finally {
        Write-Host ""
        Write-Host "  已退出状态窗口，服务仍在后台运行。" -ForegroundColor DarkGray
    }
}

switch ($Action) {
    "start"   { Start-All }
    "stop"    { Stop-All }
    "restart" { Stop-All; Start-Sleep -Milliseconds 500; Start-All }
    "status"  { Show-Status }
}

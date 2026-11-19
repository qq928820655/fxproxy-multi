<#
.SYNOPSIS
    fxproxy Windows 一键部署（由 install.bat 调用，也可直接跑）。

.DESCRIPTION
    做五件事：检查/安装 Python -> 建 venv -> 装依赖 -> 写 config.json -> 起代理与面板。
    凭证来源：插件导出文件 / Cookie 文件 / 直接贴 refresh_token / 稍后在面板添加。

.PARAMETER Country
    出口国家 ISO 码（US / GB / DE …）。留空 = 任意国家。

.PARAMETER HttpPort / SocksPort / WebPort
    监听端口。留空(0) 时：已存在的 config.json 保留原值，否则交互询问，默认 8080/1080/8765。

.PARAMETER CookieFile
    插件导出 / Cookie 文件路径。给了就自动导入，不再交互询问凭证。

.PARAMETER IndexUrl
    pip 镜像地址，例如 https://pypi.tuna.tsinghua.edu.cn/simple 。留空用官方源（失败会自动回退清华源）。

.PARAMETER Proxy
    给 pip 用的代理，例如 http://127.0.0.1:7890 。留空继承系统环境变量。
    系统代理填错（指向已关闭的端口）时 pip 会反复超时，用这个覆盖或 -NoProxy 绕过。

.PARAMETER NoProxy
    忽略系统代理环境变量直连（墙内直连 PyPI 通常不通，一般配 -IndexUrl 一起用）。

.PARAMETER SkipLogin
    跳过凭证导入（稍后在面板里加）。

.PARAMETER SkipLoginDeps
    不装 pyfxa / curl_cffi（只有「sessionToken 换 refresh_token」需要，纯 refresh_token 不需要）。

.PARAMETER NoStart
    装完不启动。

.PARAMETER Autostart
    注册登录时自动启动（schtasks，用户级，不需要管理员）。

.PARAMETER Uninstall
    停止进程、删 venv/.run/计划任务。config.json 里的凭证保留。

.EXAMPLE
    install.bat -Country US -Autostart
    install.bat -CookieFile D:\export.json -NoStart
    install.bat -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Country = "",
    [int]$HttpPort = 0,
    [int]$SocksPort = 0,
    [int]$WebPort = 0,
    [string]$CookieFile = "",
    [string]$IndexUrl = "",
    [string]$Proxy = "",
    [switch]$NoProxy,
    [switch]$SkipLoginDeps,
    [switch]$SkipLogin,
    [switch]$NoStart,
    [switch]$Autostart,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Venv    = Join-Path $Root ".venv"
$RunDir  = Join-Path $Root ".run"
$CfgPath = Join-Path $Root "config.json"
$TaskName = "fxproxy-autostart"

function Say($m)  { Write-Host $m }
function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !    $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "    X    $m" -ForegroundColor Red; exit 1 }

function Write-NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Test-PortBusy([int]$Port) {
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $Port); return $true }
    catch { return $false }
    finally { $c.Dispose() }
}

# ------------------------------------------------------------------ 卸载
if ($Uninstall) {
    Step "卸载 fxproxy"
    & (Join-Path $PSScriptRoot "ctl.ps1") stop 2>$null | Out-Null
    schtasks /delete /tn $TaskName /f 2>$null | Out-Null
    if (Test-Path $Venv)   { Remove-Item -Recurse -Force $Venv;   Ok "已删除 .venv" }
    if (Test-Path $RunDir) { Remove-Item -Recurse -Force $RunDir; Ok "已删除 .run（日志/PID）" }
    if (Test-Path $CfgPath) { Warn "config.json 已保留（含凭证）：$CfgPath" }
    Ok "卸载完成"
    exit 0
}

Write-Host ""
Write-Host "  fxproxy Windows 一键部署" -ForegroundColor White
Write-Host "  项目目录: $Root" -ForegroundColor DarkGray

if (-not (Test-Path (Join-Path $Root "pyproject.toml"))) {
    Die "没找到 pyproject.toml，确认本脚本在 <repo>\deploy\windows\ 下"
}

# ------------------------------------------------------- 1. Python
Step "1/5 检查 Python (>= 3.10)"

function Test-PyCandidate([string]$Exe, [string[]]$ArgsList) {
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $null }
    try {
        $out = & $Exe @($ArgsList + @("-c", "import sys;print('%d.%d.%d'%sys.version_info[:3])")) 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $ver = ([string]($out | Select-Object -First 1)).Trim()
    $p = $ver.Split(".")
    if ($p.Count -lt 2) { return $null }
    if ([int]$p[0] -ne 3) { return $null }
    return @{ Exe = $Exe; Args = $ArgsList; Version = $ver; Minor = [int]$p[1] }
}

function Get-PythonLauncher {
    # 先找 3.10–3.13：curl_cffi / pyfxa 在这些版本上有预编译 wheel。
    # 3.14 排最后 —— 它能跑代理，只是登录依赖大概装不上。
    $order = @(
        @{ Exe = "py";     Args = @("-3.10") },
        @{ Exe = "py";     Args = @("-3.12") },
        @{ Exe = "py";     Args = @("-3.11") },
        @{ Exe = "py";     Args = @("-3.13") },
        @{ Exe = "py";     Args = @("-3")    },
        @{ Exe = "python"; Args = @()        },
        @{ Exe = "python3"; Args = @()       }
    )
    foreach ($c in $order) {
        $r = Test-PyCandidate $c.Exe $c.Args
        if ($r -and $r.Minor -ge 10) { return $r }
    }
    return $null
}

$py = Get-PythonLauncher

if (-not $py) {
    Warn "未找到 Python 3.10+，尝试用 winget 安装 Python 3.12"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            & winget install --id Python.Python.3.12 -e --silent `
                --accept-package-agreements --accept-source-agreements
        } catch {
            Warn "winget 安装失败: $($_.Exception.Message)"
        }
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User")
        $py = Get-PythonLauncher
    }
}

if (-not $py) {
    Write-Host ""
    Die @"
没有可用的 Python 3.10+。手动装一个再重跑：
  https://www.python.org/downloads/windows/
安装时勾上 "Add python.exe to PATH"。
"@
}
Ok "Python $($py.Version)  ($($py.Exe) $($py.Args -join ' '))"
if ($py.Minor -ge 14) {
    Warn "Python $($py.Version) 上 curl_cffi / pyfxa 通常没有预编译 wheel，登录依赖可能装不上"
    Warn "只用插件导出的 refresh_token 的话不受影响；想装齐就装个 3.12 再跑一次"
}

# ------------------------------------------------------- 2. venv
Step "2/5 准备虚拟环境 .venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$pyShort = (& $py.Exe @($py.Args + @("-c", "import sys;print('%d.%d'%sys.version_info[:2])")) 2>$null | Select-Object -First 1)
$pyShort = ([string]$pyShort).Trim()

if (Test-Path $VenvPy) {
    $venvShort = ""
    try { $venvShort = ([string](& $VenvPy -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null | Select-Object -First 1)).Trim() } catch { }
    if ($venvShort -and $venvShort -ne $pyShort) {
        Warn "已有 venv 是 Python $venvShort，当前选中 $pyShort，重建（旧 venv 删掉）"
        Remove-Item -Recurse -Force $Venv
    }
}

if (Test-Path $VenvPy) {
    Ok "已存在，复用"
} else {
    & $py.Exe @($py.Args + @("-m", "venv", $Venv))
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) { Die "创建 venv 失败" }
    Ok "已创建 $Venv（Python $pyShort）"
}

# ------------------------------------------------------- 3. 依赖
Step "3/5 安装依赖"

if ($NoProxy) {
    foreach ($n in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")) {
        [Environment]::SetEnvironmentVariable($n, "", "Process")
    }
    [Environment]::SetEnvironmentVariable("NO_PROXY", "*", "Process")
    Say "    已忽略系统代理（-NoProxy）"
} elseif ($Proxy) {
    foreach ($n in @("HTTP_PROXY", "HTTPS_PROXY")) {
        [Environment]::SetEnvironmentVariable($n, $Proxy, "Process")
    }
    Say "    pip 走代理: $Proxy"
}

# 超时 + 重试：系统代理配错时 pip 会默认为长时间挂起，这里压到 20s x2
$pipCommon = @("-m", "pip", "install", "--disable-pip-version-check",
               "--timeout", "20", "--retries", "2")
if ($Proxy) { $pipCommon += @("--proxy", $Proxy) }

$pipSrc = @()
if ($IndexUrl) { $pipSrc = @("-i", $IndexUrl) }

# 装一组包；默认源失败时自动回退清华镜像再来一次
function Pip-Install([string[]]$Targets, [string]$Label) {
    & $VenvPy @($script:pipCommon + $script:pipSrc + @("-q") + $Targets)
    if ($LASTEXITCODE -eq 0) { return $true }
    if ($script:IndexUrl) { return $false }
    Warn "$Label：默认源失败，换清华镜像重试"
    $fb = $script:pipCommon + @("-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "-q") + $Targets
    & $VenvPy @fb
    return ($LASTEXITCODE -eq 0)
}

Push-Location $Root
try {
    Pip-Install @("-U", "pip") "升级 pip" | Out-Null
    if (-not (Pip-Install @("-e", ".") "基础依赖")) {
        Die "安装基础依赖失败。网络不通时加 -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple ，或 -Proxy http://127.0.0.1:7890"
    }
    Ok "基础依赖已装（requests / aiohttp）"

    if (-not $SkipLoginDeps) {
        Say "    安装登录依赖 pyfxa + curl_cffi（拿 sessionToken 换 refresh_token 时才需要）"
        if (Pip-Install @("pyfxa>=0.8", "curl_cffi>=0.7") "登录依赖") {
            Ok "登录依赖已装"
        } else {
            Warn "登录依赖装不上（Python 3.13/3.14 无预编译 wheel，或网络受限）。继续——"
            Warn "只要你导入的是 refresh_token（插件导出的就是），不影响使用。"
        }
    }
} finally {
    Pop-Location
}

$Fx = Join-Path $Venv "Scripts\fxproxy.exe"
if (-not (Test-Path $Fx)) { $Fx = $null }
if (-not $Fx) { Die "没生成 fxproxy.exe，检查 pip install -e . 的输出" }

# ------------------------------------------------------- 4. config
Step "4/5 写配置 $CfgPath"

$tokens = @()
$oldCountry = ""
$oldHttp = 0; $oldSocks = 0; $oldWeb = 0
$firstRun = -not (Test-Path $CfgPath)
if (-not $firstRun) {
    try {
        $old = Get-Content $CfgPath -Raw | ConvertFrom-Json
        $tokens = @($old.refresh_tokens | Where-Object { $_ })
        if (-not $tokens -and $old.refresh_token) { $tokens = @([string]$old.refresh_token) }
        $oldCountry = [string]$old.country
        if ($old.http_port)  { $oldHttp  = [int]$old.http_port }
        if ($old.socks_port) { $oldSocks = [int]$old.socks_port }
        if ($old.web_port)   { $oldWeb   = [int]$old.web_port }
    } catch { Warn "旧 config.json 解析失败，按新配置重写" }
    Ok "已有 config.json：$($tokens.Count) 个账号"
}

# 国家：参数 > 旧值 > 交互
if (-not $Country) {
    if ($oldCountry) { $Country = $oldCountry }
    elseif ($firstRun) {
        Say ""
        Say "  出口国家：US / GB / DE / JP … 只写两个字母；直接回车 = 任意国家（由节点决定）"
        $Country = (Read-Host "  国家代码").Trim().ToUpper()
    }
}

# 端口：参数 > 旧值 > 默认(交互确认)
function Ask-Port([string]$Label, [int]$Given, [int]$Old, [int]$Default) {
    if ($Given -gt 0) { return $Given }
    if ($Old -gt 0)   { return $Old }
    if (-not $script:firstRun) { return $Default }
    while ($true) {
        $ans = Read-Host "  $Label (默认 $Default)"
        if (-not $ans) { $p = $Default } else {
            $p = 0
            if (-not [int]::TryParse($ans, [ref]$p)) { Warn "不是数字，重来"; continue }
        }
        if (Test-PortBusy $p) { Warn "端口 $p 已被占用，换一个" ; continue }
        return $p
    }
}
$HttpPort  = Ask-Port "HTTP 代理端口"  $HttpPort  $oldHttp  8080
$SocksPort = Ask-Port "SOCKS5 端口"     $SocksPort $oldSocks 1080
$WebPort   = Ask-Port "面板/订阅端口"   $WebPort   $oldWeb   8765

# 凭证
if (-not $SkipLogin -and $CookieFile) {
    Step "导入凭证：$CookieFile"
    & $Fx -c $CfgPath login --cookie-file $CookieFile --append
    if ($LASTEXITCODE -ne 0) { Warn "导入失败，稍后可在面板里重试" } else { Ok "已导入" }
} elseif (-not $SkipLogin -and $firstRun) {
    Say ""
    Say "  凭证导入方式："
    Say "    1  用插件导出文件 / Cookie 文件导入（可多账号）"
    Say "    2  直接贴 refresh_token（多个用逗号分隔）"
    Say "    3  跳过，稍后在面板里添加"
    $choice = (Read-Host "  选择 [1/2/3]，默认 3").Trim()
    if ($choice -eq "1") {
        $p = (Read-Host "  文件路径").Trim('"', ' ')
        if ($p -and (Test-Path $p)) {
            & $Fx -c $CfgPath login --cookie-file $p --append
            if ($LASTEXITCODE -ne 0) { Warn "导入失败，稍后在面板重试" }
        } else { Warn "文件不存在，跳过" }
    } elseif ($choice -eq "2") {
        $raw = (Read-Host "  refresh_token（逗号分隔）").Trim()
        foreach ($t in ($raw -split "[,;\s]+")) {
            $t = $t.Trim()
            if ($t -match '^[A-Za-z0-9_\-]{16,}$') { $tokens += $t }
        }
        if ($tokens.Count -eq 0) { Warn "没读到合法 token，稍后在面板添加" } else { Ok "记下 $($tokens.Count) 个 token" }
    }
}

# 重新读一次（login 可能刚写进去 token）
if (Test-Path $CfgPath) {
    try {
        $now = Get-Content $CfgPath -Raw | ConvertFrom-Json
        $disk = @($now.refresh_tokens | Where-Object { $_ })
        if ($disk.Count -gt 0) { $tokens = $disk }
    } catch { }
}
$tokens = @($tokens | Select-Object -Unique)
$Country = ($Country -replace '[^A-Za-z]', '').ToUpper()

$tokenJson = "[]"
if ($tokens.Count -gt 0) {
    $quoted = $tokens | ForEach-Object { '"' + $_ + '"' }
    $tokenJson = "[" + ($quoted -join ", ") + "]"
}
$first = ""
if ($tokens.Count -gt 0) { $first = $tokens[0] }

# 用 here-string 而不是 @( ... ) -join：数组字面量里 'a' + $x + 'b' 会被逗号拆成
# 多个元素，join 出来每行中间多一个换行，JSON 就坏了。
$cfgText = @"
{
  "refresh_token": "$first",
  "refresh_tokens": $tokenJson,
  "pool_strategy": "round-robin",
  "quota_cache_ttl": 60,
  "cooldown_seconds": 300,
  "country": "$Country",
  "host": "127.0.0.1",
  "http_port": $HttpPort,
  "socks_port": $SocksPort,
  "web_port": $WebPort,
  "failover": 3,
  "sub_server": "",
  "sub_http_port": 0,
  "sub_socks_port": 0
}
"@
Write-NoBom $CfgPath $cfgText
Ok "已写入（国家=$(if ($Country) { $Country } else { '任意' })，端口 $HttpPort/$SocksPort，面板 $WebPort）"

if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir | Out-Null }

# ------------------------------------------------------- 5. 启动
Step "5/5 启动"
if ($NoStart) {
    Warn "已跳过启动（-NoStart）。手动起：deploy\windows\start.bat"
} else {
    & (Join-Path $PSScriptRoot "ctl.ps1") start -Open
}

if ($Autostart) {
    Step "注册登录自启"
    $ctl = Join-Path $PSScriptRoot "ctl.bat"
    $tr = 'cmd /c ""' + $ctl + '" start"'
    schtasks /create /tn $TaskName /tr $tr /sc onlogon /rl limited /f 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "已注册计划任务 $TaskName" } else { Warn "注册失败，可手动：计划任务 -> 登录时运行 $ctl start" }
}

Write-Host ""
Write-Host "  完成" -ForegroundColor Green
Write-Host "  面板      http://127.0.0.1:$WebPort" -ForegroundColor White
Write-Host "  代理      HTTP 127.0.0.1:$HttpPort   SOCKS5 127.0.0.1:$SocksPort"
Write-Host "  订阅      http://127.0.0.1:$WebPort/clash.yaml"
Write-Host ""
Write-Host "  起停:  deploy\windows\start.bat / stop.bat / status.bat" -ForegroundColor DarkGray
Write-Host "  日志:  .run\proxy-YYYYMMDD.log  .run\proxy.err.log" -ForegroundColor DarkGray
Write-Host ""

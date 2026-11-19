<#
.SYNOPSIS
    fxproxy Windows 在线一键部署：下载发行包 -> 解压 -> 建 venv -> 起服务。

.DESCRIPTION
    Windows 上不能用 `curl -fsSL ... | bash`，两个原因叠在一起：
      1) PowerShell 里 curl 是 Invoke-WebRequest 的别名，没有 -fsSL 这套参数，
         直接报「找不到与参数名称"fsSL"匹配的参数」；
      2) 就算改用 curl.exe 把它下下来喂给 bash，deploy/linux/bootstrap.sh 是 Linux
         安装逻辑（systemd、/opt、apt/dnf），在 Windows 上必然失败。
    这个脚本是那条命令的 Windows 版，干的事一样：取制品 -> 解压 -> 装 -> 起。

    一行装完（PowerShell 窗口里粘贴）：
      irm https://raw.githubusercontent.com/qq928820655/fxproxy-multi/main/deploy/windows/bootstrap.ps1 -OutFile $env:TEMP\fx-boot.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\fx-boot.ps1

    已经拿到 zip 的，直接用同目录的 install.bat，不必走网络。

.PARAMETER Dir
    安装目录。默认 %LOCALAPPDATA%\fxproxy。已有的 config.json / .venv / .run 不会被覆盖。

.PARAMETER Version
    指定版本号，如 0.4.1。留空取最新 Release。

.PARAMETER GhProxy
    GitHub 镜像前缀，如 https://ghproxy.net/ 。直连失败时脚本也会自己试几个公共镜像。

.PARAMETER Proxy
    透传给安装器的 pip 代理，如 http://127.0.0.1:7890 。

.PARAMETER IndexUrl
    透传给安装器的 pip 镜像。

.PARAMETER Uninstall
    停止进程、删 venv/.run/计划任务（config.json 保留）。不联网。

.EXAMPLE
    bootstrap.bat
    bootstrap.bat -Country US -Shortcut
    bootstrap.bat -NoStart -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
    bootstrap.bat -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Dir = "",
    [string]$Version = "",
    [string]$GhProxy = "",
    [string]$Proxy = "",
    [switch]$NoProxy,
    [string]$IndexUrl = "",
    [string]$Country = "",
    [string]$CookieFile = "",
    [int]$HttpPort = 0,
    [int]$SocksPort = 0,
    [int]$WebPort = 0,
    [switch]$SkipLogin,
    [switch]$SkipLoginDeps,
    [switch]$NoStart,
    [switch]$NoShortcut,
    [switch]$Autostart,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

$Repo   = if ($env:FXPROXY_REPO)   { $env:FXPROXY_REPO }   else { "qq928820655/fxproxy-multi" }
$Branch = if ($env:FXPROXY_BRANCH) { $env:FXPROXY_BRANCH } else { "main" }
if (-not $GhProxy -and $env:FXPROXY_GH_PROXY) { $GhProxy = $env:FXPROXY_GH_PROXY }
if (-not $Dir) {
    if ($env:LOCALAPPDATA) { $Dir = Join-Path $env:LOCALAPPDATA "fxproxy" }
    else { $Dir = Join-Path $HOME "fxproxy" }
}

function Say($m)  { Write-Host "[fxproxy] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !    $m" -ForegroundColor Yellow }
function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Die($m)  { Write-Host "    X    $m" -ForegroundColor Red; exit 1 }
function Rule()   { Write-Host ("-" * 58) -ForegroundColor DarkGray }

# ---------------------------------------------------------------- 下载
# 直连优先；失败才挂镜像重试。镜像前缀是拼接式的（https://ghproxy.net/https://github.com/...）。
function Get-Prefixes {
    if ($GhProxy) { return @($GhProxy, "") }
    return @("", "https://ghproxy.net/", "https://gh-proxy.com/")
}

function Try-Fetch([string]$Url, [string]$OutFile, [string[]]$Prefixes) {
    foreach ($pre in $Prefixes) {
        $u = $Url
        if ($pre) { $u = $pre.TrimEnd('/') + '/' + $Url }
        if (Test-Path $OutFile) { Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue }
        Write-Host "    GET  $u" -ForegroundColor DarkGray

        # 这里必须是 curl.exe：PowerShell 里的裸 curl 是 Invoke-WebRequest 别名。
        $exe = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($exe) {
            & $exe.Source -fsSL --retry 2 --connect-timeout 15 --max-time 600 -o $OutFile $u 2>$null
            $rc = $LASTEXITCODE
            if ($rc -eq 0 -and (Test-Path $OutFile) -and (Get-Item -LiteralPath $OutFile).Length -gt 16) { return $true }
            continue
        }
        try {
            Invoke-WebRequest -Uri $u -OutFile $OutFile -UseBasicParsing -TimeoutSec 600
            if ((Test-Path $OutFile) -and (Get-Item -LiteralPath $OutFile).Length -gt 16) { return $true }
        } catch {
            Write-Host "         $($_.Exception.Message)" -ForegroundColor DarkGray
        }
    }
    return $false
}

function Get-ReleaseAsset {
    # 返回 @{ Version = "v0.4.1"; Url = "..."; Name = "..." }；取不到返回 $null
    if ($Version) {
        $v = $Version.TrimStart("v", "V")
        $u = "https://github.com/$Repo/releases/download/v$v/fxproxy-$v-win.zip"
        return @{ Version = "v$v"; Url = $u; Name = "fxproxy-$v-win.zip"; Direct = $true }
    }
    $api = if ($env:FXPROXY_UPDATE_API) { $env:FXPROXY_UPDATE_API } else { "https://api.github.com/repos/$Repo/releases/latest" }
    $tmp = Join-Path $script:Work "rel.json"
    if (-not (Try-Fetch $api $tmp (Get-Prefixes))) { return $null }
    try {
        $j = Get-Content -LiteralPath $tmp -Raw -Encoding UTF8 | ConvertFrom-Json
        $a = @($j.assets) | Where-Object { $_.name -like "*-win.zip" } | Select-Object -First 1
        if ($a) {
            return @{ Version = $j.tag_name; Url = $a.browser_download_url; Name = $a.name; Direct = $false }
        }
    } catch {
        Warn "Release 信息解析失败：$($_.Exception.Message)"
    }
    return $null
}

# ---------------------------------------------------------------- 解压 / 覆盖
function Expand-ZipFile([string]$Zip, [string]$Dest) {
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    if (Get-Command Expand-Archive -ErrorAction SilentlyContinue) {
        Expand-Archive -LiteralPath $Zip -DestinationPath $Dest -Force
        return
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Dest)
}

function Find-Tree([string]$Dest) {
    $hit = Get-ChildItem -LiteralPath $Dest -Directory -Force |
        Where-Object { Test-Path (Join-Path $_.FullName "deploy\windows\install.ps1") } |
        Select-Object -First 1
    if ($hit) { return $hit.FullName }
    if (Test-Path (Join-Path $Dest "deploy\windows\install.ps1")) { return $Dest }
    return $null
}

function Sync-Tree([string]$Src, [string]$Dst, [switch]$KeepCfg) {
    New-Item -ItemType Directory -Force -Path $Dst | Out-Null
    Get-ChildItem -LiteralPath $Src -Force | ForEach-Object {
        # 运行态目录不动：覆盖掉等于把 venv 和 pid 文件一起打碎
        if ($_.Name -eq ".venv" -or $_.Name -eq ".run") { return }
        $target = Join-Path $Dst $_.Name
        if ($_.PSIsContainer) {
            Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse -Force
        } else {
            if ($KeepCfg -and $_.Name -eq "config.json" -and (Test-Path $target)) { return }
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
    }
}

# ---------------------------------------------------------------- 安装器调用
function Invoke-Installer([string]$Script, [string[]]$Extra) {
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Script) + $Extra
    & powershell.exe @args
    return $LASTEXITCODE
}

function Build-ForwardArgs {
    $f = @()
    if ($Country)      { $f += @("-Country", $Country) }
    if ($CookieFile)   { $f += @("-CookieFile", $CookieFile) }
    if ($IndexUrl)     { $f += @("-IndexUrl", $IndexUrl) }
    if ($Proxy)        { $f += @("-Proxy", $Proxy) }
    if ($NoProxy)      { $f += "-NoProxy" }
    if ($HttpPort)     { $f += @("-HttpPort", $HttpPort) }
    if ($SocksPort)    { $f += @("-SocksPort", $SocksPort) }
    if ($WebPort)      { $f += @("-WebPort", $WebPort) }
    if ($SkipLogin)    { $f += "-SkipLogin" }
    if ($SkipLoginDeps){ $f += "-SkipLoginDeps" }
    if ($NoStart)      { $f += "-NoStart" }
    if ($Autostart)    { $f += "-Autostart" }
    if (-not $NoShortcut) { $f += "-Shortcut" }
    return $f
}

function Show-Endpoints {
    $cfg = Join-Path $Dir "config.json"
    $h = 8080; $s = 1080; $w = 8765
    if (Test-Path $cfg) {
        try {
            $j = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($j.http_port)  { $h = [int]$j.http_port }
            if ($j.socks_port) { $s = [int]$j.socks_port }
            if ($j.web_port)   { $w = [int]$j.web_port }
        } catch { }
    }
    Write-Host ""
    Write-Host "  面板      http://127.0.0.1:$w" -ForegroundColor Green
    Write-Host "  HTTP 代理 127.0.0.1:$h"
    Write-Host "  SOCKS5    127.0.0.1:$s"
    Write-Host "  订阅      http://127.0.0.1:$w/clash.yaml"
    Write-Host "  目录      $Dir"
    Write-Host ""
}

# ---------------------------------------------------------------- 主流程
Rule
Write-Host "  fxproxy Windows 一键部署" -ForegroundColor White
Rule
Write-Host "  仓库   $Repo"
Write-Host "  目录   $Dir"
Write-Host "  PowerShell $($PSVersionTable.PSVersion)"
Rule

# 卸载：不联网，找到已装的那份直接调 install.ps1 -Uninstall
if ($Uninstall) {
    $inst = Join-Path $Dir "deploy\windows\install.ps1"
    if (-not (Test-Path $inst)) {
        foreach ($cand in @((Join-Path $env:LOCALAPPDATA "fxproxy"), (Join-Path $HOME "fxproxy"))) {
            if (Test-Path (Join-Path $cand "deploy\windows\install.ps1")) { $Dir = $cand; $inst = Join-Path $cand "deploy\windows\install.ps1"; break }
        }
    }
    if (-not (Test-Path $inst)) { Die "没找到已安装的 fxproxy（找过 $Dir）。没装过就不用卸载" }
    Say "卸载 $Dir"
    exit (Invoke-Installer $inst @("-Uninstall"))
}

$script:Work = Join-Path $env:TEMP ("fxproxy-boot-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $script:Work | Out-Null

try {
    # 1. 定位制品
    Step "定位发行包"
    $asset = Get-ReleaseAsset
    if ($asset) {
        Ok "$($asset.Version)  $($asset.Name)"
    } else {
        Warn "取不到 Release，改用 $Branch 分支源码包（没有预编译 muxserver，功能不受影响）"
        $asset = @{
            Version = "src-$Branch"
            Name    = "fxproxy-$Branch.zip"
            Url     = "https://codeload.github.com/$Repo/zip/refs/heads/$Branch"
            Direct  = $false
        }
    }

    # 2. 下载
    Step "下载"
    $zip = Join-Path $script:Work "pkg.zip"
    if (-not (Try-Fetch $asset.Url $zip (Get-Prefixes))) {
        Die "下载失败。检查网络，或用 -GhProxy https://ghproxy.net/ 指定镜像；也可以直接下载 zip 后跑 install.bat"
    }
    $size = [math]::Round((Get-Item -LiteralPath $zip).Length / 1MB, 1)
    Ok "已下载 $size MB"

    # 3. 解压 + 结构校验
    Step "解压"
    $raw = Join-Path $script:Work "x"
    try {
        Expand-ZipFile $zip $raw
    } catch {
        Die "解压失败（包可能没下全）：$($_.Exception.Message)"
    }
    $tree = Find-Tree $raw
    if (-not $tree) { Die "包里没有 deploy\windows\install.ps1，制品结构不对（可能下到了错误页）" }
    Ok "结构校验通过"

    # 4. 覆盖到安装目录（保留 config.json / .venv / .run）
    Step "写入 $Dir"
    $hadCfg = Test-Path (Join-Path $Dir "config.json")
    Sync-Tree $tree $Dir -KeepCfg
    if ($hadCfg) { Ok "已保留原有 config.json（端口与账号不变）" }
    else { Ok "首次安装" }

    # 5. 交给安装器
    Step "安装依赖并启动"
    $inst = Join-Path $Dir "deploy\windows\install.ps1"
    $fwd = Build-ForwardArgs
    Say "install.ps1 $($fwd -join ' ')"
    $rc = Invoke-Installer $inst $fwd
    if ($rc -ne 0) { Die "安装器退出码 $rc，往上翻日志" }
    Ok "安装完成"
    Show-Endpoints
} finally {
    if (Test-Path $script:Work) {
        Remove-Item -LiteralPath $script:Work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

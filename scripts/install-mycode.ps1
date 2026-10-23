<#
把 mycode 装到 Windows 的 PATH 上（用户级，不需要管理员）。

为什么要一个 shim：MyCode 与它的 kind 集群都跑在 WSL 发行版里，而人在 Windows 终端里
敲命令。shim 把命令转进发行版；「让发行版别在命令返回后被 WSL 拆掉」这件事由仓库里的
scripts/pin-wsl.sh 负责（start / prod / cloud-up 都会调它），不在这里重复实现。

用法（在仓库根目录，普通权限）：
    powershell -ExecutionPolicy Bypass -File scripts\install-mycode.ps1
发行版不是默认那个时：
    powershell -ExecutionPolicy Bypass -File scripts\install-mycode.ps1 -Distro Ubuntu-22.04
#>
[CmdletBinding()]
param(
    [string]$Distro = '',
    [string]$Repo = ''
)

$ErrorActionPreference = 'Stop'

function Fail([string]$msg) {
    Write-Host "[mycode] ❌ $msg" -ForegroundColor Red
    exit 1
}

# ── 仓库路径 ─────────────────────────────────────────────────────────────────
$repoWin = if ($Repo) { $Repo } else { Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path (Join-Path $repoWin 'mycode'))) {
    Fail "在 $repoWin 下找不到 mycode 启动器，请用 -Repo 指定仓库目录"
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { Fail '找不到 wsl.exe' }

# ── 发行版：优先 -Distro，其次 WSL 的默认发行版 ───────────────────────────────
# wsl.exe 的输出是 UTF-16，PowerShell 5.1 会把它读成夹着 NUL 的字符串，
# 所以先按「只留可打印 ASCII」洗一遍再解析——否则比较和 Trim 全都对不上。
function Clean-WslOutput([string[]]$lines) {
    return (($lines -join "`n") -replace "[^\x20-\x7E*]", '') -split "`n" |
        ForEach-Object { $_.Trim() } | Where-Object { $_ }
}

if (-not $Distro) {
    $verbose = Clean-WslOutput (& wsl.exe -l -v)
    $marked = $verbose | Where-Object { $_ -match '^\*' } | Select-Object -First 1
    if ($marked) {
        $Distro = ($marked -replace '^\*\s*', '') -split '\s+' | Select-Object -First 1
    }
    if (-not $Distro) {
        $quiet = Clean-WslOutput (& wsl.exe -l -q) | Where-Object { $_ -notmatch 'docker-desktop' }
        $Distro = $quiet | Select-Object -First 1
    }
}
if (-not $Distro) { Fail 'WSL 里没有可用发行版（docker-desktop 那个不算）' }
Write-Host "[mycode] 发行版：$Distro"

$repoWsl = ((& wsl.exe -d $Distro wslpath -u $repoWin) | Out-String).Trim()
if (-not $repoWsl) { Fail "wslpath 没能把 $repoWin 转成 WSL 路径" }
& wsl.exe -d $Distro -- bash -lc "test -f '$repoWsl/mycode'" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "在 $Distro 里看不到 $repoWsl/mycode" }
Write-Host "[mycode] 仓库（WSL 视角）：$repoWsl"

# ── 生成 shim ────────────────────────────────────────────────────────────────
$dir = Join-Path $env:USERPROFILE '.mycode'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$cmd = Join-Path $dir 'mycode.cmd'

$shim = @"
@echo off
rem 由 scripts\install-mycode.ps1 生成。要改请改那个脚本——这里会被覆盖。
setlocal EnableExtensions
set "MYCODE_DISTRO=$Distro"
set "MYCODE_REPO=$repoWsl"
wsl.exe -d %MYCODE_DISTRO% -- bash -lc "cd %MYCODE_REPO% && ./mycode %*"
exit /b %ERRORLEVEL%
"@
# 用 ANSI 而不是 ASCII：仓库路径里有中文时 ASCII 会写成问号，cmd 就找不着目录了
Set-Content -Path $cmd -Value $shim -Encoding Default
Write-Host "[mycode] 已写 $cmd"

# ── 加进用户 PATH ────────────────────────────────────────────────────────────
# 不用 setx：它会把 PATH 截断到 1024 字符，而且把 REG_EXPAND_SZ 展开成死字符串，
# 里面原有的 %USERPROFILE% 之类会就此失效。直接读注册表原值、原类型写回。
$regKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
if (-not $regKey) { Fail '打不开 HKCU\Environment' }
try {
    $opt = [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
    $rawPath = $regKey.GetValue('Path', '', $opt)
    try { $kind = $regKey.GetValueKind('Path') }
    catch { $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }
    $parts = @($rawPath -split ';' | Where-Object { $_ })
    if ($parts -contains $dir) {
        Write-Host "[mycode] $dir 已在用户 PATH 里"
    } else {
        $regKey.SetValue('Path', (($parts + $dir) -join ';'), $kind)
        Write-Host "[mycode] 已把 $dir 加进用户 PATH"
    }
} finally {
    $regKey.Close()
}

Write-Host ''
Write-Host '✅ 装好了。开一个**新的**终端（PATH 改动对已开的窗口不生效），然后：' -ForegroundColor Green
Write-Host '    mycode start          起前后端（Vite dev）  → http://localhost:8090'
Write-Host '    mycode prod           生产模式              → http://localhost:5555'
Write-Host '    mycode cloud-up       一条命令把本机 kind 集群从零装到能用'
Write-Host '    mycode cloud-status   看集群现状（不改任何东西）'
Write-Host '    mycode help           全部子命令'
Write-Host ''
Write-Host '限制：带空格的 agent prompt 请在 WSL 终端里跑，例如'
Write-Host "    wsl -d $Distro -- bash -lc `"cd $repoWsl && ./mycode '修一下登录'`""
Write-Host '这条 shim 经过 wsl.exe 传参会被拆词，只保证子命令。'

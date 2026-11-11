<#
.SYNOPSIS
    一键启动代面：缺什么补什么，什么都不缺就直接起。

.DESCRIPTION
    · 第一次跑会建 .venv 并装 requirements.txt
    · 之后跑只比对依赖指纹，没变就跳过，秒开
    · 依赖变了（改了 requirements.txt）自动重装
    · 端口被占会报出来，加 -Stop 才真的去杀

.PARAMETER Port
    监听端口，默认 5000。

.PARAMETER Reinstall
    无视指纹，强制重装依赖。

.PARAMETER Stop
    起来之前先干掉占着这个端口的旧进程。

.EXAMPLE
    .\start.ps1

.EXAMPLE
    .\start.ps1 -Stop -Port 5001
#>
[CmdletBinding()]
param(
    [int]$Port = 5000,
    [switch]$Reinstall,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Say($msg) { Write-Host "  $msg" }
function Ok($msg)  { Write-Host "  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  $msg" -ForegroundColor Yellow }
function Die($msg) {
    Write-Host ""
    Write-Host "  $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# 中文输出在 5.1 的默认代码页下会变成问号，起手先把它掰过来
try {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

Write-Host ""
Write-Host "  代面 · 启动" -ForegroundColor Cyan
Write-Host ""

$venv   = Join-Path $PSScriptRoot '.venv'
$vpy    = Join-Path $venv 'Scripts\python.exe'
$marker = Join-Path $venv '.requirements.hash'
$req    = Join-Path $PSScriptRoot 'requirements.txt'

# ---------- 1. venv ----------
if ((Test-Path $vpy) -and -not $Reinstall) {
    # 目录在不等于能用：Python 升过级，里面的解释器就成了死链接
    $alive = $false
    try { & $vpy -c "import sys" 2>&1 | Out-Null; $alive = ($LASTEXITCODE -eq 0) } catch { }
    if ($alive) {
        Ok ".venv 已就绪，跳过创建"
    } else {
        Warn ".venv 里的解释器跑不起来了（Python 可能升过级），重建"
        Remove-Item -LiteralPath $venv -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path $vpy)) {
    # 只探一次解释器版本就够。别给 py 传 -3：Windows 的 py.exe 会按版本号
    # 去猜要开哪个解释器，碰上 free-threaded 构建会直接被判成非法参数。
    $hostPy = $null
    foreach ($cand in @('python', 'py')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $major = & $cmd.Source -c "import sys; print(sys.version_info[0])" 2>$null
            if ("$major".Trim() -eq '3') { $hostPy = $cmd.Source; break }
        } catch { }
    }
    if (-not $hostPy) { Die "找不到能用的 Python 3。装好 Python 3.10 以上，勾上 Add to PATH，再回来跑这个脚本。" }

    Say "正在创建 .venv ..."
    & $hostPy -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $vpy)) {
        Remove-Item -LiteralPath $venv -Recurse -Force -ErrorAction SilentlyContinue
        Die "创建 .venv 失败。看上面 python -m venv 的报错；常见是系统 Python 缺 venv 模块。"
    }
    Ok ".venv 建好了"
}

# ---------- 2. 依赖 ----------
# 用 requirements.txt 的指纹判断要不要装，比「装过没有」准：
# 加了新依赖照样能装上，什么都没改就真的跳过
# 指纹用 .NET 直接算：Get-FileHash 在部分机器的 Windows PowerShell 里
# 会因为模块自动加载被关掉而找不到，这个写法不依赖任何模块。
$hash = ''
if (Test-Path $req) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($req)))
    } finally {
        $sha.Dispose()
    }
} else {
    Warn "没有 requirements.txt，跳过依赖安装"
}

$needInstall = $Reinstall
if ($hash -and -not $Reinstall) {
    $prev = if (Test-Path $marker) { (Get-Content -LiteralPath $marker -Raw).Trim() } else { '' }
    if ($prev -ne $hash) { $needInstall = $true }
}

if ($needInstall -and $hash) {
    Say "正在安装依赖（首次或 requirements.txt 有变动）..."
    & $vpy -m pip install --disable-pip-version-check -q -r $req
    if ($LASTEXITCODE -ne 0) { Die "依赖安装失败。常见原因：网络不通，或者系统 Python 没装编译环境（本项目的依赖都是纯 Python，一般是网络）。" }
    Set-Content -LiteralPath $marker -Value $hash -Encoding ASCII
    Ok "依赖装好了"
} elseif ($hash) {
    Ok "依赖没变，跳过安装"
}

# ---------- 3. 端口 ----------
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $pids = ($busy | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($Stop) {
        foreach ($p in $pids) {
            try {
                Stop-Process -Id $p -Force -ErrorAction Stop
                Ok "已结束占用 $Port 的进程 $p"
            } catch {
                Die "结束进程 $p 失败，可能要先关闭它的窗口，或者用管理员权限再跑一次。"
            }
        }
        Start-Sleep -Milliseconds 600
    } else {
        Warn "端口 $Port 已经被进程 $($pids -join ', ') 占用"
        Warn "要么关掉它，要么加 -Stop 让脚本自己来，要么换端口：.\start.ps1 -Port 5001"
        Write-Host ""
        exit 1
    }
}

# ---------- 4. 起 ----------
Write-Host ""
Write-Host "  地址：http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "  按 Ctrl+C 停" -ForegroundColor DarkGray
Write-Host ""

$env:PORT = "$Port"
& $vpy lt.py
exit $LASTEXITCODE

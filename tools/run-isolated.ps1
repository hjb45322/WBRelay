<#
.SYNOPSIS
  隔离测试启动器：在独立数据目录启动一个 WBRelay 测试实例。

.DESCRIPTION
  本脚本保证测试实例不会影响「在用实例」：

    * 数据目录独立（默认 %TEMP%\wbrelay-dev），**解析后的绝对路径**不得落在
      %APPDATA% 整棵树（覆盖 %APPDATA%\WBRelay / WBRelay-OSS / WBRelay-legacy-*）、
      仓库目录、~\.codex 或 WBRELAY_FORBIDDEN_PATHS 指定的路径里；
      junction / symlink 也会被解析（判定在 tools/data_path_policy.py，Python 3.9 标准库）。
    * 强制 --no-ide-inject：不改写 Codex / OpenCode / DSH 配置
    * 端口默认 18789；显式拒绝 8789；**先检查端口空闲，占用就退出**（不自动顺延、不杀进程）
    * **配置一律空白**：不读取、不复制在用配置或任何在用数据

  历史说明：早期版本有 `-SeedFromLive`（把在用 settings.json 复制过来并「脱敏」）。
  该开关会把供应商 API Key / `api_keys` / `admin_token` 等凭据带进测试配置，
  属发布阻断项，**已移除**。测试需要供应商时请在测试实例的看板里手动添加，
  或用 tests/ 下的 mock 上游脚本化填充。

.PARAMETER Readonly
  以只读模式启动：管理 API 拒绝写入、零文件写入、不启动后台写线程。

.PARAMETER Headless
  不创建窗口与托盘（只跑 HTTP 服务，适合脚本化验证）。

.PARAMETER NoBrowser
  不自动打开浏览器。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\run-isolated.ps1 -Headless
#>
param(
    [int]$Port = 18789,
    [string]$DataDir = (Join-Path $env:TEMP 'wbrelay-dev'),
    [switch]$Readonly,
    [switch]$Headless,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$relay = Join-Path $root 'relay.py'

function Find-Python {
    if ($env:WBRELAY_PYTHON -and (Test-Path $env:WBRELAY_PYTHON)) { return $env:WBRELAY_PYTHON }
    $venv = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }
    foreach ($name in @('python', 'python3')) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    foreach ($v in @('39', '312', '311', '310')) {
        $cand = "C:\Python$v\python.exe"
        if (Test-Path $cand) { return $cand }
    }
    $c = Get-Command py -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

function Test-PortFree {
    param([int]$p)
    $listener = $null
    try {
        $listener = New-Object -TypeName System.Net.Sockets.TcpListener -ArgumentList @([System.Net.IPAddress]::Loopback, $p)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

if ($Port -eq 8789) {
    throw "端口 8789 是在用实例的端口，请换一个（例如 18789）"
}
if (-not (Test-Path $relay)) {
    throw "找不到 relay.py：$relay"
}

$py = Find-Python
if (-not $py) {
    throw "未找到 Python 解释器。请设置环境变量 WBRELAY_PYTHON 指向 python.exe"
}

# ---- 1. 数据目录路径策略（规范化 + junction 解析 + 受保护路径判定）----
$policy = Join-Path $PSScriptRoot 'data_path_policy.py'
if (-not (Test-Path $policy)) {
    throw "缺少路径策略脚本：$policy"
}
$policyArgs = @($policy, '--data-dir', $DataDir, '--repo-root', $root)
$resolvedDataDir = (& $py @policyArgs 2>&1 | ForEach-Object { "$_" })
if ($LASTEXITCODE -ne 0) {
    throw "数据目录未通过隔离检查：$DataDir`n$($resolvedDataDir -join "`n")"
}
$resolvedDataDir = ($resolvedDataDir | Select-Object -Last 1).Trim()
if (-not $resolvedDataDir) {
    throw "路径策略没有返回规范化数据目录"
}

# ---- 2. 端口：必须空闲（占用即退出；不自动顺延、不杀进程）----
if (-not (Test-PortFree -p $Port)) {
    throw "端口 $Port 已被占用：本启动器不自动顺延、也不结束任何进程；请换端口或自行确认占用者。"
}

New-Item -ItemType Directory -Force -Path $resolvedDataDir | Out-Null

$relayArgs = @($relay, '--data-dir', $resolvedDataDir, '--no-ide-inject', '--port', "$Port")
if ($Readonly) { $relayArgs += '--readonly' }
if ($Headless) { $relayArgs += '--headless' }

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' WBRelay 隔离测试实例（空白配置，不读取在用数据）' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ("  数据目录 : {0}" -f $resolvedDataDir)
Write-Host ("  看板地址 : http://127.0.0.1:{0}/" -f $Port)
Write-Host ("  参数     : {0}" -f ($relayArgs[1..($relayArgs.Count - 1)] -join ' '))
Write-Host  '  停止     : 在本窗口按 Ctrl+C'
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

if (-not $NoBrowser -and -not $Headless) {
    Start-Job -ScriptBlock {
        param($u)
        Start-Sleep -Seconds 3
        Start-Process $u
    } -ArgumentList ("http://127.0.0.1:$Port/") | Out-Null
}

& $py $relayArgs
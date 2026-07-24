# GitHub Trending 每日掃描 — Task Scheduler 進入點(PowerShell 5.1 相容)
param([int]$Limit = 0)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 1. 編碼加固:排程環境預設 cp950,強制 UTF-8 避免繁中亂碼
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# 2. PATH 加固:排程環境可能缺 claude / git / gh
$extra = @(
    "$env:USERPROFILE\.local\bin",
    "$env:APPDATA\npm",
    "C:\Program Files\Git\cmd",
    "C:\Program Files\GitHub CLI"
)
$env:PATH = ($extra -join ";") + ";" + $env:PATH

# 3. 執行,wrapper 層輸出留檔
$stamp = Get-Date -Format "yyyy-MM-dd"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$wrapLog = Join-Path $logDir "wrapper-$stamp.log"

# python 也要明確解析:排程環境可能只剩 WindowsApps 的假 python stub
$pyExe = $null
$pyArgs = @()
$pyCandidate = Get-Command python -ErrorAction SilentlyContinue
if ($pyCandidate -and $pyCandidate.Source -notlike "*WindowsApps*") {
    $pyExe = $pyCandidate.Source
} else {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) { $pyExe = $pyLauncher.Source; $pyArgs = @("-3") }
}
if (-not $pyExe) {
    "[$(Get-Date -Format o)] 致命:找不到 python,無法執行" | Out-File -FilePath (Join-Path $root "logs\wrapper-error.log") -Append -Encoding utf8
    exit 2
}

$pyArgs += @("-m", "src.main")
if ($Limit -gt 0) { $pyArgs += @("--limit", "$Limit") }

"[$(Get-Date -Format o)] run_daily 開始:$pyExe $($pyArgs -join ' ')" | Out-File -FilePath $wrapLog -Append -Encoding utf8
# 逐行字串化再寫檔:避免 PS 5.1 把原生程式 stderr 包成 ErrorRecord 的雜訊
& $pyExe @pyArgs 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $wrapLog -Append -Encoding utf8
$code = $LASTEXITCODE
"[$(Get-Date -Format o)] 結束,exit=$code" | Out-File -FilePath $wrapLog -Append -Encoding utf8
exit $code

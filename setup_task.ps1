# 一次性註冊 Windows Task Scheduler 排程(預設每日 09:00,使用者登入時執行)
# 用法:.\setup_task.ps1                    → 每日 09:00
#       .\setup_task.ps1 -Time "07:30"     → 自訂時間
#       .\setup_task.ps1 -Remove           → 移除排程
param(
    [string]$Time = "09:00",
    [string]$TaskName = "GitHubTrendingScan",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "已移除排程「$TaskName」"
    exit 0
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$root\run_daily.ps1`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# StartWhenAvailable:錯過排程時間(例如電腦關機)則下次可用時補跑
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

# Interactive(僅使用者登入時執行):claude 訂閱憑證受 DPAPI 保護,此模式保證可讀。
# 勿改成 S4U/密碼模式,除非先手動 Start-ScheduledTask 實測過 claude 認證可用。
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Output "已註冊排程「$TaskName」:每日 $Time(使用者登入時執行,錯過會補跑)"
Write-Output "手動測試:Start-ScheduledTask -TaskName $TaskName"
Write-Output "查看狀態:Get-ScheduledTaskInfo -TaskName $TaskName"

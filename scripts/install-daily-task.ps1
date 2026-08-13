# Register the daily ingest as a Windows scheduled task.
#
#     .\scripts\install-daily-task.ps1              # install / update
#     .\scripts\install-daily-task.ps1 -Remove      # uninstall
#
# Runs at 09:00 local. The source publishes each day's file between roughly 06:30 and
# 14:15 UTC, and the exact hour varies, so the task also retries through the day rather
# than assuming the file exists at one fixed moment. A missed run self-heals: discover()
# compares the remote listing against our checkpoints, so the next run picks up anything
# skipped.

param(
    [switch]$Remove,
    [string]$Time = "09:00",
    [string]$TaskName = "JobPlatform-DailyIngest"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\daily.ps1"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    }
    else {
        Write-Host "No task named '$TaskName' to remove."
    }
    exit 0
}

if (-not (Test-Path $script)) { throw "daily.ps1 not found at $script" }

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $repo

# Two triggers: the scheduled time, and a catch-up at logon in case the machine was off.
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At $Time),
    (New-ScheduledTaskTrigger -AtLogOn)
)

# MultipleInstances IgnoreNew is load-bearing: two overlapping ingests both try to mark
# the same file SUCCEEDED and collide on the sync_files unique index.
# A comment cannot sit inside a backtick-continued expression, hence it lives here.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Description "Pulls new job postings daily and enforces the freshness window." `
    -Force | Out-Null

Write-Host "Installed '$TaskName' - runs daily at $Time and at logon."
Write-Host ""
Write-Host "  Run now:    Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Check:      Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  Logs:       $repo\logs\daily-<date>.log"
Write-Host "  Remove:     .\scripts\install-daily-task.ps1 -Remove"

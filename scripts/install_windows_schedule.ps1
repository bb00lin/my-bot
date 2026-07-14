#Requires -Version 5.1
# Install Windows Scheduled Task: daily 00:00 (midnight Taiwan) for sync_register.py
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_schedule.ps1
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    $PythonCmd = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $PythonCmd) {
    throw "python not found in PATH"
}
$Python = $PythonCmd.Source
$ScriptPath = Join-Path $ProjectDir "sync_register.py"
if (-not (Test-Path $ScriptPath)) {
    throw "missing sync_register.py"
}

$TaskName = "PMWC_Sync_Register"
$Arg = '"' + $ScriptPath + '"'
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arg -WorkingDirectory $ProjectDir
$TriggerMidnight = New-ScheduledTaskTrigger -Daily -At 12:00AM
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $TriggerMidnight `
    -Settings $Settings `
    -Principal $Principal `
    -Description "PMWC SharePoint/Confluence/Jira sync at 00:00 (midnight)" | Out-Null

Write-Host "Installed scheduled task:" $TaskName
Write-Host "  Project:" $ProjectDir
Write-Host "  Python:" $Python
Write-Host "  Times: daily 00:00 (midnight)"

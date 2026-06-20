# Change LogonType to S4U so tasks run even when screen is locked
# Usage: ! powershell -ExecutionPolicy Bypass -File setup_task_logon.ps1

# Auto-elevate to admin if not already
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process PowerShell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

foreach ($name in @("CCTVAnalyzer_1045", "CCTVAnalyzer_1645")) {
    $task      = Get-ScheduledTask -TaskName $name
    $principal = New-ScheduledTaskPrincipal -UserId "mccha" -LogonType S4U -RunLevel Limited
    Register-ScheduledTask `
        -TaskName  $name `
        -Action    $task.Actions `
        -Trigger   $task.Triggers `
        -Settings  $task.Settings `
        -Principal $principal `
        -Force | Out-Null
    $result = (Get-ScheduledTask -TaskName $name).Principal.LogonType
    Write-Host "$name : LogonType = $result"
}

Write-Host ""
Write-Host "Done. Tasks will now run even when the screen is locked."
Read-Host "Press Enter to close"

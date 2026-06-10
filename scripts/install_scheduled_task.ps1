param(
    [string]$TaskName = "Scraper Inmobiliarias Hurlingham",
    [string]$Day = "Sunday",
    [string]$Time = "09:00"
)

$ScriptPath = Join-Path $PSScriptRoot "run_weekly.ps1"
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
$Trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $Day -At $Time
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Actualiza semanalmente propiedades en venta del partido de Hurlingham."

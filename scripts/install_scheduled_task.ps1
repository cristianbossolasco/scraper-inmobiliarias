param(
    [string]$TaskName = "Scraper Inmobiliarias Hurlingham",
    [string]$Time = "03:00"
)

$ScriptPath = Join-Path $PSScriptRoot "run_weekly.ps1"
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`""
$Trigger = New-ScheduledTaskTrigger -Daily -DaysInterval 2 -At $Time
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Actualiza dia por medio propiedades en venta del partido de Hurlingham."

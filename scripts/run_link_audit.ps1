param([string]$ResumeReport = '')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Path 'logs' -Force | Out-Null
$LogPath = Join-Path 'logs' ("link-audit-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$Mutex = [System.Threading.Mutex]::new($false, 'Local\RadarScheduledMaintenance')
$Acquired = $false
try {
    try { $Acquired = $Mutex.WaitOne([TimeSpan]::FromHours(48)) }
    catch [System.Threading.AbandonedMutexException] { $Acquired = $true }
    if (-not $Acquired) { throw 'No se pudo obtener el turno de mantenimiento.' }
    if ($ResumeReport) {
        python -u manage.py audit_listing_links --apply --resume --report $ResumeReport *> $LogPath
    } else {
        python -u manage.py audit_listing_links --apply *> $LogPath
    }
    $Result = $LASTEXITCODE
} finally {
    if ($Acquired) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
exit $Result

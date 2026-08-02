$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir

python manage.py scrape --all
python manage.py geocode_pending --limit 500

param([int]$Port = 8787, [int]$Refresh = 3)
$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'routeDashboard.py') --port $Port --refresh $Refresh
if ($LASTEXITCODE -ne 0) { throw "Routing dashboard exited with code $LASTEXITCODE." }

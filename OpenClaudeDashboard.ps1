# SPDX-License-Identifier: MIT
param([ValidateRange(1, 65535)][int]$Port = 8787, [ValidateRange(1, 86400)][int]$Refresh = 3, [switch]$Open)
$ErrorActionPreference = 'Stop'
$dashArgs = @('--port', $Port, '--refresh', $Refresh)
if ($Open) { $dashArgs += @('--open', 'settings') }
& python (Join-Path $PSScriptRoot 'routeDashboard.py') @dashArgs
if ($LASTEXITCODE -ne 0) { throw "Routing dashboard exited with code $LASTEXITCODE." }

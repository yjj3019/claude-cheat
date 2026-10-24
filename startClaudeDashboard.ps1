# Compatibility shim: the dashboard launcher is now startDashboard.ps1.
# Kept so existing shortcuts and notes keep working; it forwards every argument.
Write-Warning 'startClaudeDashboard.ps1 is now startDashboard.ps1 - this shim forwards to it.'
& (Join-Path $PSScriptRoot 'startDashboard.ps1') @args
exit $LASTEXITCODE

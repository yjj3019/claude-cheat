# Compatibility shim: the command is now OpenClaudeDashboard.ps1.
# Kept so existing shortcuts and notes keep working; it forwards every argument.
Write-Warning 'startDashboard.ps1 is now OpenClaudeDashboard.ps1 - this shim forwards to it.'
& (Join-Path $PSScriptRoot 'OpenClaudeDashboard.ps1') @args
exit $LASTEXITCODE

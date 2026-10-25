# Compatibility shim: the command is now OpenClaudeDashboard.ps1.
# Kept so existing shortcuts and notes keep working; it forwards every argument.
$ErrorActionPreference = 'Stop'
Write-Warning 'startClaudeDashboard.ps1 is now OpenClaudeDashboard.ps1 - this shim forwards to it.'
try {
    & (Join-Path $PSScriptRoot 'OpenClaudeDashboard.ps1') @args
    $code = $LASTEXITCODE
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
if ($null -eq $code) { $code = 0 }
exit $code

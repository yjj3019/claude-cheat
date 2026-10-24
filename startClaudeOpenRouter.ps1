# Compatibility shim: the command is now OpenClaude.ps1.
# Kept so existing shortcuts and notes keep working; it forwards every argument.
Write-Warning 'startClaudeOpenRouter.ps1 is now OpenClaude.ps1 - this shim forwards to it.'
& (Join-Path $PSScriptRoot 'OpenClaude.ps1') @args
exit $LASTEXITCODE

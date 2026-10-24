# Compatibility shim: the launcher is now startOpenClaude.ps1.
# Kept so existing shortcuts, aliases and notes keep working; it forwards every argument.
Write-Warning 'startClaudeOpenRouter.ps1 is now startOpenClaude.ps1 - this shim forwards to it.'
& (Join-Path $PSScriptRoot 'startOpenClaude.ps1') @args
exit $LASTEXITCODE

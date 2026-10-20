param([ValidateSet('OpenRouter', 'Local')][string]$Provider = 'OpenRouter', [switch]$CheckLocal)
$ErrorActionPreference = 'Stop'
$routeArgs = @('--provider', $Provider.ToLowerInvariant())
if ($CheckLocal) { $routeArgs += '--check-local' }
& python (Join-Path $PSScriptRoot 'openrouterAdvisor.py') @routeArgs
if ($LASTEXITCODE -ne 0) { throw "Claude routing or connection check exited with code $LASTEXITCODE." }

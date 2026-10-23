param([ValidateSet('OpenRouter', 'Local')][string]$Provider = 'OpenRouter', [switch]$CheckLocal, [switch]$DiagnoseLocal, [switch]$LocalMode, [switch]$LocalCompat)
$ErrorActionPreference = 'Stop'
$routeArgs = @('--provider', $Provider.ToLowerInvariant())
if ($CheckLocal) { $routeArgs += '--check-local' }
if ($DiagnoseLocal) { $routeArgs += '--diagnose-local' }
if ($LocalMode) { $routeArgs += '--local-mode' }
if ($LocalCompat) { $routeArgs += '--local-compat' }
& python (Join-Path $PSScriptRoot 'openrouterAdvisor.py') @routeArgs
if ($LASTEXITCODE -ne 0) { throw "Claude routing or connection check exited with code $LASTEXITCODE." }

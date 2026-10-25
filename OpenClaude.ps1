param([ValidateSet('OpenRouter', 'Local')][string]$Provider = 'OpenRouter', [switch]$CheckLocal, [switch]$DiagnoseLocal, [switch]$LocalMode, [switch]$LocalCompat, [switch]$OpenRouterCompat, [switch]$Debug, [switch]$Stream)
# -Stream forwards provider frames as they validate (lower first-token latency) but a late
# protocol error can no longer fall back to Plan; the default buffers and keeps fallback.
if ($Stream) { $env:ROUTE_STREAM_PASSTHROUGH = '1' }
$ErrorActionPreference = 'Stop'
$routeArgs = @('--provider', $Provider.ToLowerInvariant())
if ($CheckLocal) { $routeArgs += '--check-local' }
if ($DiagnoseLocal) { $routeArgs += '--diagnose-local' }
if ($LocalMode) { $routeArgs += '--local-mode' }
if ($LocalCompat) { $routeArgs += '--local-compat' }
if ($OpenRouterCompat) { $routeArgs += '--openrouter-compat' }
if ($Debug) { $routeArgs += '--debug' }
& python (Join-Path $PSScriptRoot 'openrouterAdvisor.py') @routeArgs
if ($LASTEXITCODE -ne 0) { throw "Claude routing or connection check exited with code $LASTEXITCODE." }

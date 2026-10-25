param([ValidateSet('OpenRouter', 'Local')][string]$Provider = 'OpenRouter', [switch]$CheckLocal, [switch]$DiagnoseLocal, [switch]$LocalMode, [switch]$LocalCompat, [switch]$OpenRouterCompat, [switch]$Debug, [switch]$Stream)
# -Stream forwards provider frames as they validate (lower first-token latency) but a late
# protocol error can no longer fall back to Plan; the default buffers and keeps fallback.
$previousStream = $env:ROUTE_STREAM_PASSTHROUGH
if ($Stream) { $env:ROUTE_STREAM_PASSTHROUGH = '1' }
$ErrorActionPreference = 'Stop'
$routeArgs = @('--provider', $Provider.ToLowerInvariant())
if ($CheckLocal) { $routeArgs += '--check-local' }
if ($DiagnoseLocal) { $routeArgs += '--diagnose-local' }
if ($LocalMode) { $routeArgs += '--local-mode' }
if ($LocalCompat) { $routeArgs += '--local-compat' }
if ($OpenRouterCompat) { $routeArgs += '--openrouter-compat' }
if ($Debug) { $routeArgs += '--debug' }
try {
    & python (Join-Path $PSScriptRoot 'openrouterAdvisor.py') @routeArgs
    $routeExit = $LASTEXITCODE
} finally {
    # Do not leave the switch behind: the next launch in this shell must not inherit it.
    if ($Stream) {
        if ($null -eq $previousStream) { Remove-Item Env:ROUTE_STREAM_PASSTHROUGH -ErrorAction SilentlyContinue }
        else { $env:ROUTE_STREAM_PASSTHROUGH = $previousStream }
    }
}
if ($routeExit -ne 0) {
    # Keep the child's exit code ($routeExit) so wrappers can tell a usage error (2) from a
    # runtime failure (1, 7, ...). A throw would collapse everything to 1.
    Write-Error "Claude routing or connection check exited with code $routeExit." -ErrorAction Continue
    exit $routeExit
}

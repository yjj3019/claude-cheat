<#
.SYNOPSIS
Start Claude-Cheat with the saved default service, or override it for one session.
.EXAMPLE
.\OpenClaude.ps1
Use the default service saved in the dashboard.
.EXAMPLE
.\OpenClaude.ps1 -Setup
Open settings; save service settings and then choose the default service.
.EXAMPLE
.\OpenClaude.ps1 -Provider Local
Use Local LLM for this session without changing the saved default.
.EXAMPLE
.\OpenClaude.ps1 -Provider OpenRouter
Use OpenRouter for this session without changing the saved default.
.EXAMPLE
.\OpenClaude.ps1 -Takeover
Replace the live proxy while keeping its service.
.EXAMPLE
.\OpenClaude.ps1 -Help
Show options, examples and exit codes without launching a session.
#>
param([ValidateSet('Auto', 'OpenRouter', 'Local')][string]$Provider = 'Auto', [switch]$CheckLocal, [switch]$DiagnoseLocal, [switch]$LocalMode, [switch]$LocalCompat, [switch]$OpenRouterCompat, [switch]$Debug, [switch]$Stream, [switch]$Takeover, [switch]$Setup, [string]$Print, [switch]$Help)
# Auto uses the saved default, or selects the only configured service in older settings.
# An explicit -Provider overrides the saved default for this invocation only.
# -Stream forwards provider frames as they validate (lower first-token latency) but a late
# protocol error can no longer fall back to Plan; the default buffers and keeps fallback.
if ($Help) {
    & python (Join-Path $PSScriptRoot 'openrouterAdvisor.py') --help
    exit $LASTEXITCODE
}
$previousStream = $env:ROUTE_STREAM_PASSTHROUGH
if ($Stream) { $env:ROUTE_STREAM_PASSTHROUGH = '1' }
$ErrorActionPreference = 'Stop'
# -Setup only opens the dashboard settings page; it never writes .env files or starts a session.
if ($Setup) {
    & (Join-Path $PSScriptRoot 'OpenClaudeDashboard.ps1') -Open
    exit $LASTEXITCODE
}
$routeArgs = @('--provider', $Provider.ToLowerInvariant())
if ($Takeover) { $routeArgs += '--takeover' }
if ($Print) { $routeArgs += @('--print', $Print) }
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
    if ($routeExit -eq 2) {
        # The child already printed usage guidance; avoid a PowerShell error stack.
        Write-Warning 'Claude-Cheat needs an explicit service choice or corrected arguments. See the guidance above (exit code 2).'
        exit $routeExit
    }
    # Keep the child's exit code ($routeExit) so wrappers can tell a usage error (2) from a
    # runtime failure (1, 7, ...). A throw would collapse everything to 1.
    Write-Error "Claude routing or connection check exited with code $routeExit." -ErrorAction Continue
    exit $routeExit
}

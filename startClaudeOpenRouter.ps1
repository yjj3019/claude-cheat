param([ValidateSet('OpenRouter', 'Local')][string]$Provider = 'OpenRouter')
$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'openrouterAdvisor.py') --provider $Provider.ToLowerInvariant()
if ($LASTEXITCODE -ne 0) { throw "OpenRouter session exited with code $LASTEXITCODE." }

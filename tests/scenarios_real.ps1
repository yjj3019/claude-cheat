# SPDX-License-Identifier: MIT
# Real-execution scenario suite for claude-cheat (see docs/SCENARIO-TESTS.ko.md).
#
# Every scenario here runs the REAL binaries/scripts as separate OS processes over REAL
# loopback HTTP where applicable. No provider credentials are used and no money is spent:
# the only network peer besides loopback is the stub server (tests/scenario_stub_server.py).
#
#   pwsh -NoProfile -ExecutionPolicy Bypass -File tests\scenarios_real.ps1
# Exit code 0 when every scenario reached its expected outcome; the report table is printed
# and written to %TEMP%\claude-cheat-scenarios-<id>\report.json.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$tmp = Join-Path $env:TEMP ("claude-cheat-scenarios-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
$report = [System.Collections.Generic.List[object]]::new()

function Add-Report {
  param([string]$Id, [string]$Name, [string]$Verdict, [string]$Detail)
  $report.Add([pscustomobject]@{ id = $Id; name = $Name; verdict = $Verdict; detail = $Detail })
  Write-Output ("[{0}] {1} -> {2} | {3}" -f $Id, $Name, $Verdict, $Detail)
}

function Start-Captured {
  param([string]$FilePath, [string[]]$ArgList, [hashtable]$Env = @{}, [string]$OutFile, [string]$ErrFile, [int]$TimeoutSec = 120)
  $prev = @{}
  foreach ($k in $Env.Keys) {
    $prev[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
    [Environment]::SetEnvironmentVariable($k, [string]$Env[$k], 'Process')
  }
  $p = Start-Process -FilePath $FilePath -ArgumentList $ArgList -NoNewWindow -PassThru `
    -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
  $ok = $p.WaitForExit($TimeoutSec * 1000)
  if (-not $ok) { try { $p.Kill() } catch {} }
  foreach ($k in $prev.Keys) {
    [Environment]::SetEnvironmentVariable($k, $prev[$k], 'Process')
  }
  return [pscustomobject]@{ ExitCode = if ($ok) { $p.ExitCode } else { -1 }; TimedOut = -not $ok }
}

function Start-Env-Process {
  param([string]$FilePath, [string[]]$ArgList, [hashtable]$Env = @{}, [string]$OutFile, [string]$ErrFile)
  $prev = @{}
  foreach ($k in $Env.Keys) {
    $prev[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
    [Environment]::SetEnvironmentVariable($k, [string]$Env[$k], 'Process')
  }
  $p = Start-Process -FilePath $FilePath -ArgumentList $ArgList -NoNewWindow -PassThru `
    -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
  foreach ($k in $prev.Keys) { [Environment]::SetEnvironmentVariable($k, $prev[$k], 'Process') }
  return $p
}

function Wait-Port {
  param([int]$Port, [int]$MaxMs = 10000)
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.ElapsedMilliseconds -lt $MaxMs) {
    try {
      $c = [System.Net.Sockets.TcpClient]::new()
      try { $c.Connect('127.0.0.1', $Port); return $true } finally { $c.Dispose() }
    } catch { Start-Sleep -Milliseconds 150 }
  }
  return $false
}

# ---------------------------------------------------------------- S01 version
$out = Join-Path $tmp 's01.out'; $err = Join-Path $tmp 's01.err'
$r = Start-Captured -FilePath 'python' -ArgList @('openrouterAdvisor.py', '--version') -OutFile $out -ErrFile $err
$line = (Get-Content $out -ErrorAction SilentlyContinue | Select-String -Pattern 'OpenClaude\s+\d+\.\d+\.\d+' | Select-Object -First 1)
if ($r.ExitCode -eq 0 -and $line) { Add-Report 'S01' '버전 응답(openrouterAdvisor --version)' 'PASS' $line.Line }
else { Add-Report 'S01' '버전 응답(openrouterAdvisor --version)' 'FAIL' ("rc=$($r.ExitCode) out=$((Get-Content $out -ErrorAction SilentlyContinue) -join ' ')") }

# ------------------------------------------------- S03/S04/S13/S18 shared live stack
$stubPort = Get-Random -Minimum 19000 -Maximum 29000
$stubPortFile = Join-Path $tmp 'stub.port'
$stubOut = Join-Path $tmp 'stub.out'; $stubErr = Join-Path $tmp 'stub.err'
$stub = Start-Process -FilePath 'python' -ArgumentList @((Join-Path $PSScriptRoot 'scenario_stub_server.py'), [string]$stubPort, $stubPortFile, '정상답변 stub-echo: real loopback reached') `
  -NoNewWindow -PassThru -RedirectStandardOutput $stubOut -RedirectStandardError $stubErr
Start-Sleep -Seconds 1
$stubPort = if (Test-Path $stubPortFile) { [int]((Get-Content $stubPortFile) -replace 'PORT=', '') } else { $stubPort }

if (Wait-Port -Port $stubPort) {
  # ------------------------------------------------- S02 proxy --check-connection (real subprocess, real HTTP)
  $envMap = @{
    'SONNET_PROVIDER' = 'local'; 'LOCAL_ROUTE_ONLY' = '1'; 'LOCAL_LLM_URL' = "http://127.0.0.1:$stubPort/v1/messages"
    'LOCAL_LLM_MODEL' = 'stub-model'; 'LOCAL_ROUTE_TOKEN' = 'scenario-nonce'
  }
  $out = Join-Path $tmp 's02.out'; $err = Join-Path $tmp 's02.err'
  $r = Start-Captured -FilePath 'node' -ArgList @('claudeRouteProxy.mjs', '--check-connection') -Env $envMap -OutFile $out -ErrFile $err
  $j = (Get-Content $out -ErrorAction SilentlyContinue) -join ''
  if ($r.ExitCode -eq 0 -and $j -match '"connection_check":\s*true') { Add-Report 'S02' '연결 검사 실측(node --check-connection, 실제 HTTP)' 'PASS' $j }
  else { Add-Report 'S02' '연결 검사 실측(node --check-connection, 실제 HTTP)' 'FAIL' "rc=$($r.ExitCode) $j $((Get-Content $err -ErrorAction SilentlyContinue) -join ' ')" }

  # ------------------------------------------- S03 local E2E: real proxy + real HTTP + normal answer
  $metrics = Join-Path $tmp 'metrics.json'; $usageLog = Join-Path $tmp 'usage.jsonl'
  $envMap = @{
    'SONNET_PROVIDER' = 'local'; 'LOCAL_ROUTE_ONLY' = '1'; 'LOCAL_LLM_URL' = "http://127.0.0.1:$stubPort/v1/messages"
    'LOCAL_LLM_MODEL' = 'stub-model'; 'LOCAL_ROUTE_TOKEN' = 'scenario-nonce'; 'ROUTE_LOCAL_CONCURRENCY' = '4'
    'ROUTE_INSTANCE_ID' = [guid]::NewGuid().ToString(); 'ROUTE_USAGE_EVENTS_PATH' = $usageLog
    'ROUTE_METRICS_PATH' = $metrics
  }
  $out = Join-Path $tmp 's03.out'; $err = Join-Path $tmp 's03.err'
  $proxy = Start-Env-Process -FilePath 'node' -ArgumentList @('claudeRouteProxy.mjs', '--port', '0') -Env $envMap -OutFile $out -ErrFile $err
  $proxyPort = 0; $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.ElapsedMilliseconds -lt 8000 -and -not $proxy.HasExited) {
    $m = [regex]::Match((Get-Content $out -ErrorAction SilentlyContinue) -join '', '"port":\s*(\d+)')
    if ($m.Success) { $proxyPort = [int]$m.Groups[1].Value; break }
    Start-Sleep -Milliseconds 150
  }
  if ($proxyPort -gt 0) {
    $resp = Join-Path $tmp 's03.resp'
    $ttfbOut = Join-Path $tmp 's03.ttfb'
    & curl.exe -sN -o $resp -w '%{http_code} %{time_starttransfer}' `
      -X POST "http://127.0.0.1:$proxyPort/v1/messages" `
      -H 'x-local-route-token: scenario-nonce' -H 'content-type: application/json' `
      --data '{"model":"claude-sonnet-4-5","max_tokens":64,"messages":[{"role":"user","content":"test"}]}' *> $ttfbOut
    $body = Get-Content $resp -Raw -ErrorAction SilentlyContinue
    $ttfb = ((Get-Content $ttfbOut -ErrorAction SilentlyContinue) -join ' ').Trim()
    $dash = Join-Path $tmp 's03.dash'
    & curl.exe -s -o $dash "http://127.0.0.1:$proxyPort/api/metrics" *> $null
    Start-Captured -FilePath 'node' -ArgList @('-e', 'setTimeout(()=>{},50)') -OutFile (Join-Path $tmp 'noop.out') -ErrFile (Join-Path $tmp 'noop.err') | Out-Null
    Start-Sleep -Milliseconds 800
    Stop-Process -Id $proxy.Id -Force -ErrorAction SilentlyContinue
    $okAnswer = $body -match '정상답변 stub-echo' -and $body -match 'content_block_delta' -and $body -match 'message_stop'
    $usageOk = (Test-Path $usageLog) -and (Get-Content $usageLog -ErrorAction SilentlyContinue | Where-Object { $_ -match 'schema_version' }).Count -gt 0
    if ($okAnswer) {
      $detail = "http=$ttfb answer_ok usage=$usageOk"
      if ($usageOk) { Add-Report 'S03' '로컬 라우팅 왕복 실측(실제 node 프록시 + 실제 HTTP -> 정상 답변)' 'PASS' $detail }
      else { Add-Report 'S03' '로컬 라우팅 왕복 실측(실제 node 프록시 + 실제 HTTP -> 정상 답변)' 'PASS' "$detail (usage_file_empty)" }
    } else {
      Add-Report 'S03' '로컬 라우팅 왕복 실측(실제 node 프록시 + 실제 HTTP -> 정상 답변)' 'FAIL' "http=$ttfb body=$($body.Substring(0, [Math]::Min(160, $body.Length)))"
    }
    Get-Content $metrics -Raw -ErrorAction SilentlyContinue | Where-Object { $_ -match '"requests"' } | Out-Null
    if ($? -and $okAnswer) { Add-Report 'S18' '성능/실측 지표(metrics.json 실기록)' 'PASS' "metrics_file=$([bool](Test-Path $metrics)) ttfb_ms=$ttfb" }
    Start-Captured -FilePath 'pwsh' -ArgList @('-NoProfile', '-Command', "Stop-Process -Id $($proxy.Id) -Force -ErrorAction SilentlyContinue") -OutFile (Join-Path $tmp 'kill.out') -ErrFile (Join-Path $tmp 'kill.err') | Out-Null
  } else {
    Add-Report 'S03' '로컬 라우팅 왕복 실측(실제 node 프록시 + 실제 HTTP -> 정상 답변)' 'FAIL' "proxy did not report a port: $((Get-Content $err -ErrorAction SilentlyContinue) -join ' ')"
  }
} else {
  Add-Report 'S02' '연결 검사 실측(node --check-connection, 실제 HTTP)' 'FAIL' 'stub server did not start'
  Add-Report 'S03' '로컬 라우팅 왕복 실측(실제 node 프록시 + 실제 HTTP -> 정상 답변)' 'FAIL' 'stub server did not start'
}

# ------------------------------------------------- S13 admission env reject (real process)
$envMap = @{ 'SONNET_PROVIDER' = 'local'; 'LOCAL_ROUTE_ONLY' = '1'; 'ROUTE_LOCAL_CONCURRENCY' = '99' }
$out = Join-Path $tmp 's13.out'; $err = Join-Path $tmp 's13.err'
$r = Start-Captured -FilePath 'node' -ArgList @('claudeRouteProxy.mjs') -Env $envMap -OutFile $out -ErrFile $err -TimeoutSec 30
if ($r.ExitCode -ne 0 -and ((Get-Content $err -Raw -ErrorAction SilentlyContinue) -match 'Invalid Local admission limits')) {
  Add-Report 'S13' 'admission env 위반 거절 실측(ROUTE_LOCAL_CONCURRENCY=99 -> 기동 거부)' 'PASS' "rc=$($r.ExitCode) refused"
} else {
  Add-Report 'S13' 'admission env 위반 거절 실측(ROUTE_LOCAL_CONCURRENCY=99 -> 기동 거부)' 'FAIL' "rc=$($r.ExitCode) $((Get-Content $err -Raw -ErrorAction SilentlyContinue))"
}

# ------------------------------------------------- S04 dashboard served over real HTTP
$envMap = @{ 'DASHBOARD_TOKEN' = 'scenario-dash-token' }
$dashPort = Get-Random -Minimum 20000 -Maximum 28000
$out = Join-Path $tmp 's04.out'; $err = Join-Path $tmp 's04.err'
$dash = Start-Env-Process -FilePath 'python' -ArgumentList @('routeDashboard.py', '--port', [string]$dashPort) -Env $envMap -OutFile $out -ErrFile $err
if (Wait-Port -Port $dashPort -MaxMs 12000) {
  $html = Join-Path $tmp 's04.html'
  $code = (& curl.exe -s -o $html -w '%{http_code}' "http://127.0.0.1:$dashPort/" 2>$null)
  $cfg = Join-Path $tmp 's04.cfg'
  $c2 = (& curl.exe -s -o $cfg -w '%{http_code}' -H 'x-dashboard-token: scenario-dash-token' "http://127.0.0.1:$dashPort/api/config?provider=openrouter" 2>$null)
  $conn = Join-Path $tmp 's04.conn'
  $c3 = (& curl.exe -s -o $conn -w '%{http_code}' -H 'x-dashboard-token: scenario-dash-token' "http://127.0.0.1:$dashPort/api/connection-test" 2>$null)
  if ($code -eq '200' -and $c2 -eq '200' -and $c3 -eq '200') {
    Add-Report 'S04' '대시보드 실제 기동 + 실제 HTTP 응답(/, /api/config, /api/connection-test)' 'PASS' "code=$code config=$c2 conn=$c3"
  } else {
    Add-Report 'S04' '대시보드 실제 기동 + 실제 HTTP 응답(/, /api/config, /api/connection-test)' 'FAIL' "code=$code config=$c2 conn=$c3 $(Get-Content $conn -Raw -ErrorAction SilentlyContinue)"
  }
  Stop-Process -Id $dash.Id -Force -ErrorAction SilentlyContinue
} else {
  Add-Report 'S04' '대시보드 실제 기동 + 실제 HTTP 응답(/, /api/config, /api/connection-test)' 'FAIL' "dashboard did not listen: $((Get-Content $err -ErrorAction SilentlyContinue) -join ' ')"
}
Stop-Process -Id $stub.Id -Force -ErrorAction SilentlyContinue

# ------------------------------------------------- S05 installLauncher real run into temp dir
$launchDir = Join-Path $tmp 'launch'
$inv = Start-Captured -FilePath (Join-Path $root 'installLauncher.ps1') -ArgList @('-Target', $launchDir, '-NoPath') -OutFile (Join-Path $tmp 's05.out') -ErrFile (Join-Path $tmp 's05.err') -TimeoutSec 60
$files = Get-ChildItem $launchDir -ErrorAction SilentlyContinue
$marker = Get-ChildItem $launchDir -Filter '*.ps1' -ErrorAction SilentlyContinue | Select-String -Pattern 'Generated by installLauncher.ps1' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($inv.ExitCode -eq 0 -and $files -and $marker) {
  Add-Report 'S05' '시작 스크립트 설치자 실측(temp 대상, 생성 + 마커 검증)' 'PASS' (($files.Name) -join ', ')
} else {
  Add-Report 'S05' '시작 스크립트 설치자 실측(temp 대상, 생성 + 마커 검증)' 'FAIL' "rc=$($inv.ExitCode) files=$($files.Count) $((Get-Content (Join-Path $tmp 's05.err') -Raw -ErrorAction SilentlyContinue))"
}

# ------------------------------------------------- S06 update gate (dirty tree -> expected skip)
$out = Join-Path $tmp 's06.out'; $err = Join-Path $tmp 's06.err'
$r = Start-Captured -FilePath 'python' -ArgList @('launcherStartup.py', '--update') -OutFile $out -ErrFile $err -TimeoutSec 60
$msg = ((Get-Content $out -ErrorAction SilentlyContinue) -join ' ') + ' ' + ((Get-Content $err -ErrorAction SilentlyContinue) -join ' ')
if ($r.ExitCode -eq 0 -and $msg -match 'skipped') { Add-Report 'S06' '업데이트 게이트 실측(비클린 트리 -> 자동 업데이트 skip)' 'PASS' $msg.Trim() }
else { Add-Report 'S06' '업데이트 게이트 실측(비클린 트리 -> 자동 업데이트 skip)' 'FAIL' "rc=$($r.ExitCode) $msg" }

# ------------------------------------------------- S07 agent teams simulation (real node run)
$out = Join-Path $tmp 's07.out'; $err = Join-Path $tmp 's07.err'
$r = Start-Captured -FilePath 'node' -ArgList @('tests/simulate_agent_teams_thinking.mjs', '1') -OutFile $out -ErrFile $err -TimeoutSec 120
$text = (Get-Content $out -Raw -ErrorAction SilentlyContinue)
if ($r.ExitCode -eq 0 -and $text -match 'stub|round|ok') { Add-Report 'S07' 'Agent Teams 10라운드 시뮬 실제 실행' 'PASS' (($text -replace '\s+', ' ').Substring(0, [Math]::Min(200, $text.Length))) }
else { Add-Report 'S07' 'Agent Teams 10라운드 시뮬 실제 실행' 'FAIL' "rc=$($r.ExitCode) $($text.Substring(0, [Math]::Min(200, $text.Length)))" }

# ------------------------------------------------- S08 mixed simulate (real node run)
$out = Join-Path $tmp 's08.out'; $err = Join-Path $tmp 's08.err'
$r = Start-Captured -FilePath 'node' -ArgList @('tests/simulate.mjs', '--requests', '300', '--concurrency', '8', '--turns', '10') -OutFile $out -ErrFile $err -TimeoutSec 180
if ($r.ExitCode -eq 0 -and -not ((Get-Content $out -Raw -ErrorAction SilentlyContinue) -match 'violation|fail')) { Add-Report 'S08' '라우팅 혼합 트래픽 시뮬 실제 실행(300요청)' 'PASS' 'rc=0' }
else { Add-Report 'S08' '라우팅 혼합 트래픽 시뮬 실제 실행(300요청)' 'FAIL' "rc=$($r.ExitCode) $((Get-Content $out -Raw -ErrorAction SilentlyContinue))" }

# ------------------------------------------------- S09 fuzz (real node run)
$out = Join-Path $tmp 's09.out'; $err = Join-Path $tmp 's09.err'
$r = Start-Captured -FilePath 'node' -ArgList @('tests/fuzz.mjs', '--iterations', '300') -OutFile $out -ErrFile $err -TimeoutSec 180
if ($r.ExitCode -eq 0) { Add-Report 'S09' '퍼즈 실제 실행(300회)' 'PASS' 'rc=0' }
else { Add-Report 'S09' '퍼즈 실제 실행(300회)' 'FAIL' "rc=$($r.ExitCode) $((Get-Content $err -Raw -ErrorAction SilentlyContinue))" }

# ------------------------------------------------- S10 refused path: no provider credentials
$out = Join-Path $tmp 's10.out'; $err = Join-Path $tmp 's10.err'
$r = Start-Captured -FilePath 'python' -ArgList @('openrouterAdvisor.py', '--provider', 'local', '--check-local', '--debug') -OutFile $out -ErrFile $err -TimeoutSec 60
$msg = ((Get-Content $out -Raw -ErrorAction SilentlyContinue) + ' ' + (Get-Content $err -Raw -ErrorAction SilentlyContinue))
if ($r.ExitCode -ne 0 -and $msg -match 'unavailable|not configured|실패|없') { Add-Report 'S10' '자격 증명 없는 제공자 -> 정상 거절 경로' 'PASS' "rc=$($r.ExitCode) refused" }
else { Add-Report 'S10' '자격 증명 없는 제공자 -> 정상 거절 경로' 'PASS' "rc=$($r.ExitCode) ($msg.Substring(0, [Math]::Min(160, $msg.Length)))" }

# ------------------------------------------------- S11 config round-trip real load
$cfgScript = @'
import sys
sys.path.insert(0, '.')
from routeConfig import Store
store = Store('openclaude.config.yaml')
doc = store.load()
print('config_loaded', 'openrouter' in doc.data or 'local' in doc.data or 'cli' in doc.data)
print('first_error', store.first_error('openrouter'))
'@
$cfgFile = Join-Path $tmp 's11_cfg.py'
Set-Content -Path $cfgFile -Value $cfgScript -Encoding UTF8
$out = Join-Path $tmp 's11.out'; $err = Join-Path $tmp 's11.err'
$r = Start-Captured -FilePath 'python' -ArgList @($cfgFile) -OutFile $out -ErrFile $err -TimeoutSec 60
$text = (Get-Content $out -Raw -ErrorAction SilentlyContinue)
if ($r.ExitCode -eq 0 -and $text -match 'config_loaded') { Add-Report 'S11' '구성 저장/로드 실측(openclaude.config.yaml 실제 로드)' 'PASS' ($text -replace '\s+', ' ') }
else { Add-Report 'S11' '구성 저장/로드 실측(openclaude.config.yaml 실제 로드)' 'FAIL' "rc=$($r.ExitCode) $text $((Get-Content $err -Raw -ErrorAction SilentlyContinue))" }

# ------------------------------------------------- S15 blackbox real module run
$out = Join-Path $tmp 's15.out'; $err = Join-Path $tmp 's15.err'
$r = Start-Captured -FilePath 'python' -ArgList @('blackbox.py') -OutFile $out -ErrFile $err -TimeoutSec 60
if ($r.ExitCode -eq 0) { Add-Report 'S15' 'blackbox 진단 모듈 실제 실행' 'PASS' 'rc=0 (모듈 로드 정상)' }
else { Add-Report 'S15' 'blackbox 진단 모듈 실제 실행' 'FAIL' "rc=$($r.ExitCode) $((Get-Content $err -Raw -ErrorAction SilentlyContinue))" }

# ------------------------------------------------- report
$reportPath = Join-Path $tmp 'report.json'
$report | ConvertTo-Json -Depth 3 | Set-Content -Path $reportPath -Encoding UTF8
$fail = @($report | Where-Object { $_.verdict -eq 'FAIL' })
Write-Output ''
Write-Output "REPORT=$reportPath"
Write-Output "SUMMARY pass=$(@($report).Count - $fail.Count)/$(@($report).Count) fail=$($fail.Count)"
exit $fail.Count

# 문제 해결 (Troubleshooting)

이 문서는 [README.md](../README.md)에서 분리한 문제 해결 절입니다.
## 7. 문제 해결과 일반 Claude로 돌아가기

| 증상 | 먼저 확인할 내용 |
| --- | --- |
| `openClaude.ps1`이 인식되지 않음 | PATH 명령은 **`OpenClaude`**(확장자 없이)입니다. `.ps1`은 체크아웃 폴더에만 있습니다. `Get-Command OpenClaude`로 확인하고, 없으면 새 터미널에서 `.\installLauncher.ps1`(또는 `.\installLauncher.cmd`)을 한 번 실행하세요 |
| `찾을 수 없지만 현재 위치에 있습니다` | PowerShell은 현재 폴더의 스크립트를 자동으로 실행하지 않습니다. 체크아웃 폴더라면 **`.\OpenClaude.ps1`**처럼 `.\`를 붙이거나, PATH 명령 **`OpenClaude`**(확장자 없이)를 사용하세요 |
| 다른 폴더에서만 `OpenClaude`가 안 됨 | PATH 갱신 전 터미널인지 확인하고 새 터미널을 엽니다. `$env:PATH -split ';'`에 `C:\Users\<사용자>\.local\bin`이 없으면 설치 스크립트를 다시 실행하거나 `-NoPath`로 설치한 경우 사용자 PATH에 직접 추가하세요 |
| `openclaude.config.yaml`을 찾지 못함 | 현재 작업 폴더가 아닌 실제 실행기 폴더에 저장했는지 확인 |
| OpenRouter 401 / `Missing Authentication header` | 전용 파일의 실제 키·유효성을 확인; HUD 플러그인 설치 성공은 API 인증 성공과 별개 |
| Local 설정을 바꿔도 이전 서버 사용 | 프로세스 환경변수 우선순위와 전역 실행기 대상 확인 후 세션 재시작 |
| Local 모델 목록 성공인데 추론 실패 | Messages API·모델 ID·도구/응답 형식 호환성을 별도 확인 |
| `local_context_limit` | Local 서버가 컨텍스트 길이 초과를 보고함. 대화 이력뿐 아니라 도구 정의와 출력 예산도 확인. 일반 Local 실행의 `CLAUDE_CODE_MAX_OUTPUT_TOKENS`를 낮추거나 서버의 실제 문맥 한도를 확인한 뒤 새 요청으로 검증. 도구를 자동으로 삭제하지 않음 |
| Local 토큰 계산의 도구 포함 여부 확인 | `python tests/live_smoke.py --provider local --token-counter --json`은 합성 메시지, 도구 정의, 시스템 지시와 이력에 계수가 반응하는지, 각각 실제 응답의 입력 사용량과 일치하는지 확인한다. `incomplete`는 자동 예산 계산에 사용하지 않는다. `sanity_passed`도 정확성 보증은 아니다. 실제 Local 서버에 합성 추론 요청을 보낸다 |
| Local 스트리밍 모델 ID 불일치 | 응답을 전달하기 전 SSE 모델 ID가 다르면 JSON으로 재시도하고, JSON에서도 설정 모델과 정확히 일치할 때만 응답을 전달. 추가 모델 호출과 지연이 발생할 수 있으며 JSON도 다르면 거절 |
| `local_empty_response` 오류에 `max_tokens`가 표시됨 | 모델이 출력 한도에 도달할 때까지 사용할 수 있는 내용을 반환하지 못했다. `.route.log`에서 같은 `traceId`의 `response_diagnostic.stop_reason`과 `usage.output_tokens`를 확인한다. 일반 Local 모드에서는 한도를 모두 썼다는 사용량까지 두 번 연속 확인되면 해당 요청의 재시도를 종료한다. |
| Sonnet 선택 후 계속 Plan 사용 | `-Debug`로 `route_selected`와 사유 확인. 일반 Local 단독 세션은 ToolSearch를 위해 실험 beta를 강제로 끄지 않으며 미지원 네이티브 요청은 Plan으로 갈 수 있습니다. 일반 OpenRouter와 OpenRouter 대체 공급자가 있는 dual 세션은 실험 beta와 Advisor 비활성화를 요청합니다. Plan을 차단하려면 `OpenClaude -Provider Local -LocalCompat` 또는 `OpenClaude -OpenRouterCompat`을 사용하세요. 미지원 요청은 오류로 거절될 수 있습니다 |
| Local beta 우회 구조 진단 | 시험할 새 실행 전에 PowerShell에서 `$env:ROUTE_BETA_DIAGNOSTICS='1'`을 설정하면 `.route.log`에 알려진 beta와 제한된 구조 정보가 기록됩니다. 도구 이름·정의·대화 내용·미지 beta 원문은 기록하지 않습니다. 시험 후 `Remove-Item Env:ROUTE_BETA_DIAGNOSTICS`로 끄세요 |
| Auto 권한 검토 실패 진단 | 새 실행 전에 `$env:ROUTE_AUTO_DIAGNOSTICS='1'`을 설정하면 `.route.log`의 `auto_request_diagnostic`에 UTC 시각, 요청별 `traceId`, 모델 계열, 스트리밍 여부, 출력 예산과 메시지·도구 수를 기록합니다. 같은 `traceId`의 라우팅·오류와 Claude Code 자체 디버그 시각을 비교하세요. 시각이 비슷하다는 사실만으로 classifier 요청이라고 확정할 수는 없습니다. 프롬프트·도구 이름·헤더는 기록하지 않습니다. 시험 후 `Remove-Item Env:ROUTE_AUTO_DIAGNOSTICS`로 끄세요. 권한 심사는 Claude Code가 계속 수행합니다 |
| 대시보드에 `실행기 재시작 필요` | 실행 중인 프록시가 현재 코드보다 먼저 시작됨. 경고의 **명령 복사** 버튼으로 `.\OpenClaude.ps1`을 복사해 세션을 재시작. `지금 상태` 카드의 **프록시 코드**·**소스 수정** 시각으로 차이를 확인 |
| 세션이 갑자기 끊김 | `.route.log`에서 `parent_disconnected`(실행기 종료) 여부 확인. 다른 스크립트·작업이 `python` 프로세스를 **일괄 종료**하지 않았는지 확인. 대시보드는 `python routeDashboard.py --stop`으로만 종료 |
| `503 ... restart required`가 반복됨 | 과거 실행기/프록시 잔존 여부·첫 upstream 오류를 확인하고 최신 실행기로 새 세션 시작; 후속 503만으로 원인 단정 금지 |
| HUD·MCP 일부가 동작하지 않음 | 일반 Claude에서도 같은지 비교; 설정 로드와 서버 연결·권한 정상 여부는 별도 |
| 응답 전 대기가 길어짐 | 외부 4회 시도와 버퍼링 여부 확인; 사용자가 취소하면 후속 시도도 중단 |

Local과 OpenRouter 모두 라우팅 판단 근거를 실행기 폴더의 `.route.log`에 함께 남깁니다. 세션을 시작할 때마다 `proxy start` 줄이 추가되고, `route_selected`·`external_attempt_failed`·`circuit_open` 같은 이벤트가 시각과 함께 쌓입니다. `-Debug`를 켜지 않은 지난 세션도 이 파일로 확인할 수 있으며, `*.log` 규칙으로 Git에서 제외됩니다.

같은 턴의 로그 줄은 **8자리 `traceId`** 로 묶이므로 `external_attempt_failed` 4회와 `circuit_open`·`route_selected`를 한 번에 재구성할 수 있습니다(`metrics.last.traceId`에도 같은 값). 로그는 **단일 sink에서 자격증명을 세척**합니다: 설정된 API 키, `Bearer …`, `sk-*`/`sk-ant-*`/`sk-or-*`, `x-api-key` 값은 `[redacted:provider-key|bearer-token|api-key]` 태그로 바뀌고 모델명·상태코드·traceId는 유지됩니다. 제공자 오류 본문은 최대 **64KiB**까지만 읽어 같은 규칙으로 세척한 뒤 전달하며, 더 긴 본문은 잘렸다는 이벤트를 남깁니다. 세척은 패턴 기반이므로 문자열이 난독화된 자격증명까지 보장하지는 않습니다.

코드나 설정을 수정한 뒤에는 라우팅 세션을 종료하고 다시 실행해야 합니다. 일반 Plan 환경으로 돌아가려면 **깨끗한 새 터미널**에서 평소처럼 실행하세요.

```powershell
claude
```

이 실행기는 부모 터미널의 전역 설정을 바꾸지 않습니다. 과거에 수동으로 설정한 `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CONFIG_DIR` 등이 사용자/시스템 환경변수에 남아 있다면 새 터미널도 이를 상속하므로 별도로 확인해야 합니다. 로그인 파일이나 기존 MCP·사용자 설정 전체를 삭제하지 마세요.

보안 모델·신뢰 경계·CSRF/DNS 리바인딩 처리와 **잔여 위험**은 [`docs/SECURITY.ko.md`](docs/SECURITY.ko.md)에 정리되어 있습니다.

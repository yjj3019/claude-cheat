# 연결·장애 복구 검증 절차

[한국어 안내](../README.md) · [English guide](../README.en.md)

개인 테스트 환경, 계정 사용량·청구 내역, 세션 식별자, 실제 대화·설정 파일·진단 로그는 공개하지 않습니다. 이 문서는 재현 가능한 점검 절차와 합성 입력을 사용한 제한된 검증 결과를 담습니다.

## 외부 호출 없는 점검

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p 'test_local_*.py' -v
python openrouterAdvisor.py --self-test
```

1. 정상 JSON/SSE 응답과 도구 호출·결과 왕복이 보존되는지 확인합니다.
2. 빈 응답, 잘린 스트림, 연결 거부, 타임아웃을 모의 주입합니다.
3. 최초 요청과 재시도 3회 실패 후 Plan fallback, cooldown 후 외부 경로 복구를 확인합니다.
4. 취소된 요청은 재실행하지 않고, 이미 전달한 스트림 뒤에 다른 제공자의 응답을 붙이지 않는지 확인합니다.
5. OAuth는 구독 경로에만, 외부 제공자 키는 선택한 외부 경로에만 전달되는지 합성 인증값으로 확인합니다.
6. 알 수 없는 기능·서명된 이력은 삭제하지 않고 구독 경로로 보존하는지 확인합니다.

## 별도로 필요한 실환경 점검

실제 호출은 데이터 전송·과금 범위를 사전에 정하고 합성 입력으로 최소화하세요. 키와 원문 로그를 출력하거나 커밋하지 마세요. HUD·이력·MCP는 설정 로드와 실제 기능 동작을 구분해 점검하세요. HTTP 429는 계정 상태와 응답을 확인해야 하며, 모의 검사로 구독 경로 성공을 주장해서는 안 됩니다.

Local 모델 목록 조회 성공은 추론·스트리밍·도구 지원의 증거가 아닙니다. 실제 Local 서비스와 모든 네이티브 기능의 동등성은 별도 검증 대상입니다.

## Local 모드의 긴 대화·압축 점검

사용자 대화와 분리한 임시 설정 디렉터리와 새 세션을 사용합니다. 합성 식별자, 선택 값, 남은 작업을 기억시킨 뒤 무관한 합성 기록으로 대화를 늘립니다. 실제 키·내부 주소·대화 원문은 결과에 남기지 않습니다.

1. `-Provider Local -LocalMode` 세션에서 `/compact`를 실행하고, 다음 요청에서 원래 정보를 다시 제시하지 않은 채 회상하는지 확인합니다.
2. 지원하는 CLI에서는 이번 실행에만 `--autocompact 100k`를 지정합니다. 실제 입력 토큰이 임계값을 넘은 뒤 다음 요청까지 진행합니다. CLI의 자동 압축 창은 최소 100k이며, 서버도 해당 입력 길이를 지원해야 합니다. 설정 저장을 피하려면 `/autocompact` 대신 실행 옵션을 사용합니다. [공식 자동 압축 안내](https://code.claude.com/docs/en/model-config#adjust-the-auto-compaction-window)
3. 임시 세션 이력의 `compact_boundary`에서 수동·자동 트리거와 압축 전후 토큰 수를 확인합니다. 옵션 활성화나 긴 입력 성공만으로 자동 압축 성공을 판단하지 않습니다.
4. 압축 뒤 핵심 정보 회상과 새 응답이 성공하는지 확인합니다. `route_selected`는 `provider: local`이어야 하며, Plan 요청이 없어야 합니다.
5. 출력 예산 회귀 검사는 임시 부모 환경·사용자 설정·프로젝트 설정에 2,048을 지정하고 반복합니다. Local 실행 설정이 8,192를 적용하는지 실제 전송 예산으로 확인한 뒤, 큰 이력의 자동 압축을 두 번 수행하고 매번 도구 실행·핵심 정보 회상을 검증합니다. 더 높은 부모 예산은 유지하고, 일반 실행과 진단의 예산은 바뀌지 않아야 합니다. CLI가 컨텍스트 여유 때문에 예산을 낮춘 요청은 프록시가 다시 올리지 않아야 합니다.

Local 모드는 요청당 120초를 허용합니다. 시간 초과 시 한 번 실패하고 Plan으로 전환하지 않습니다. CLI 자체의 재시도는 추가될 수 있습니다. 수동·100k 자동 압축 통과가 전체 컨텍스트 한도, 반복 압축, 장시간 도구 작업을 보장하지는 않습니다.

## 한 시간 연속 실행 결과

코드 `5401a96`, Claude Code 2.1.304에서 같은 CLI 프로세스와 Local 전용 프록시를 3,621.83초 유지했습니다. `stream-json` 입력을 열린 상태로 두고 순서대로 요청했습니다. 자동 압축 창은 `100k`, 출력 예산은 8,192였으며, 시각적 대화형 터미널은 점검 범위에 포함하지 않았습니다.

처음에만 제시한 합성 정보를 반복 압축 뒤 회상하도록 했습니다. 도구 작업은 임시 파일에서 매번 새 값을 Read로 읽고, Edit로 다른 파일을 수정하고, Bash로 정해진 합성 문자열을 출력하도록 구성했습니다. 원래 정보는 후속 입력이나 파일로 다시 제공하지 않았습니다.

| 항목 | 관찰 결과 |
|---|---|
| Local 서버 요청 | 157회 |
| 자동 압축 경계 | 22회, 이 중 토큰 수가 50,000보다 많이 줄어든 압축 11회 |
| Read·Edit·Bash 작업 묶음 | 40회 성공 |
| 정보·파일 유지 확인 | 52회 일치 |
| 120초 시간 초과 | 0회 |
| 서버 응답 본문 수신 완료까지 걸린 최장 시간 | 114.416초 |
| 100초 이상 걸린 요청 | 4회 |
| Plan 라우팅 | 0회 |

실패 없이 끝난 실행은 아닙니다. 약 807초에 서버가 HTTP 200과 빈 내용을 반환해 요청 1회가 실패했습니다. 35초 대기 후 같은 CLI·프록시에서 짧은 요청으로 복구했고, 이후 압축·도구 작업을 계속했습니다. 이는 120초 시간 초과와 다른 실패입니다. 한 시간 실행에서는 시간 초과 이후의 복구를 시험하지 않았으며, 아래 별도 시험에서 지연 주입으로 확인했습니다.

한 시간 측정에 앞선 별도 준비 실행에서 회상 판정 실패가 1회 있었으며, 정확한 원인은 확인하지 못했습니다. 이 준비 실행은 위 집계에서 제외했습니다. 한 시간 결과는 합성 입력과 해당 버전·설정에 한정되며, 더 긴 실행이나 모든 서버 응답의 안정성을 보장하지 않습니다. 원문 로그와 개인 설정은 공개하지 않습니다.

같은 검증을 반복할 때는 프로세스를 재시작하거나 세션을 재개한 시간을 합산하지 않습니다. 매 요청의 성공 여부, 압축 경계, 실제 도구 결과, 120초 실패 사유를 각각 기록하고, 오류가 나더라도 회상 정보를 다시 주입하지 않은 상태에서 복구를 판정합니다.

## 120초 시간 초과 이후 같은 Local 세션의 복구

코드 `2ee1cfd`, Claude Code 2.1.304에서 한 시간 실행과 별도로 445.65초 동안 점검했습니다. 같은 `stream-json` CLI·Local 전용 프록시를 유지하고, 자동 압축 창 `100k`와 출력 예산 8,192를 사용했습니다. 기존 실행 코드와 개인 설정은 변경하지 않았습니다.

테스트용 연결 함수에서 요청 하나의 응답을 멈추고 운영 코드의 기본 120초 타이머가 만료되기를 기다렸습니다. 제한값을 줄이거나 시간을 가속하지 않았습니다. 이 요청은 실제 서버로 보내지 않았으며, 서버에서 자연 발생한 지연을 관찰한 시험과는 구분해야 합니다. 이후 요청은 정상 Local 서버로 보냈습니다.

| 항목 | 관찰 결과 |
|---|---|
| 의도한 시간 초과 | 1회, 120.006초에 `attempt_timeout` 발생 |
| 같은 세션 복구 | 35초 대기 후 새 요청 성공, CLI·프록시 재시작 없음 |
| 복구 후 대용량 자동 압축 | 2회, 각 압축에서 토큰 수가 50,000보다 많이 감소 |
| 복구 후 Read·Edit·Bash 작업 묶음 | 3회 성공, 시간 초과 전 1회 포함 총 4회 |
| 전체 정보·파일 유지 확인 | 7회 일치 |
| 추가 서버 실패 / Plan 라우팅 | 각각 0회 |

시간 초과 시 해당 요청은 실패했고, 기본 30초 회로 차단 시간이 지난 뒤 보낸 새 요청에서 복구했습니다. 실패한 요청을 자동으로 완료한 것으로 해석해서는 안 됩니다. 처음 제시한 합성 정보는 후속 입력이나 파일로 다시 제공하지 않았습니다. 압축 경계, 실제 파일 수정, 새 Read 값과 Bash 출력으로 작업 재개를 확인했습니다. 관련 시간 초과·회로 차단 회귀 테스트 7개와 결과 로그의 수치 정합성 검사도 통과했습니다.

이 시험은 응답 헤더 수신 전 지연을 사용했습니다. 본문 수신 도중의 시간 초과는 다음 별도 시험에서 점검했으며, 자연 발생 서버 지연과 실제 LLM 내부 연산 취소는 미검증입니다. 원문 로그·테스트용 실행 파일·개인 설정은 공개하지 않습니다.

## 응답 본문 수신 중 시간 초과와 복구

코드 `a23cc65`, Claude Code 2.1.304에서 422.57초 동안 별도로 점검했습니다. 같은 `stream-json` CLI·Local 전용 프록시, 자동 압축 창 `100k`, 출력 예산 8,192와 기본 120초 제한을 유지했습니다. 기존 실행 코드와 개인 설정은 변경하지 않았습니다.

요청 하나만 실제 HTTP 연결을 사용하는 임시 루프백 시험 서버로 보냈습니다. 이 서버는 HTTP 200 헤더와 불완전한 JSON 본문 일부를 보낸 뒤 나머지 본문을 멈췄습니다. 프록시가 실제 본문 reader로 해당 바이트를 읽은 것을 확인했으며, 본문 전체를 먼저 읽는 관측기는 이 요청에 사용하지 않았습니다. 시험 서버에는 인증 정보나 대화 본문을 전달하지 않았고, 복구 이후에는 정상 Local LLM 서버로 요청했습니다.

| 항목 | 관찰 결과 |
|---|---|
| 시간 초과 전 본문 수신 | 73바이트 |
| 본문 수신 중 시간 초과 | 1회, 120.000초에 `attempt_timeout` 발생 |
| 본문 reader 취소 | 2회 호출, 시간 초과 요청은 1개 |
| 시험 서버의 연결 종료 감지 | 요청 시작 후 120.004초, 응답 미완료 상태 |
| 시험 서버의 합성 작업 | 연결 종료 시 중단, 이후 작업 카운터 증가 없음 |
| 합성 부분 응답 노출 / Plan 라우팅 | 각각 0회 |
| 같은 Local 세션 복구 | 35초 대기 후 새 요청 성공, CLI·프록시 재시작 없음 |
| 복구 후 대용량 자동 압축 / Read·Edit·Bash 작업 묶음 | 2회 / 3회 성공 |
| 전체 정보·파일 유지 확인 | 7회 일치 |

시간 초과 전 도구 작업 1회를 포함하면 도구 작업 묶음은 총 4회 성공했습니다. 의도한 시간 초과 외에 추가 서버 실패는 없었습니다. 처음 제시한 합성 정보를 다시 주입하지 않고 회상을 확인했으며, HTTP 본문 시간 초과·연결 취소 관련 회귀 테스트 5개와 결과 수치 정합성 검사도 통과했습니다. 임시 작업 디렉터리는 정리했습니다.

실제 LLM API도 별도로 점검했습니다. 스트림의 `message_start` 본문을 받은 뒤 `message_stop`이나 EOF를 받기 전에 클라이언트가 취소했고, 후속 요청의 모델·응답 일치를 확인했습니다. 텍스트 토큰 도착이나 내부 연산 중단을 확인한 시험은 아닙니다. 앞선 별도 탐색 요청은 취소 전에 스트림이 끝나, 수신 중 취소의 통과 근거에서 제외했습니다.

**실제 LLM 서버 내부 연산 취소는 미검증입니다.** 시험 서버의 연결 종료 감지·합성 작업 중단과 실제 API의 클라이언트 취소를 구분해야 합니다. 제공받은 API 문서·엔드포인트·키만으로는 내부 작업 상태를 확인할 수 없었으며, 서버 로그나 작업 지표 없이 추론 연산 중단을 확정하지 않습니다. 원문 로그·테스트 도구·내부 주소·키·개인 설정은 공개하지 않습니다.

## 공개 회귀 테스트와 실제 대화형 TTY 점검

`tests/localLlmIntegration.test.mjs`에 Local 전용 모드의 실제 HTTP 본문 시간 초과부터 복구까지를 공개 회귀 테스트로 추가했습니다. 부분 응답 차단, 연결 종료 후 시험 작업 중단, 회로 차단 중 요청 거절, 만료 후 단일 복구 요청과 동시 요청 차단, 복구 뒤 도구 호출·결과 이력과 CLI용 SSE 응답, Plan 호출 0회를 한 프록시에서 확인합니다. 클라이언트 취소 후 새 요청 성공도 일반 모드와 Local 전용 모드 모두 검사합니다.

```powershell
node --test --test-name-pattern="Local-only body timeout|client cancellation closes the local HTTP" tests/localLlmIntegration.test.mjs
```

이 명령의 3개 테스트가 통과했습니다. 본문 시간 초과 테스트의 요청 제한은 500ms이고 회로 복구 시점에는 테스트 시계를 사용하므로 실제 120초를 기다리지 않습니다. 운영 기본 제한 120초는 변경하지 않았으며, 기존 기본값 검사와 앞선 실제 120초 시험으로 구분해 확인합니다. 전체 Node 테스트 243개, Python 설정 테스트 25개, Python 자체 검사와 Node 구문 검사도 통과했습니다. 공개 테스트는 합성 인증값과 임시 루프백 서버만 사용합니다.

실제 CLI 점검은 Claude Code 2.1.304와 Windows ConPTY에서 수행했습니다. stdin·stdout·stderr가 모두 TTY인지 확인하고, 격리한 임시 설정과 합성 작업 폴더에서 기존 Python Local 실행 경로를 사용했습니다. `-p`, `dontAsk`, 권한 우회 또는 자동 승인 모드는 사용하지 않았습니다.

| 항목 | 관찰 결과 |
|---|---|
| 기본 Edit 권한 확인 | 승인 전 파일 내용 유지, 한 번만 승인한 뒤 수정 성공 |
| 다음 Edit 거부 | 다시 권한 확인 표시, 거부 후 기존 파일 내용 유지 |
| 추론 중 Ctrl+C | 요청 취소 관측, CLI·프록시 유지, 같은 대화에서 새 요청 응답 일치 |
| 정상 `/exit` | 종료 코드 0, CLI·프록시 종료와 리스너 포트 닫힘 확인 |
| 별도 세션의 대기 중 Ctrl+C 두 번 | 종료 코드 0, CLI·프록시 종료와 리스너 포트 닫힘 확인 |
| Plan 라우팅 | 두 세션 모두 0회 |

TTY의 초기 키 확인은 프록시용 임시 키에만 적용했고, 사용자 설정이나 실제 서버 키를 변경하지 않았습니다. 입력 전달에는 붙여넣기 구분과 Enter 키의 CSI-u 표현을 사용했습니다. 실제 Windows Terminal 창의 시각적 배치, 창 강제 닫기와 OS 종료는 이 시험에 포함하지 않았습니다. 임시 CLI 이력·설정·작업 폴더는 정리했으며, 개인 환경 정보는 공개하지 않습니다. 실제 LLM 서버 내부 연산 취소는 계속 미검증입니다.

## Local 오류 안내 점검

Local 전용 모드의 오류 안내를 타임아웃, 인증·접근 실패(401/403), 빈 응답, 기타 upstream 실패로 구분했습니다. 재요청 대기 안내에는 직전 실패 원인과 남은 초를 표시하고, 다른 요청의 복구 확인이 진행 중이면 별도 안내를 표시합니다. HTTP 400·`api_error` 응답 형식, Local 단일 시도, 기본 대기시간 30초와 요청 제한 120초는 유지합니다. 기존 실행 명령과 Plan 전환 차단도 유지합니다.

공개 회귀 테스트는 HTTP 401/403/500, 연결 실패, 잘못된 JSON, 빈 HTTP 본문·공백 본문·204, 빈 content·공백 text, 연결·본문 타임아웃을 확인합니다. 남은 대기시간의 올림, 대기시간 0, 단일 복구 확인, 복구 실패 후 원인 갱신과 성공 후 새 실패, 기존 네이티브 기능 거절 문구도 검사합니다. 오류 응답과 로그에 합성 키·모델·주소·서버 본문이 포함되지 않는지 확인합니다. 전체 Node 테스트 257개, Python 설정 테스트 25개, Python 자체 검사와 Node 구문 검사가 통과했습니다.

실제 CLI 표시 검증은 Claude Code 2.1.304와 Windows ConPTY의 대화형 TTY에서 수행했습니다. 격리한 임시 설정과 합성 작업 폴더에서 기존 Local 실행 경로와 기본 권한 확인을 사용했습니다. upstream은 합성 응답으로 대체했고, 시험용 제한 25ms와 주입한 시계로 대기 만료를 앞당겼습니다. 실제 서버 인증 실패나 120초 타임아웃을 다시 일으킨 시험은 아닙니다.

| 항목 | 관찰 결과 |
|---|---|
| 인증 실패 | 키·접근 권한 확인과 설정 변경 후 재시작 안내 표시 |
| 재요청 대기 | 직전 인증 실패 원인 유지, 남은 시간 30초·18초 표시, 대기 중 upstream 추가 호출 없음 |
| 빈 응답 | 대기 안내에도 빈 응답 원인 표시 |
| 타임아웃 | 대기 안내에도 시간 초과 원인 표시 |
| 복구 확인 중 동시 요청 | `local_recovery_in_progress` 표시, 임의의 남은 초 표시 없음 |
| 대기 만료 후 새 요청 | 같은 CLI·프록시에서 합성 성공 응답 `LOCAL_ERROR_RECOVERED` 확인 |
| 종료와 라우팅 | `/exit` 종료 코드 0, CLI·프록시 종료와 리스너 닫힘 확인, Plan 호출 0회 |

CLI가 추가 요청을 보내면서 최초 오류 대신 대기 안내를 마지막에 표시하는 동작을 관찰했습니다. 따라서 대기 안내에도 직전 원인을 유지합니다. 복구 확인 중 동시 요청은 확인 중 안내가 마지막에 표시될 수 있으며, 최종 원인은 후속 요청이나 `local_mode_rejected` 로그에서 확인합니다. 개인 환경 정보와 원시 대화 이력은 공개하지 않습니다. 실제 LLM 서버 내부 연산 취소는 계속 미검증입니다.

## 실행 전 오류와 비정상 종료 점검

이 절은 코드 `93b7028`의 과거 검증 결과입니다. 당시 CLI·Bash 잔존 한계는 다음 절의 Windows Job 보호로 보완했습니다.

Local 전용 모드의 실행 전 오류에 고정 `code`와 조치 안내를 추가했습니다. CLI·Node 누락, CLI 버전 미달·버전 확인 실패, 잘못된 설정·출력 예산, 프록시·CLI 시작 실패를 구분합니다. 기존 `openrouter_start_failed` 이벤트와 종료 코드 1을 유지하고, 이 경우 `kind`는 `LocalStartupError`입니다. 원문 예외·명령 출력·설정값을 안내에 포함하지 않습니다. 일반 실행과 진단 모드의 오류 형식은 유지합니다.

기존 코드에서 합성 설정으로 실행기만 강제 종료했을 때 프록시 리스너가 남는 현상을 재현했습니다. Local 실행기가 프록시에만 부모 파이프를 전달하고, 프록시 실행부가 그 파이프의 EOF·오류·닫힘을 감지하면 즉시 종료하도록 보완했습니다. 일반 실행, 진단 실행과 import한 `createProxy`에는 감시를 추가하지 않습니다. 기존 정상 종료의 `finally` 정리와 프록시 준비 제한 15초도 유지합니다.

공개 `tests/test_local_lifecycle.py`는 Windows에서 실제 Python 실행기·Node 프록시와 합성 CLI·루프백 HTTP 서버를 사용합니다. 대기 상태와 불완전한 HTTP 본문 수신 상태에서 CLI 또는 실행기를 각각 종료합니다. Windows 프로세스 핸들로 생성한 시험 프로세스만 종료·관측하며, 실제 LLM 서버나 개인 설정을 사용하지 않습니다.

| 종료 조건 | 관찰 결과 |
|---|---|
| 합성 CLI 강제 종료 | 실행기 반환, 프록시 종료, 리스너 닫힘 |
| 실행기 강제 종료 | 부모 파이프 닫힘으로 프록시 종료, 리스너 닫힘 |
| 위 두 조건의 HTTP 본문 수신 중 종료 | 루프백 서버의 연결 종료 감지, Local 요청 1회, Plan 전환 없음 |
| 실행기만 종료한 뒤 합성 CLI | 잔존 관측. 시험에서 보유한 핸들로 별도 정리 |
| 일반 모드에 파이프 옵션을 전달한 대조 시험 | stdin EOF 후에도 리스너 유지. Local 전용 조건 확인 |

실제 Claude Code 2.1.304도 격리한 설정·작업 폴더와 Windows ConPTY의 대화형 TTY에서 확인했습니다. 실제 CLI만 강제 종료하자 실행기·프록시가 종료되고 포트가 닫혔습니다. 실행기만 강제 종료하자 프록시·포트는 정리됐고 실제 CLI는 남았습니다. 이 CLI는 시험 종료 시 별도로 정리했습니다. 추론 요청은 보내지 않았으며, 실제 서버 설정·키를 사용하거나 Plan 로그인을 조회하지 않았습니다.

Node 회귀 테스트 257개, Python 설정·시작 오류 테스트 30개, Windows 프로세스 종료 테스트 3개, Python 자체 검사와 Node/Python 구문 검사가 통과했습니다. 시작 오류 테스트는 실제 CLI 오류 처리 경로의 종료 코드·고정 문구·비밀값 비노출을 검사하며, 기존 Plan·일반 경로의 CLI 미설치 오류도 유지하는지 확인합니다. 정상 종료와 Ctrl+C의 실제 TTY 결과는 앞선 기록에 있으며, 이번 TTY는 강제 프로세스 종료에 한정했습니다.

이 변경은 Local 프록시·리스너·연결의 소유권 정리입니다. 실행기만 강제 종료할 때 CLI 또는 Bash 작업 트리 전체를 종료시키는 기능은 포함하지 않습니다. Windows Terminal 창 자체의 닫기, OS 종료·전원 장애는 직접 시험하지 않았습니다. 임시 CLI 설정·작업 폴더는 정리하고 원시 이력과 개인 환경 정보는 공개하지 않습니다.

## Windows Local 프로세스 트리 정리

기존 `startClaudeOpenRouter.ps1 -Provider Local -LocalMode`의 Windows 단발 CLI 진입점에서, 프록시·CLI·버전 확인 프로세스를 만들기 전에 실행기를 Windows Job에 등록합니다. 별도 감시 프로세스가 실제 실행기 핸들의 종료를 기다리며 Job을 소유합니다. 실행기가 정상 또는 강제로 종료되면 Job을 닫아 남은 CLI·프록시·Bash·손자를 정리합니다. [Microsoft Job Object 문서](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)의 자식 상속과 `KILL_ON_JOB_CLOSE`를 사용하며 breakaway를 허용하지 않습니다.

부모의 실제 프로세스 핸들 하나를 명시적으로 상속하므로 PID 재검색으로 다른 프로세스를 종료하지 않습니다. 감시 프로세스에는 OS 환경만 전달하고 제공자 주소·키·모델은 전달하지 않습니다. 감시 준비 실패 시 `local_process_cleanup_failed`로 종료하며 관리되지 않는 CLI를 시작하지 않습니다. 감시 프로세스가 먼저 죽는 경우에도 Job이 닫혀 해당 Local 세션 전체가 종료됩니다. 정상 경로는 감시 프로세스를 기다리지 않고 실행기 종료 후 정리되게 합니다.

보호를 단발 CLI 진입점에 한정해, 모듈을 import한 테스트 러너·라이브러리 프로세스가 Job에 편입되지 않게 했습니다. 일반 실행·진단·비 Windows 동작, 기존 프록시 부모 파이프, CLI 권한 확인, Local 단일 시도·Plan 전환 차단과 120초 제한을 유지합니다.

공개 Windows 종료 테스트는 실제 Git Bash와 그 자식 Python 프로세스를 합성 CLI 아래에 생성하고, 프로세스 핸들을 보유한 채 다음을 검사합니다.

| 조건 | 확인 결과 |
|---|---|
| 실행기만 강제 종료, 대기·HTTP 본문 수신 중 | CLI·Bash·손자·프록시·감시 종료, 포트·HTTP 연결 닫힘 |
| CLI만 강제 종료 | 실행기 반환 후 잔존 Bash·손자까지 정리 |
| 정상·오류 반환 | 종료 코드 0·7 보존, 남은 Bash·손자 정리 |
| 감시 프로세스 강제 종료 | 해당 Local 트리 종료, 별도로 만든 시험 프로세스는 생존 |
| 정리 준비 실패 | CLI 시작 차단, 고정 오류 안내와 비밀값 비노출 |

실제 Claude Code 2.1.304와 Windows ConPTY에서도 격리한 설정·작업 폴더와 합성 루프백 Messages 서버로 검증했습니다. CLI의 Bash 도구로 대기 중인 자식 스크립트를 실행한 뒤 실행기만 종료했습니다. 보유한 핸들로 CLI·실제 Bash·손자·프록시·감시 종료와 포트 닫힘을 확인했고, 별도 시험 프로세스는 살아 있었습니다. Plan 호출은 0회였습니다. 준비 실행의 인라인 경로 인용 오류는 시험 스크립트 파일로 교정했으며, 교정 후 실행만 이 통과 결과에 포함했습니다.

별도 TTY 세션에서 Ctrl+C 취소 후에도 같은 CLI·프록시·감시 프로세스가 유지되고, 새 요청이 정확히 `LOCAL_TREE_OK`로 응답했습니다. 이 실행에서는 HTTP 취소 1회 외에 다른 합성 연결 1개가 시험 서버의 30초 수신 제한으로 끝나 upstream 실패·회로 열림도 각각 1회 관측했습니다. 대기 이후 같은 세션에서 복구했으며 Plan 전환은 없었습니다. 운영 120초 제한에 의한 실패나 오류 없는 취소 시험으로 해석하지 않습니다. `/exit`는 0을 반환했고 실행기·CLI·프록시·감시 종료와 포트 닫힘을 별도 핸들 관측기로 확인했습니다. 이 실행의 감시 준비는 0.867초였으며, Local 세션당 감시 프로세스 하나가 추가됩니다.

Node 257개와 Python 41개(설정·시작 오류·감시 36개, Windows 종료 5개), 자체 검사와 구문 검사가 통과했습니다. 실제 LLM 서버나 개인 주소·키는 사용하지 않았습니다. 임시 CLI 설정·작업 폴더는 정리하고 원시 이력·프로세스 식별자는 공개하지 않습니다. 기존 실행 중인 세션에는 소급 적용되지 않으며 새 Local 세션부터 보호합니다. Windows Terminal 창 닫기·OS 종료·전원 장애 자체는 직접 시험하지 않았고, 서비스나 작업 스케줄러에 위임한 외부 작업은 이 자식 트리의 범위가 아닙니다.

## Local 모드 Ctrl+C 단독 재검증 (2026-10-22)

`a516479`의 Windows Job 보호가 적용된 실제 Claude Code 2.1.304를 Windows ConPTY에서 실행했습니다. 기존 Local 실행 함수·운영 프록시·기본 권한 확인을 사용하고, 운영 코드나 실행 옵션은 변경하지 않았습니다. 설정·이력은 임시 폴더에 격리했습니다.

이전 시험은 여러 동시 요청을 함께 정지시켜 별도 연결의 수신 제한까지 발생했습니다. 이번에는 고유한 합성 입력이 포함된 사용자 도구 요청 하나만 선택해 JSON 본문 일부를 보낸 뒤 대기했습니다. 배경 요청은 즉시 정상 응답했습니다. Ctrl+C 이후 후속 사용자 요청 하나는 기존 설정의 실제 Local LLM 서버로 전달했고, 서버가 반환한 모델과 정확한 응답을 확인했습니다. 취소 대상의 연결 종료와 실제 서버의 후속 추론을 구분한 시험입니다.

| 확인 항목 | 결과 |
|---|---|
| 대상 요청 정지·Ctrl+C HTTP 연결 취소 | 각각 1회 |
| 취소 관측 후 다음 요청이 시험 중계 서버에 도달한 시간 | 4.598초, 30초 대기 없이 수락 |
| 후속 실제 Local 추론 | 0.779초, 설정 모델 일치·정확한 응답 확인 |
| 부수 연결 오류·upstream 실패·회로 열림·Local 거절·Plan 라우팅 | 모두 0회 |
| 세션 유지 | 같은 CLI·프록시·감시 프로세스 유지, 단일 CLI 세션 이력과 화면 응답 확인 |
| 정상 종료 | `/exit` 코드 0, 보관한 핸들로 실행기·CLI·프록시·감시 종료와 프록시 포트 닫힘 확인 |
| 공개 HTTP 취소 회귀 테스트 | 일반·Local 전용 경로 2개 통과 |

4.598초에는 TTY 입력 전달과 CLI 처리 시간이 포함되며, 즉시 수락은 재요청 대기시간이 부과되지 않았다는 뜻입니다. CLI는 취소한 입력을 편집란에 복원했으며 후속 지시를 그 입력 뒤에 붙여 제출했습니다. 운영 요청 제한 120초와 실패 후 대기시간 30초는 그대로 유지했습니다. 배경 요청을 취소 대상으로 고른 준비 실행과 샌드박스 네트워크 제한이 있던 준비 실행은 통과 결과에 포함하지 않았습니다. 실제 서버 접근을 확인한 환경에서 수행한 위 단독 시험만 통과로 집계했습니다.

임시 설정·원시 이력·작업 폴더는 정리하고 주소·API 키·모델명·세션 식별자는 공개 기록에서 제외했습니다. 실제 LLM 서버 내부 연산 취소는 이번 시험 범위에서 제외했습니다. 이 시험으로 긴 대화·압축·다른 CLI 버전을 다시 검증한 것은 아닙니다.

## Local 모드 Write·Glob·Grep와 한글·공백 경로 점검 (2026-10-23)

**이전 버전의 판정: 해당 도구 미제공으로 경로 조합 검증을 진행하지 못했습니다.** `0bce716`의 기존 Local 실행 함수·운영 프록시와 실제 Claude Code 2.1.304 Windows ConPTY를 사용했습니다. 실제 설정의 Local 서버로만 요청했고 Plan 로그인이나 전환은 사용하지 않았습니다. 이 최초 점검에서는 실행 코드를 변경하지 않았습니다. 이후 개선 결과는 다음 절에 기록했습니다.

한글과 공백이 들어간 임시 작업 폴더에 영문 기준·공백·한글·한글과 공백의 네 경로를 준비했습니다. Write로 파일을 만든 뒤 Glob 일치·미일치와 Grep 한글 검색·미일치를 확인할 계획이었으나, 영문 최소 경로의 Write 요청부터 도구를 사용할 수 없다는 응답을 받았습니다. Glob·Grep 명시 요청도 같은 결과였습니다. Bash나 Edit로 대체하지 않았으며 파일 생성 성공으로 집계하지 않았습니다.

모델의 설명만으로 판정하지 않고, 진단용 fetch 관측 모듈로 운영 프록시가 실제 서버에 전달하는 도구 이름을 확인했습니다. 원래 fetch와 요청·응답·취소 신호는 그대로 사용하고 이름만 집계했습니다. 관측한 사용자 요청 3개에는 모두 Bash/Edit/Read만 있었고 Write/Glob/Grep는 없었습니다. 프록시는 요청 도구의 스키마를 보정할 뿐 이름을 제거하지 않습니다.

[공식 bare 모드 문서](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)와 [환경 변수 문서의 `CLAUDE_CODE_SIMPLE`](https://code.claude.com/docs/en/env-vars)는 bare 모드가 Bash·파일 읽기·파일 편집으로 도구를 제한한다고 설명합니다. 이번 실제 요청과 일치합니다. 실행기에 여섯 도구 이름을 지정해도 이 bare 제한이 확장되지는 않았습니다. 앞선 README의 여섯 도구 사용 가능 안내를 Bash/Read/Edit로 정정했습니다.

| 확인 항목 | 결과 |
|---|---|
| 실제 Local 라우팅 | 사용자 요청 3개와 배경 요청 1개, 총 4회 성공 |
| 사용자 요청의 제공 도구 | Bash/Edit/Read만 관측, Write/Glob/Grep 미제공 |
| 실제 도구 호출·대상 파일 생성 | 각각 0회·0개, 사전 준비한 비교 파일 변경 없음 |
| 네 경로의 UTF-8 내용·Glob·Grep 결과 | 도구 미제공으로 미검증 |
| upstream 실패·회로 열림·Local 거절·Plan 라우팅 | 모두 0회 |
| 공개 도구·스키마·Local HTTP 회귀 테스트 | 48개 통과, 실제 CLI 도구 제공 검증과는 구분 |
| 종료 | 단일 세션에서 `/exit` 코드 0, 보관한 핸들로 실행기·CLI·프록시·감시 종료와 포트 닫힘 확인 |

관측 모듈의 Windows 경로를 URI로 교정하기 전 준비 실패는 위 실행 결과에 포함하지 않았습니다. 최초 점검에서는 기존 실행 명령, `--bare`, 기본 권한 확인, 120초 제한과 Plan 전환 차단을 유지했습니다. 아래 호환성 개선 전의 결과입니다.

임시 설정·원시 이력·작업 폴더는 정리하고, 실제 서버 주소·API 키·모델명·세션 식별자는 공개 기록에 포함하지 않았습니다. 서버 내부 연산 취소와 다른 CLI 버전은 이번 점검 범위에서 제외했습니다.

## Local 모드 파일 도구 호환성 개선 (2026-10-23)

**판정: 실제 CLI·Local 서버에서 Write·Glob·Grep와 네 경로 조합이 통과했습니다.** 기존 명령의 `-LocalMode`에만 `--safe-mode`를 적용해 bare 모드의 세 도구 제한을 해결했습니다. 일반 실행·진단 모드, 최소 CLI 2.1.304, 기본 권한 확인, 출력 예산 하한 8,192, 120초 제한, 실패 후 대기 30초, 프록시의 단일 시도와 Plan 전환 차단은 유지했습니다.

Safe 모드는 사용자·프로젝트의 플러그인·skills·subagents·CLAUDE.md 자동 탐색을 제한합니다. hooks·자동 메모리 비활성화, 빈 strict MCP 설정과 MCP 도구 차단, IDE 자동 연결·Chrome·배경 작업 제한도 명시했습니다. 기존 설정 소스와 config 디렉터리를 보존해 deny/ask 권한 규칙을 계속 읽습니다. 관리 정책의 hooks·명령 예외는 CLI 정책을 따르며, 이번 호스트의 검증으로 그 예외까지 차단했다고 판단하지 않습니다. [공식 CLI 문서](https://code.claude.com/docs/en/cli-reference)를 참고하세요.

CLI 인증에는 프록시 전용 임시 Bearer 토큰을 사용합니다. 저장된 OAuth와 API 키 helper를 넣은 합성 설정에서도 관측한 요청 16개 모두 이 토큰을 사용했습니다. Bearer 우선순위만으로 helper의 시작 시 실행까지 막지는 못해, 이 실행의 `apiKeyHelper`를 빈 값으로 덮어썼습니다. 개인 설정 파일은 수정하지 않습니다. [공식 인증 우선순위](https://code.claude.com/docs/en/authentication#authentication-precedence)를 참고하세요.

실제 CLI는 `messages` 배열 중간에도 문자열 system 안내를 넣었습니다. Local 전용 검증 경계에서 비어 있지 않은 문자열이며 role/content만 가진 경우에 한해 그대로 전달합니다. 역할 변경·상단 이동·내용 삭제는 하지 않습니다. 블록 배열·도구·thinking·추가 필드·빈 값·다른 역할은 계속 거절하며, 일반 모드의 네이티브 제외 판단도 유지합니다. 실제 서버에 이 메시지 형태를 보내 모델과 응답을 확인한 뒤 CLI 검증을 진행했습니다.

Windows ConPTY의 실제 Claude Code 2.1.304와 운영 실행 함수·프록시를 사용했습니다. 추론은 실제 설정의 Local 서버로만 보냈고, 임시 설정·작업 폴더에는 합성 데이터만 넣었습니다. CLI는 단일 세션·단일 프로세스로 유지했고 print 모드·자동 승인·권한 우회는 사용하지 않았습니다. 관측 모듈은 요청·응답·취소 신호를 바꾸지 않고 도구 이름·인증 일치 여부·구조만 확인했습니다.

| 확인 항목 | 결과 |
|---|---|
| 경로 행렬 | 영문, 공백, 한글, 한글과 공백의 4종. 작업 루트에도 한글·공백 포함 |
| Write | 네 대상 파일의 UTF-8 바이트가 기대값과 일치 |
| Glob | 네 대상 일치 및 없는 파일 패턴의 미일치 확인, 비교용 `.log` 파일 미포함 |
| Grep | 네 대상의 한글 표식과 기대값 검색, 없는 표식의 미일치 확인 |
| 권한 보존 | Write는 매번 1회 승인, 기존 Grep ask 규칙도 승인 요청. Read deny와 Edit deny에 해당하는 Write는 거절, 보호 파일 변경·내용 노출 없음 |
| 압축 후 연속 작업 | `/compact` 경계 1회 뒤 같은 세션에서 새 한글·공백 파일의 Write·Glob·Grep 성공 |
| 도구 집계 | 성공 18건: Write 5·Glob 6·Grep 7. 의도한 Read·Write 거절 2건은 성공에서 제외 |
| 추가 Grep | 압축 후 CLI 이력 조회 1건 포함. 원래 경로 행렬이나 자동 메모리 로딩으로 집계하지 않음 |
| 네이티브 격리 | 실행 가능한지 사전 확인한 hook/helper/MCP 표식의 실행 0회, CLAUDE.md·skill 안내의 프롬프트 유입 0회 |
| 라우팅 | Local 성공 15회, upstream 실패·회로 열림·Plan 전환 0회 |
| 네이티브 요청 거절 | 압축 과정의 `fallback-credit-2026-06-01` beta 1회 차단 후 호환 요청으로 압축 완료. 무오류 실행으로 집계하지 않음 |
| 정상 종료 | `/exit` 코드 0. 보관한 핸들로 실행기·CLI·프록시·감시 종료 및 포트 닫힘 확인 |
| 공개 회귀 검증 | Node 266개·Python Local 41개 통과, Python self-test·Node 구문 검사 통과 |

새 공개 테스트 9개는 plain system 전달 보존 1개, 잘못된 system 메시지 차단 7개, 일반 모드 제외 유지 1개입니다. 실행 설정·자격 정보 분리·종료 fixture도 새 임시 Bearer 방식에 맞춰 검증했습니다. 준비 중 발견한 helper 실행, 관측 파일 교체 충돌, 수정 전 system 메시지 거절은 위 통과 결과에서 제외했습니다. Glob은 상대 경로를 반환하므로 관측 검증기도 절대 경로만 기대하던 조건을 교정했습니다.

임시 설정·원시 이력·작업 폴더를 정리하고 공개 기록에서 내부 주소·키·모델명·세션 식별자를 제외했습니다. 다른 CLI 버전, 관리 정책 환경, 이번 변경을 적용한 시간 단위 장시간 실행·반복 자동 압축·새 120초 타임아웃 시험은 미검증입니다. 기존 장시간·타임아웃 기록은 해당 버전의 이력으로 유지합니다. 실제 LLM 서버 내부 연산 취소도 계속 미검증입니다.

## English

The updated Local-only mode uses safe mode to expose all six file/shell tools while retaining existing deny/ask rules and explicitly restricting native integrations. Real Claude Code 2.1.304 in Windows ConPTY and the configured Local server passed all four Write/Glob/Grep path cases, negative searches, UTF-8 byte checks, permission denials, and Write/Glob/Grep continuation after one manual compaction. There were 18 successful tool results and two intentional permission denials; Local routes succeeded 15 times with no upstream failure or Plan route. One unsupported fallback-credit beta request was rejected before a compatible compaction request succeeded. All 16 observed CLI requests used the synthetic proxy Bearer token; hook/helper/MCP canaries did not execute. `/exit` returned zero, retained handles confirmed the owned tree exited, and the listener closed. All 266 Node tests, 41 Python Local tests, self-tests, and Node syntax checks passed. Temporary test data was removed and private configuration remains unpublished. Other versions, managed-policy exceptions, new hour-long/repeated-auto-compaction runs, and new 120-second timeout runs were not checked. Actual backend computation cancellation remains unverified.

This document contains reusable validation procedures and a limited synthetic-input run summary. Keep machine details, credentials, account usage, billing records, session identifiers, and raw diagnostics private. Separate synthetic regression checks from explicitly authorized live validation; metadata checks do not prove inference or full native-feature compatibility.

The following bare-mode result is historical (`0bce716`), before the compatibility fix above.

The 2026-10-23 Write/Glob/Grep path check could not proceed: real Claude Code 2.1.304 in the existing Local bare mode offered only Bash/Edit/Read. A name-only fetch observer confirmed this on three actual user requests to the configured Local server; a background request also succeeded. No requested tool ran and no target file was created, so the four ASCII/space/Korean/combined path cases remain unverified. The [official bare-mode reference](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode) documents the same restricted tool set. The README's earlier six-tool availability claim was corrected. All 48 selected tool/schema/Local-HTTP regression tests passed, but do not prove CLI tool availability. Upstream failures, circuit openings, Local rejections, and Plan routes were zero. `/exit` returned 0 and retained handles confirmed the full owned tree exited and the listener closed. The observer URI setup failure is excluded. Production code, default permissions, native restrictions, the 120-second deadline, and zero Plan fallback were preserved. Temporary settings/history/workspaces were removed; private configuration values remain unpublished. Server computation cancellation and other CLI versions were outside this check.

The isolated Ctrl+C recheck on 2026-10-22 used real Claude Code 2.1.304 in Windows ConPTY with the existing Job-protected Local launch function and production proxy. Exactly one matching user/tool request stalled after a partial JSON body; background requests succeeded immediately. Ctrl+C closed that connection. The next user request reached the test relay 4.598 seconds later, without cooldown, and actual configured Local inference completed in 0.779 seconds with the configured model and exact reply confirmed. The CLI restored the cancelled input into its editor, and the follow-up instruction was appended before submission. The same CLI/proxy/guardian and a single session history were maintained. Incidental errors, upstream failures, circuit openings, Local rejections, and Plan routes were all zero. `/exit` returned 0; retained handles confirmed all four owned processes exited and the proxy listener closed. Both public HTTP cancellation tests passed. Production code, the 120-second deadline, and the 30-second failure cooldown were unchanged. Misclassified-background and sandbox-network setup runs are excluded from the passing result. Temporary settings/history/workspaces were removed, and private configuration values remain unpublished. Actual server computation cancellation was excluded; this check does not repeat compaction, long-run, or other-version validation.

The Windows standalone Local CLI entry point now enrolls its launcher in a kill-on-close Job before spawning the proxy or CLI. A hidden guardian outside that Job owns its non-inheritable handle and waits on an explicitly inherited real launcher handle. Closing the Job removes remaining CLI, proxy, Git Bash, and descendants without PID searches or global kills. The guardian receives only OS environment values. Startup preparation failure blocks unmanaged execution with `local_process_cleanup_failed`; guardian failure ends that Local tree. Imported library calls, ordinary launches, diagnostics, and non-Windows behavior remain unchanged.

All 257 Node and 41 Python tests passed, including five Windows lifecycle tests with actual Git Bash descendants, idle/active HTTP termination, exit codes 0/7, guardian failure, and an unrelated process surviving. A real Claude Code 2.1.304 TTY Bash task also lost its entire owned tree after killing only the launcher; all retained process handles signaled exit, the listener closed, and an unrelated process survived. The corrected file-based script run is counted; an earlier inline-quoting setup failure is excluded.

A separate TTY session retained the same CLI/proxy/guardian after Ctrl+C and returned the exact synthetic recovery marker. One HTTP cancellation and one separate fixture's 30-second receive failure were observed, with one upstream failure/circuit opening and subsequent same-session recovery; this was not an error-free cancellation run or a production 120-second timeout. Plan calls remained zero. `/exit` returned 0, and external retained handles confirmed launcher, CLI, proxy, and guardian exit plus listener closure. Guardian startup took 0.867 seconds in that run and adds one process per Local session. Only synthetic loopback APIs and isolated settings were used; temporary settings/workspaces were cleaned. Actual terminal-window closure, OS/power failure, and external service/scheduler jobs were not verified. Existing sessions require a fresh Local launch to acquire protection.

Local-only startup failures now add a fixed code and action message to the existing `openrouter_start_failed` event, with `kind: LocalStartupError` and exit code 1. Missing CLI/Node, unsupported or unreadable CLI version, invalid configuration/output budget, proxy startup, and CLI spawn failures are distinguished without echoing exception text or configuration values. Ordinary launches and diagnostics retain their error format.

At commit `93b7028`, a synthetic baseline reproduced an orphaned proxy listener after killing only the Python launcher. The Local-only launcher gained a stdin pipe to its proxy; EOF/error/closure ends the proxy immediately. Ordinary launches, diagnostics, and imported `createProxy` instances do not acquire this behavior. Three public Windows lifecycle tests used actual Python/Node processes, synthetic CLI/upstream fixtures, and retained process handles. Killing either CLI or launcher while idle or receiving an incomplete HTTP body closed the proxy listener and active connection, with one Local call and no Plan fallback. Ordinary mode ignored the pipe option, as intended.

Those earlier interactive Windows ConPTY checks used real Claude Code 2.1.304 and isolated synthetic settings without inference requests or Plan login. Killing the CLI ended the launcher and proxy and closed the port. Killing only the launcher ended the proxy and closed the port, but left the CLI alive; the test then cleaned it explicitly. All 257 Node tests, 30 Python configuration/startup tests, three Windows lifecycle tests, Python self-tests, and syntax checks passed. Whole CLI/Bash termination was outside that pipe guard and is covered by the newer Job verification above. Actual terminal-window closure, OS shutdown, and power loss were not tested. Temporary CLI settings and workspaces were removed; private environment information remains unpublished.

Local-only error guidance distinguishes timeouts, HTTP 401/403 authentication/access failures, empty responses, other upstream failures, cooldowns with the previous cause and rounded remaining seconds, and an active recovery probe. HTTP 400, the `api_error` envelope, one upstream attempt, the 30-second cooldown, the 120-second production deadline, existing launch commands, and zero Plan fallback remain unchanged. All 257 Node tests, 25 Python configuration tests, Python self-tests, and Node syntax checks passed. Regression cases include empty/blank HTTP bodies, HTTP 204, empty content, blank text, invalid JSON, connection/body timeouts, wait rounding, zero cooldown, concurrent probes, cause updates, and redaction.

Interactive Claude Code 2.1.304 in Windows ConPTY displayed authentication, empty-response, timeout, 30/18-second cooldown, and recovery-in-progress guidance. CLI follow-up requests sometimes made cooldown guidance the final visible error; it retained the previous cause. After the injected clock passed the cooldown, the same CLI/proxy returned the exact synthetic recovery reply. Plan calls were zero; `/exit` returned 0, both processes stopped, and the listener closed. This UI test used synthetic upstream responses and a test-only 25ms deadline, not new real-server failures or another 120-second wait. Personal settings were unchanged. Actual LLM computation cancellation remains unverified.

At commit `5401a96`, Claude Code 2.1.304 kept one CLI process and one Local-only proxy running for 3,621.83 seconds using stream-json input, a 100k auto-compaction window, and an 8,192 output budget. The run completed 11 large automatic compactions (out of 22 automatic boundaries), 40 Read/Edit/Bash rounds, and 52 retention checks. Of 157 upstream requests, none timed out at 120 seconds; the slowest completed in 114.416 seconds. There were no Plan routes. One HTTP 200 response had empty content; a short request after a 35-second cooldown recovered in the same process, and subsequent work continued. This was a run with a recovered failure, not an error-free pass. Timeout recovery was not exercised during that run; a separate injected-stall test is described below. Visual interactive-terminal behavior and longer runs remain unverified. A separate setup run had an unresolved recall-check failure and is excluded from these counts.

At commit `2ee1cfd`, a separate 445.65-second run used Claude Code 2.1.304 with the same stream-json, 100k compaction window, and 8,192 output budget. A test-only connection function stalled one request until the unchanged production deadline aborted it at 120.006 seconds; that request was not sent to the actual server. After a 35-second cooldown, a new request recovered against the real Local server in the same CLI and proxy processes. Two large automatic compactions and three Read/Edit/Bash rounds then succeeded. Across the run, all seven retention checks passed; there were no additional upstream failures or Plan routes. Seven related timeout/circuit regression tests and evidence consistency checks also passed. The failed request itself was not automatically completed. Production launch code and personal settings were unchanged. This test stalled before response headers; body-receive timeouts were checked separately below. Naturally occurring server delays and actual LLM computation cancellation remain unverified. Raw logs and private settings remain unpublished.

At commit `a23cc65`, a separate 422.57-second run used Claude Code 2.1.304 with the same stream-json, 100k compaction window, 8,192 output budget, and unchanged 120-second deadline. One request used a real HTTP connection to a temporary loopback fixture that sent headers and 73 bytes of partial JSON, then stalled. No configured credentials or conversation body were sent to the fixture. The production body reader consumed those bytes before the deadline fired at 120.000 seconds. Reader cancellation was called twice for that one request. The fixture observed the unfinished response connection closing at 120.004 seconds and stopped its synthetic work, with no subsequent counter growth. No partial content reached the CLI, and there were no Plan routes. After a 35-second cooldown, the same CLI and proxy processes recovered against the real Local server; two large automatic compactions and three Read/Edit/Bash rounds succeeded. All seven retention checks passed, with no additional upstream failure. Five related HTTP timeout/cancellation regression tests and evidence consistency checks passed; temporary workspaces were cleaned up. Production code and personal settings were unchanged.

A separate real-API probe cancelled on `message_start`, before `message_stop` or EOF, and confirmed a subsequent inference's model and reply. It did not establish text-token arrival or computation cancellation. An earlier probe reached stream completion before cancellation and was excluded as proof of cancellation during reception. **Actual LLM server computation cancellation remains unverified.** Fixture socket closure and synthetic job cleanup do not establish backend inference termination. The provided API documentation, endpoint, and key do not expose the server-side job evidence needed to confirm it. Raw logs, test tools, internal addresses, keys, and private settings remain unpublished.

Public regression coverage in `tests/localLlmIntegration.test.mjs` now checks the Local-only HTTP body timeout, partial-response suppression, fixture disconnect cleanup, cooldown rejection, one recovery probe with concurrent rejection, and subsequent tool-history/SSE round trips with zero Plan calls. Client cancellation and immediate recovery are tested in both ordinary and Local-only modes. The three targeted tests and all 243 Node tests, 25 Python configuration tests, Python self-tests, and Node syntax checks passed. The body-timeout case uses a 500ms deadline and an injected clock; production's 120-second default is unchanged and covered separately. Only synthetic credentials and owned loopback servers are used by these public tests.

Interactive verification used Claude Code 2.1.304 in Windows ConPTY with real TTY stdin/stdout/stderr, isolated temporary settings, synthetic files, and the existing Python Local launch path with default permissions. No print mode, `dontAsk`, permission bypass, or automatic approval was enabled. A single approved Edit ran only after approval; a subsequent denied Edit left the file unchanged. Ctrl+C during inference cancelled the request while preserving the CLI and proxy, and a new request in the same conversation succeeded. Normal `/exit` and a separate session's double Ctrl+C at idle both returned zero, terminated the CLI and proxy, and closed the proxy listener. Both sessions had zero Plan routes. Only the temporary proxy key was accepted during onboarding. Bracketed paste and a CSI-u Enter event were used for terminal input. Raw temporary history, settings, and workspaces were removed. Visual rendering in Windows Terminal, force-closing the terminal window, and OS shutdown were not tested; actual LLM computation cancellation remains unverified.

# 연결·장애 복구 검증 절차

[한국어 안내](../README.md) · [English guide](../README.en.md)

개인 테스트 환경, 계정 사용량·청구 내역, 세션 식별자, 실제 대화·설정 파일·진단 로그는 공개하지 않습니다. 이 문서는 재현 가능한 점검 절차와 합성 입력을 사용한 제한된 검증 결과를 담습니다.

## 외부 호출 없는 점검

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p test_local_config.py -v
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

## English

This document contains reusable validation procedures and a limited synthetic-input run summary. Keep machine details, credentials, account usage, billing records, session identifiers, and raw diagnostics private. Separate synthetic regression checks from explicitly authorized live validation; metadata checks do not prove inference or full native-feature compatibility.

At commit `5401a96`, Claude Code 2.1.304 kept one CLI process and one Local-only proxy running for 3,621.83 seconds using stream-json input, a 100k auto-compaction window, and an 8,192 output budget. The run completed 11 large automatic compactions (out of 22 automatic boundaries), 40 Read/Edit/Bash rounds, and 52 retention checks. Of 157 upstream requests, none timed out at 120 seconds; the slowest completed in 114.416 seconds. There were no Plan routes. One HTTP 200 response had empty content; a short request after a 35-second cooldown recovered in the same process, and subsequent work continued. This was a run with a recovered failure, not an error-free pass. Timeout recovery was not exercised during that run; a separate injected-stall test is described below. Visual interactive-terminal behavior and longer runs remain unverified. A separate setup run had an unresolved recall-check failure and is excluded from these counts.

At commit `2ee1cfd`, a separate 445.65-second run used Claude Code 2.1.304 with the same stream-json, 100k compaction window, and 8,192 output budget. A test-only connection function stalled one request until the unchanged production deadline aborted it at 120.006 seconds; that request was not sent to the actual server. After a 35-second cooldown, a new request recovered against the real Local server in the same CLI and proxy processes. Two large automatic compactions and three Read/Edit/Bash rounds then succeeded. Across the run, all seven retention checks passed; there were no additional upstream failures or Plan routes. Seven related timeout/circuit regression tests and evidence consistency checks also passed. The failed request itself was not automatically completed. Production launch code and personal settings were unchanged. This test stalled before response headers; body-receive timeouts were checked separately below. Naturally occurring server delays and actual LLM computation cancellation remain unverified. Raw logs and private settings remain unpublished.

At commit `a23cc65`, a separate 422.57-second run used Claude Code 2.1.304 with the same stream-json, 100k compaction window, 8,192 output budget, and unchanged 120-second deadline. One request used a real HTTP connection to a temporary loopback fixture that sent headers and 73 bytes of partial JSON, then stalled. No configured credentials or conversation body were sent to the fixture. The production body reader consumed those bytes before the deadline fired at 120.000 seconds. Reader cancellation was called twice for that one request. The fixture observed the unfinished response connection closing at 120.004 seconds and stopped its synthetic work, with no subsequent counter growth. No partial content reached the CLI, and there were no Plan routes. After a 35-second cooldown, the same CLI and proxy processes recovered against the real Local server; two large automatic compactions and three Read/Edit/Bash rounds succeeded. All seven retention checks passed, with no additional upstream failure. Five related HTTP timeout/cancellation regression tests and evidence consistency checks passed; temporary workspaces were cleaned up. Production code and personal settings were unchanged.

A separate real-API probe cancelled on `message_start`, before `message_stop` or EOF, and confirmed a subsequent inference's model and reply. It did not establish text-token arrival or computation cancellation. An earlier probe reached stream completion before cancellation and was excluded as proof of cancellation during reception. **Actual LLM server computation cancellation remains unverified.** Fixture socket closure and synthetic job cleanup do not establish backend inference termination. The provided API documentation, endpoint, and key do not expose the server-side job evidence needed to confirm it. Raw logs, test tools, internal addresses, keys, and private settings remain unpublished.

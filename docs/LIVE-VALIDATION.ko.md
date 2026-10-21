# 연결·장애 복구 검증 절차

[한국어 안내](../README.md) · [English guide](../README.en.md)

개인 테스트 환경, 계정 사용량·청구 내역, 세션 식별자, 실제 대화·설정 파일·진단 로그는 공개하지 않습니다. 이 문서는 특정 실환경의 통과 보고서가 아니라 재현 가능한 점검 절차입니다.

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

Local 모드는 요청당 120초를 허용합니다. 시간 초과 시 한 번 실패하고 Plan으로 전환하지 않습니다. CLI 자체의 재시도는 추가될 수 있습니다. 수동·100k 자동 압축 통과가 전체 컨텍스트 한도, 반복 압축, 장시간 도구 작업을 보장하지는 않습니다.

## English

This is a reusable validation procedure, not a live-environment pass report. Keep machine details, credentials, account usage, billing records, session identifiers, and raw diagnostics private. Separate synthetic regression checks from explicitly authorized live validation; metadata checks do not prove inference or full native-feature compatibility.

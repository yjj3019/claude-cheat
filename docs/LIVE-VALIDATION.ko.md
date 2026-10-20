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

## English

This is a reusable validation procedure, not a live-environment pass report. Keep machine details, credentials, account usage, billing records, session identifiers, and raw diagnostics private. Separate synthetic regression checks from explicitly authorized live validation; metadata checks do not prove inference or full native-feature compatibility.

# 공개 구현 이력 안내

[한국어 안내](../README.md) · [English guide](../README.en.md)

코드 변경 이력은 Git 커밋에서 확인하세요. 개인 PC 구성, 계정 상태, 실호출 비용과 세션 기록은 공개 문서와 Git 이력에서 제외합니다. 현재 사용법과 기능 제약은 README, 재현 가능한 점검 절차는 [연결·장애 복구 점검](LIVE-VALIDATION.ko.md)을 기준으로 확인하세요.

과거 검사 결과나 정책을 현재 기능의 보증으로 해석하지 마세요. 합성 테스트와 실제 서비스 검증은 구분해야 합니다.


## 버전별 주요 변경

### 1.0.7 (2026-11-08, 커밋 2d06db9)

세션 종료 후 대시보드 정리와 토큰 효율 측정이 주제입니다.

- **종료 세션 잔존 해소** — 대시보드 자동 takeover가 원래 CLI 종료를 감지해 정리하고, launcher pid이 죽은 세션의 handoff를 새로고침 루프가 제거합니다. 30일 고아 정리까지 기다릴 필요가 없습니다.
- **프롬프트 캐시 측정** — 사용 원장(스키마 v3)이 `cache_read_tokens`·`cache_creation_tokens`를 수집하고, 오늘 사용 카드에 캐시 적중률을 표시합니다. `ROUTE_STABILIZE_PROMPT_CACHE`의 효과를 실측으로 판단할 수 있습니다.
- **실행 중 백엔드 전환** — `routing: dual_provider` 별칭이 설정 화면에서 토글로 노출됩니다. 별칭은 새 세션에 부여되고, 세션의 `/model`에서 Local↔OpenRouter를 즉시 전환합니다.
- **전체 요청 시간 예산** — `ROUTE_REQUEST_BUDGET_MS`(기본 0, 비활성)가 Local 대기+시도+재시도 대기를 합산해 제한합니다. 켜기 전까지 기존 동작은 그대로입니다.
- **성능** — SSE 프레임 파싱을 한 번의 줄 순회로(4,000프레임 기준 1.44ms→0.77ms), 대시보드 설정 읽기에 size+mtime 캐시(스냅숏 스파이크 제거).
- **기타** — 콘솔 없는 부모의 자식 실행이 cmd 창을 띄우지 않도록 CREATE_NO_WINDOW 적용, 버전 배지가 커밋 변경을 추적, 업스트림 진행 중 클라이언트 취소를 진단 이벤트로 기록.

### 1.0.7 추가 (2026-11-09, 커밋 `dfd3e29`–`02931a7`)

CLI 백엔드 진단과 Codex 0.167.0 검증이 추가되었습니다.

- **Plan 우회 완화** — CLI 백엔드 세션에서 Claude Code에 실험 beta 비활성화를 요청합니다. 명시적 Advisor 설정은 건드리지 않습니다.
- **CLI 오류 단계 구분** — 브리지 오류에 고정 진단 코드가 표시됩니다. `cli_process_failed`(프로세스 비정상 종료)·`cli_turn_failed`(CLI 실패 이벤트)·`cli_empty_output`(출력 없음)·`not_logged_in`·`unsupported_request` 등이며, 요청 내용과 CLI 출력 원문은 기록하지 않습니다. 대시보드 최근 이력에도 허용된 코드만 표시합니다.
- **Codex 0.167.0 검증** — 격리 플래그 22개 수용, 도구 제안→결과 왕복, 구조화 출력, 장문 문맥(약 770KB) 실측을 통과해 검증 목록에 추가했습니다. 검증 프로토콜은 도구 없는 턴, 도구 왕복, 구조화 출력, 장문 문맥의 4단계입니다.
- **참고** — `--disable` 플래그를 `exec` 하위 명령이 아닌 최상위에 22개 반복하면 Codex 0.167.0이 오류로 종료합니다. 브리지는 `exec` 뒤에 넣으므로 영향이 없습니다. 향후 Codex 업데이트 시 같은 확인이 필요합니다.

### 1.0.7 추가 (2026-11-11) — `--bg` 분리 세션의 프록시 수명과 버전 캐시 회귀

문서에 남아 있던 분리 실행(`--bg`) 프록시 수명 경계를 해결했습니다.

- **`--bg` 프록시 keeper** — `--bg` 네이티브 인자를 받으면 실행기가 프록시의 파이프 끝을 이어받는 전용 keeper(`--keep-instance`)를 분리 실행하고, 전면 프로세스가 끝나도 세션이 계속 라우팅합니다. keeper는 인스턴스 핸드오프가 사라지거나(takeover·정지·정리) 프록시가 자기 토큰으로 응답을 멈추면 종료되고, 마지막 파이프 끝이 닫히며 프록시도 함께 마칩니다. 인스턴스 디렉터리가 없으면 기존 실행기 수명 방식을 유지합니다. keeper는 도착 즉시 인스턴스 핸드오프의 소유주(`launcher_pid`)를 자기 pid로 이어받아, 실행기가 이미 떠난 뒤에도 대시보드의 죽은 세션 스윕이 분리 세션을 해제하지 않도록 합니다(회귀 테스트 고정).
- **버전 캐시 회귀 수정** — 2507200 커밋이 "checkout 변경 시 캐시 무효" 규칙을 넣으며 `test_local_info_is_cached_until_explicit_refresh`의 계약(명시적 refresh 전에는 git을 다시 실행하지 않음)을 깨뜨려 전체 Python 스위트가 1건 실패하던 회귀를 되돌렸습니다(재사용 규칙을 "TTL + 명시적 refresh만"으로 복원).
- **Windows 환경 클리어 회귀 수정** — 테스트가 `os.environ`을 완전히 비울 때 `SYSTEMROOT`가 사라지며 CPython의 SSL 계층이 루프백 HTTP까지 `SSLError`로 실패해 `LocalMetadataTests` 16건이 환경·머신에 따라 깨지던 원인을 제거했습니다(환경 클리어 시 `SYSTEMROOT` 보존).
- **검증** — `tests/test_launcher_arguments.py`에 keeper 단위 테스트 3건(핸드오프 소멸·응답 정지·`--bg` 표식) 추가. `tests.test_route_version` 31건 통과, `tests.test_handover_contract`·`tests.test_windows_runtime` 전체 통과.

남는 경계 — 사용자 지정 API URL에서 비활성화되는 Remote Control 제한과 실제 인증 환경의 Agent Teams·SSH/MCP·Plugins 통합 검증은 그대로입니다(Claude Code 자체 제약·실사용 환경 필요).

### 이전 버전

1.0.5·1.0.6의 항목은 Git 커밋(`52a5da1`, `14037f4`)과 [PROXY-REFERENCE](PROXY-REFERENCE.md)의 환경 변수 표에서 확인하세요. 과거 문서를 현재 기능의 보증으로 읽지 마세요.

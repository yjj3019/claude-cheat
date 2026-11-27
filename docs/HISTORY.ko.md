# 공개 구현 이력 안내

[한국어 안내](../README.md) · [English guide](../README.en.md)

코드 변경 이력은 Git 커밋에서 확인하세요. 개인 PC 구성, 계정 상태, 실호출 비용과 세션 기록은 공개 문서와 Git 이력에서 제외합니다. 현재 사용법과 기능 제약은 README, 재현 가능한 점검 절차는 [연결·장애 복구 점검](LIVE-VALIDATION.ko.md)을 기준으로 확인하세요.

과거 검사 결과나 정책을 현재 기능의 보증으로 해석하지 마세요. 합성 테스트와 실제 서비스 검증은 구분해야 합니다.



## 버전별 주요 변경

### 1.0.7 추가 (2026-11-18) — Local 라우팅과 요청별 사용량

- 일반 Local 단독 세션에서 MCP ToolSearch를 사용할 수 있도록 런처의 beta 설정을 조정했다. 이전 대화의 도구 정의를 바꾸지 않고 해석할 수 있는 요청만 Local로 보낸다.
- 일반 Local 요청의 첫 응답 대기와 응답 도중의 정체를 구분한다. 선택적 전체 요청 제한시간(`ROUTE_REQUEST_BUDGET_MS`)에는 대기열, 모델 호출, 재시도 대기가 모두 포함된다.
- 사용량을 모델 호출 시도별로 집계한다. Local이 스트리밍 응답에 다른 모델 ID를 반환하면, 클라이언트에 응답을 보내기 전에 JSON 응답으로 모델을 다시 확인한다.

구현 커밋은 `b9a3e55`, 병합 커밋은 `792f7ba`다. MCP 도구 왕복과 Auto 권한 검토에서 확인한 범위는 [실제 실행 기록](LIVE-VALIDATION.ko.md)에 정리했다.

### 1.0.7 추가 (2026-11-16) — Gateway·Monitoring 개선

- 승인된 내부망 HTTP/원격 HTTPS 주소의 프록시 연결 준비 오류 수정.
- 프록시 시작 실패를 단계·고정 코드·종료 코드로 기록하고 모니터링에 표시.
- 실행 모델과 다음 실행 설정 분리, 실행 모델 기준 단가 조회, Local 대기열 필드 정합성 수정.
- 같은 호스트·서버 origin의 동시성·대기열 공유, 연결 기반 허가 회수, 조정 프로세스 장애 시 요청 취소.
- 세션 메트릭 병렬 수집과 전체 대기 예산, 마지막 정상값·수집 지연 표시.
- 실패·지연 요청의 제한된 추적 기록. 프롬프트·응답 내용 제외.
- 인증된 탭별 명령·처리 확인으로 설정 재조회와 입력값 보존, 재시작 후 재연결.
- 실제 런처/Node/HTTP와 Edge 브라우저 회귀 검증 추가. 외부 모델 기능 검증과 구분.
- 이미 취소된 요청의 공유 제한 연결 생성 방지, HTTP/2 유휴 연결 정리와 진행 중인 요청 분리, 탭 상태 변경 시 브라우저 갱신 타이머 중복 방지.
- 공유 제어 요청·응답의 형식을 검증해 잘못된 메시지가 서버나 클라이언트를 종료하지 않도록 수정.
- 여러 탭의 세션 수집이 같은 조회를 공유하고 잠금 밖에서 대기하도록 수정. 잠금 대기도 전체 시간 제한에 포함.
- 이중 제공자 세션의 Local 대기열을 시작 제공자와 독립적으로 표시.
- 기존 탭의 명령 처리 확인, 미응답 시 취소 후 새 탭 열기, 만료된 이동 명령 차단을 추가.

### 1.0.7 추가 (2026-11-15) — 전면 감사 개선: admission env 검증 통일·대시보드/구성/설치자·CLI 진단

전체 소스 감사([SOURCE-AUDIT.ko.md](SOURCE-AUDIT.ko.md))와 비판적 검토([CRITICAL-REVIEW.ko.md](CRITICAL-REVIEW.ko.md))에서 도출된 문제점의 수정 라운드입니다.

- **로컬 admission env 검증 통일(MAJOR M1)** — `ROUTE_LOCAL_CONCURRENCY`(1–64)·`ROUTE_LOCAL_QUEUE_LIMIT`(0–256)·`ROUTE_LOCAL_QUEUE_WAIT_MS`(0–600000)·`ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS`(30s–15min)를 런처가 상한까지 검증해 올바른 값만 프록시에 전달하고(유니코드 숫자 `²`·`６５` 포함 set-but-invalid → `local_admission_env_ignored`로 무시 → 프록시 기본값), 프록시를 직접 실행할 때 잘못된 값이 설정돼 있으면 `Invalid Local admission limits`로 시작이 실패합니다(기존 "잘못된 값은 기본값" 문구를 README에서 정정). `ROUTE_HANDOVER_TIMEOUT_MS`는 0·음수·비수치일 때 120초 유지, 600초 초과분만 600초로 제한합니다. 다섯 env는 프록시 decode에서 소비 후 제거 목록에 추가되어 하위 프로세스로 새지 않습니다.
- **CLI 백엔드 사전 점검 실패는 지연이 아니라 중단** — CLI 백엔드 세션이 사전 점검에 실패하면 `provider_connection_unavailable`(continue) 대신 `local_cli_backend_unavailable`로 시작이 멈춥니다. 첫 턴에서 죽는 세션 대신 시작 진단으로 표면화됩니다.
- **대시보드** — 사용량 화면의 부분 집계/세션 비용이 사용할 수 없는 단가를 $0으로 위장하지 않고 `None`으로 표기하며, 일부 단가만 있는 레코드가 크래시하지 않습니다. 만료 세션 스윕·설정 저장이 심링크/정션을 따라가지 않습니다(`is_dir` 대신 `is_symlink` 거부, `os.scandir(follow_symlinks=False)`). 설정 저장이 고정 `.tmp` 이름을 버리고 고유 임시 파일+`os.replace`를 사용하며, 저장된 부분이 `read_provider`가 거부할 형식이면 저장이 실패합니다(첫 오류 보고). `/api/models-search` 요금 제한이 실측 카운터 경로로 전환되어 7번째 요청이 429를 반환합니다. 대시보드 PS1의 `-Port`(1–65535)·`-Refresh`(1–86400) 검증, auto-reload 끔 값 목록이 버전 캐시와 정렬됩니다.
- **구성** — `update_text`가 BOM 없는 파일뿐 아니라 BOM 있는 섹션 헤더와도 대칭으로 일치합니다. 설치자의 배치 파일 인코딩 감지가 PowerShell 5.1뿐 아니라 7에서도 동작하고(CodePages 인코딩 공급자 등록), 드라이브 루트를 `C:`로 줄이지 않으며, 포워더 소유권 판정이 바이트 비교가 아니라 생성 마커 줄로 바뀌어 checkout 이동 후에도 갱신·제거가 가능하고 `-Remove`는 설치자가 추가한 정확한 PATH 항목만 제거합니다.
- **CLI 진단** — 레거시 `%~dp0%` shim(SHIM 정규식) 인식, 버전 해석을 앞뒤 고정(fullmatch)으로 엄격화(꼬리 토큰의 `0.167.0-dev` 등은 `version_failed`), 미검증 버전 연결 검사가 `unsupported_version`로 보고됩니다. 즉시 종료한 CLI 자식이 작업 연결 전에 `process_tracking_unavailable`로 가려지면 실제 사유(`not_logged_in` 등)를 진단하고, 종료 코드가 주어지면 `tree`/`root` 라벨이 taskkill 결과를 따릅니다. 품질 프로브의 `TimeoutError`가 `probe_timeout`으로 구분됩니다.
- **검증** — `tests/test_local_admission_env.py` 신규(경계·유니코드·과장 숫자 8건), `localAdmission.test.mjs` 확장(유니코드·시작 실패 목록, 시도 시간 초과 경계), 대시보드·구성·설치자·CLI 수정에 각 테스트 추가. Python·Node 전체 스위트 재실행 통과.

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

### 1.0.7 추가 (2026-11-12) — Local LLM 저장 전 모델 목록 검색

설정 페이지의 Local LLM 모델 `목록` 버튼이 **저장된 설정 없이도** 동작하도록 했습니다.

- **저장 전 검색** — 입력한 API Endpoint(`/v1/messages`)와 API 키를 **둘 다** 입력하면(키가 필요 없는 서버면 아무 값) 저장 전 값으로 서버의 `/v1/models` 목록을 불러와 선택할 수 있습니다. 기존 `/api/models`(저장 설정 기준)는 그대로이며, 검색 전용 `POST /api/models-search`가 추가됐습니다.
- **입력 순서 재배치** — Local LLM 필드 순서를 `Messages URL → 서버 키 → 모델 ID(목록)`으로 바꿔 "엔드포인트 → 키 → 모델 검색" 흐름에 맞게 조정했습니다.
- **URL 자동 완성** — Messages URL에 `http://127.0.0.1:11434`(호스트:포트)나 `…/v1`까지만 입력하면 저장·검색 경계에서 `…/v1/messages`로 자동 보정됩니다(`normalize_local_url`). 다른 경로(`/v1/chat/completions` 등)·잘못된 입력은 그대로 두어 기존 엄격한 검증이 판단하며, 저장 형식은 런처·프록시가 이미 기대하는 `…/v1/messages`로 유지됩니다.
- **LAN http 예외** — 검색 프로브에 입력한 **내부 HTTP 예외** 주소까지 전달해, 루프백이 아닌 LAN http 서버(예: `http://172.21.135.113:4000/v1/messages`)도 저장 전 목록 검색이 가능합니다(저장과 동일한 "예외 주소 = URL" 규칙). 예외가 비어 있으면 검색 프로브가 같은 주소를 자동으로 적용하고(검색 전용 편의, 저장 시에는 UI가 필드를 자동 채워 명시 기록),Messages URL이 규칙 위반이면 피커에 구체적 사유를 안내합니다.
- **크레덴셜 경계 유지** — `/api/test`의 "주소·키는 저장 값만" 원칙을 깨지 않도록, 검색 프로브는 전송된 주소와 키만 사용하고 저장된 키로 절대 대체하지 않습니다(주소+저장 키 조합은 키 유출 경로이기 때문). 검색 요청은 토큰 인증 + 분당 6회 제한, 모델 ID 자리표시자로 저장과 동일한 URL·키 규칙을 그대로 검증합니다. `/v1/models`는 전송된 주소의 같은 출처에서만 유도됩니다.
- **검증** — `ModelsSearchEndpointTests`(토큰 필수·필드 누락/비허용 URL·전송 값만 사용·출처 규칙·LAN http 예외·base 엔드포인트 자동 완성) + `NormalizeLocalUrlTests` 2건 + 저장 시 정규화 1건 추가.

### 1.0.7 추가 (2026-11-12) — Agent Teams·추론(thinking) 활성화: 10회 시뮬레이션과 로컬 동시성·검증 가시성

Agent Teams와 thinking의 실질 제약을 10라운드 시뮬레이션으로 도출하고, 연구 리포트([AGENT-TEAMS-THINKING-RESEARCH.ko.md](AGENT-TEAMS-THINKING-RESEARCH.ko.md))의 권장 우선순위 1~4를 구현했습니다.

- **10회 시뮬레이션** — `tests/simulate_agent_teams_thinking.mjs`가 Agent Teams(버스트 4–12턴 × 10라운드, 팀 툴 세트·`cache_control`)와 thinking(6시나리오 × 10라운드)을 합성 업스트림으로 돌립니다. 결과: 기본 한계(동시 2+대기 8)에서 burst>10일 때만 `local_busy`(4/80턴), thinking은 일반 로컬이 요청 강제 비활성·서버 thinking 블록 반환 시 503, compat 왕복(서명 없는 블록 반환·히스토리 제거로 pinning 불성립)·Plan 의존(403)까지 확인. 예상 밖 실패 0건.
- **로컬 동시성 env(A2)** — `ROUTE_LOCAL_CONCURRENCY`(1–64)·`ROUTE_LOCAL_QUEUE_LIMIT`(0–256)·`ROUTE_LOCAL_QUEUE_WAIT_MS`(0–600000, 기존 60s 상한 완화)·`ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS`(30s–15min, compat/local-only 세션)를 프록시가 읽고 런처가 올바른 값만 전달(잘못된 값은 무시+경고). 팀 규모나 느린 GPU에 맞게 버스트 `local_busy`를 완화합니다.
- **thinking 검증과 가시성(T2)** — 품질 검증(`modelQualityProbe`)에 `--thinking` 옵트인 3번째 합성 호출을 추가해 서버가 thinking 블록을 `present`/`absent`/`invalid`/`error`로 판정합니다. 대시보드 **도구 왕복 검사** 결과와 evidence에 `thinking`이 쌓이고, 진단 `capabilities.thinking`이 미검증 `unknown`에서 판정값으로 채워집니다. Local 설정 카드에는 Agent Teams·thinking → `-LocalCompat` 권장 힌트가 표시됩니다.
- **Agent Teams 행(A3)** — 진단 `capabilities.agent_teams`이 지원 제공자에서 `supported`로 보고됩니다(팀 툴은 표준 API shape 통과 — 시뮬레이션으로 검증). live 팀 검증 절차(인터랙티브 세션, 메일박스·태스크 파일 확인)는 연구 리포트 §4에 정리했습니다.
- **모드 선택 가이드(A1+T1)** — README "모드 선택 가이드: Agent Teams와 추론(thinking)"이 목적별 권장 세션(팀·thinking → `-LocalCompat`, 구독 thinking 턴 → 일반 세션의 Plan 폴백)과 로컬 동시성 조정 표를 정리합니다.
- **검증** — `localAdmission.test.mjs` +3(env 오버라이드·병렬 승인·타임아웃 클램프), `test_model_quality_probe.py` +6, `test_route_dashboard.py` +4. Node·Python 전체 스위트 통과. live P1 probe(`python modelQualityProbe.py --provider local --run --thinking`)는 저장된 실제 로컬 키가 필요합니다.

### 이전 버전

1.0.5·1.0.6의 항목은 Git 커밋(`52a5da1`, `14037f4`)과 [PROXY-REFERENCE](PROXY-REFERENCE.md)의 환경 변수 표에서 확인하세요. 과거 문서를 현재 기능의 보증으로 읽지 마세요.

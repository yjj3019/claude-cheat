# 전체 소스 비판적 검토 보고서 (2026-11-15)

- 기준: `9d1b2f0`, 작업 트리 clean. 선행 감사(docs/SOURCE-AUDIT.ko.md)는 "의도된 설계"를 존중하며 사실을 검증하는 관점이었고, 이 문서는 **설계·등급·프로세스까지 의심하는 관점**으로 재심사한다. 감사에서 "도달 불가"·"INFO"로 낮춘 항목과 감사가 질문하지 않은 구조적 전제를 재평가했다.
- 배경 사실: 프록시는 **세션당 1개 프로세스**(파이프가 닫히면 `process.exit(0)`, claudeRouteProxy.mjs:2677-2686), 수용 큐·브레이커는 전부 세션 로컬 상태(1287-1290), 런타임 파일은 저장소 루트 고정(routeDashboard.py:61-71), 대시보드는 6,906줄 단일 파일, 세션 1개당 `.proxy-metrics.json` 단일 경로 공유(openrouterAdvisor.py:955), env 삭제 목록 2652-2655, admission env 파싱 1205-1210 · throw 1282-1285, attemptTimeout 1160-1163, README:546 "잘못된 값은 기본값", VERSION=1.0.7.

---

## A. 설계·구조 비판 (핵심)

### A1. 수용 제어가 "서버 단위"가 아니라 "세션 단위" — 문서화된 의미와 전제가 다르다
- 프록시는 런처 세션당 1개이며 수용 큐(concurrency 2 + queue 8), 서킷브레이커, 큐 대기 제한이 모두 **프로세스(세션) 로컬**이다. `ROUTE_LOCAL_CONCURRENCY`는 "로컬 서버 부하 제어"처럼 보이지만 실제로는 **세션 하나**의 동시성이다. Agent Teams 10개 팀(세션 10개)이면 로컬 서버에는 기본값으로 최대 **20 active + 80 queued**가 쌓일 수 있다. 시뮬레이션(10회)은 단일 프록시만 모델링했으므로 이 부하 형태는 **한 번도 검증되지 않았다**. 개별 프록시는 "안전"해도 서버는 무방비 — 감사가 유일하게 질문하지 않은 전제.
- 비판: 이 설계가 의도적이라면 "팀 단위 조절"로 문서화해야 하고, 아니라면 전역 조정(공유 락/포트 기반 세마포어)이 필요하다.

### A2. 동일 규칙이 두 언어 + 여러 파일에 분산 — "한쪽만 고치면 조용히 갈라지는" 구조 (M1의 뿌리)
- env 검증: 런처 파이선 하한(openrouterAdvisor.py:962-970, 상한 **없음**) ↔ 프록시 throw(claudeRouteProxy.mjs:1282-1285) — 이미 갈라짐(MAJOR). false-set 어휘도 4종(`routeVersion.py:54-58` / `routeDashboard.py:6294` / `openrouterAdvisor.py:183-185` / JS `=== 'true'`). 모델/URL 정규화도 양쪽에 별도 구현. 미러링 부채는 오늘 M1을 만들었고 앞으로도 만든다. 문서(README:546 "잘못된 값은 기본값")와 주석("fails startup loudly", 1204)이 코드와 모순된 것도 같은 원인.
- 비판: 이 저장소의 최대 잠재 결함 원천은 로직 자체라기보다 **규칙의 단일 소스 부재**다. 다음 규칙도 곧 갈라질 예정: admission 상한, 버전 허용목록(문서: 0.165-0.167), 429/재시도 해석(parseRetryAfter ↔ `_http_json`의 `max(300,…)`), 테스트 수.

### A3. 공유 상태가 프로세스 단위로 설계되지 않음 — 메트릭 유실은 "INFO"가 아니다
- `.proxy-metrics.json`은 **단일 경로에 last-writer-wins**(고유 tmp명 + rename, claudeRouteProxy.mjs:1503-1522, 대시보드도 같은 파일, routeDashboard.py:65). 동시 세션 2개면 나중 프로세스의 메트릭이 이전 세션 것을 **덮어쓴다**. `.session-handoff.json`도 전역 단일 레코드라 두 번째 세션이 첫 번째 등록을 덮어 쓴다(openrouterAdvisor.py:508-535). 유일하게 `.route.log`(공유 append)만 정당. 레코드에 세션 식별자(instances/<uuid>/)가 이미 있는데 전역 파일만 단일값이다.
- 비판: 감사는 "손상 없음"을 증명했지만 **유실**(동시 세션 메트릭·핸드오프 소유권)은 증명하지 않았다. 유실 계열은 INFO가 아니라 MINOR~MAJOR이다.

### A4. 기능이 patch 번호로 누적 — 1.0.7에 메이저 기능 3개가 묻혔다
- `9d1b2f0`이 "Local model pre-save search / Agent Teams·thinking / admission 튜닝"을 1.0.7 패치로 추가했고 VERSION·메이저 문서의 "현재 버전" 표기도 그대로다. README:604의 테스트 수(726/712)는 이미 실제(811/854)와 이탈 — 문건-현실 정합 점검이 릴리스 절차에 없다는 증거.

### A5. 검증 문화: 시뮬레이션 > 실기 — 잠복 오도 2건이 "다음 버전에서 확정 장애"로 도래한다
- codex 버전 하드 허용목록(cliBackend.py:447-448, fail-closed)과 `connection_test`의 "ready"(버전 미검사, routeCli.py:346-385)는 **차기 codex에서 확정적으로 모순**된다(대시보드 "ready" + 모든 세션 `backend_version_unverified`). P1(실기 thinking probe)과 실 Agent Teams 검증이 키 부재로 미실행인 채 "구현 완료" 표기된 것도 같은 문화다. 반복되는 감사 문서(~640KB)에 비해 실기 항목은 계속 미뤄진다.
- 비판: 854+811개 테스트는 **자체 계약**만 검증한다. 실 계약(실 Claude Code·실 로컬 서버·실 codex)은 live_* 스모크(기본 skip)와 P1뿐. 계약 검증이 스위트 밖에 있는 한 "테스트 1665개 통과"는 안심 근거가 아니다.

### A6. 기본값이 대상 사용자와 상충 — 일반 로컬 30초 침묵 윈도우 고정
- 일반 모드의 30초(first chunk)는 "noPlan" 스트리밍 가정인데, 로컬 우선 사용자의 추론형 모델은 첫 토큰이 30초를 넘는 경우가 흔하다(시뮬레이션도 합성 레이턴시로만 검증). `ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS`가 일반 모드에 적용 안 되는(1160-1163) 지점이 있어 튜닝으로도 못 바꾼다 — 기본값 선택이 제품 목표와 상충.

---

## B. 감사 등급 재검토 (엄격 재평가)

| 감사 판정 | 재평가 | 근거 |
|---|---|---|
| M1 런처 상한 미검사 — MAJOR | **유지 (MAJOR)** — 문서 보장(README:546, HISTORY, RESEARCH:56)이 거짓 + 실패 모드가 흔한 입력 | openrouterAdvisor.py:962-970 vs 1282-1285 직접 확인 |
| `.proxy-metrics.json` last-writer-wins — INFO | **MINOR 상향** — 동시 세션에서 메트릭 유실(데이터 손실 계열) | 세션당 1프로세스 설계(A1)와 결합 시 정기 발생 |
| `connection_test` "ready" 미검사 — INFO | **MAJOR 후보(잠복)** — 차기 codex에서 확정적 오도 | routeCli.py:346-385 vs cliBackend.py:447-448 |
| update_text BOM / write_provider 섹션 오류 — MINOR(도달 불가) | 유지하되 **"잠복"으로 분류**: 도달 불가는 오늘의 호출 그래프 기준, A2 미러링 부채로 언제든 도달 |
| 세션 단위 수용 — 미지적 | **MINOR~MAJOR (신규)** — A1 |
| env 삭제 목록(2652-2655)에 ROUTE_LOCAL_*, ROUTE_HANDOVER_TIMEOUT_MS 누락 | **MINOR (신규)** — 유출은 없음(프록시는 자식 없음)이나 목록 유지보수가 미러링 부채임을 스스로 증명 |
| ATTEMPT_TIMEOUT 'abc'→조용한 기본 vs 1282 throw vs 1160 주석 "fails startup loudly" | **MINOR (신규)** — 같은 변수군에서 실패 방식 3종 비일관 |

---

## C. 확인된 강점 (비판적 관점에서도 유지)
- 큐 해제는 래치+`indexOf<0` 가드로 이중 해제 불가(1289-1321), 큐 대기가 attempt 타임아웃을 소비하지 않는 구조(2167-2171 vs 2267-2269), 파이썬 쓰기 전부 mkstemp+`os.replace`+O_NOFOLLOW, POST 전용·토큰·Host/Origin/요금·BYTES_CAP, thinking 서명 제거·비밀 마스킹, 업데이트 fast-forward 전용+마커 배타, VERSION/예제 파일 정합. 이 선은 견고하다 — 비판의 초점은 "틀린 곳"이 아니라 "전제(단일 프로세스, 단일 언어, 실기)가 깨지는 시점"에 있다.

---

## E. 기능 단위 비판 평가 매트릭스 (2026-11-15 추가)

지금까지의 감사/비판은 파일·컴포넌트 기준이었으므로, **기능 단위로 재배열**해 평가 수준과 잠복 결함을 정리한다. ①기능 ②핵심 파일 ③비판 요지 ④등급 ⑤검증 수준.

| # | 기능 | 핵심 파일 | 비판 요지 | 등급 | 검증 수준 |
|---|---|---|---|---|---|
| 1 | 로컬 LLM 라우팅(일반/compat/localOnly) | claudeRouteProxy.mjs | 큐·래치·모델 게이트는 견고하나 ①수용이 **세션 단위**(다중 세션 부하 미검증) ②일반 모드 30초 침묵 윈도우 고정(첫 토큰 30초+ 모델은 기본 실패) ③attemptTimeout 'abc' 조용한 기본 vs 형제 throw 비일관 | MINOR~MAJOR | 유닛 다수 + 시뮬레 10회(단일 프록시 한정) |
| 2 | OpenRouter 라우팅 | claudeRouteProxy.mjs | 재시도/Retry-After/파스스루/redact 견고. parseRetryAfter(JS) ↔ `_http_json`(Py) 해석 이원화, Python 미러 없음 | MINOR | 유닛 60커버 |
| 3 | CLI 브리지 (codex 0.165~0.167) | cliBackend.py | ①fail-closed(안전) ↔ `connection_test` "ready" 오도 **잠복 MAJOR** ②`_SHIM_RE` 레거시 shim 미매칭 ③effort 인용 [UNVERIFIED] | MINOR~**잠복 MAJOR** | 합성 픽스처만, 실 codex 무검증 |
| 4 | Agent Teams (우선순위 4) | 런처+proxy+시뮬 | ①실 팀 검증 미실행(절차만 문서화) ②팀 10개 = 세션 10개 = 서버에 20+80 누적(A1) ③틴 후 미보고 경로 | MINOR~MAJOR(A1) | 시뮬 10회(단일 세션) |
| 5 | 추론(thinking) S1~S6 · P1 | proxy+probe+dashboard | 서명 제거·스트립·display→Plan→403 검증됨. P1(실기) 미실행, TimeoutError 라벨 2종 | MINOR | 유닛+시뮬, 실기 부재 |
| 6 | Local admission 큐 (A2) | proxy+런처 | **M1 상한 미러링 누락**(문서 보장 거짓), 빈 문자열/유니코드 숫자 2방향, 런처 경유 테스트 부재 | **MAJOR** | 직접 재검증 완료 |
| 7 | 사전저장 모델 검색 | routeDashboard | SSRF 경계/토큰/6-min/저장 키 비결합 엄격 ✓, 429 분기 엔드포인트 테스트 부재 | MINOR | 게이트는 커버, 실 서버 무검증 |
| 8 | 품질 프로브 | modelQualityProbe+dashboard | JSON 계약 정합 ✓, probe_timeout/configuration_or_request_failed 라벨 차이, 24h 캐시 TTL | MINOR | 유닛, P1 실기 부재 |
| 9 | 대시보드 모니터링 | routeDashboard(6906줄) | 모노리스 + 임베디드 HTML ②메트릭 **last-writer-wins**(동시 세션 유실·A3) ③평가 캐시/스냅샷 원자 ✓ | MINOR~MAJOR(A3) | 유닛+DOM 99, DOM은 스텁 |
| 10 | 핸드오버/takeover(--bg/--takeover) | 런처+dashboard+proxy | 드레인/거버넌스/타임아웃 엄격, 핸드오프 **전역 스톰프**(2번째 세션이 덮어씀), job attach 전 즉사 → 진단 가림 | MINOR | 유닛 다수, 실 세션 일부 |
| 11 | 사용량 원장 | usage_ledger | 스키마 v1~v3/NaN·시간대 대응 견고, 고정 .tmp 경쟁(자기복구), syspath 복구 ✓ | MINOR | 유닛 432줄/다수 |
| 12 | 자동 업데이트 | launcherStartup/routeVersion/installLauncher | fast-forward 전용+마커 배타+마커 절대 자동삭제 안 함 ✓, PS7 [UNVERIFIED], 체크아웃 이동 후 포워더 재설치·삭제 불가, -Remove PATH 잔여 | MINOR | 유닛, 실 업데이트 무검증 |
| 13 | 구성 관리 | routeConfig | 원자 쓰기+교차프로세스 락+DPAPI ✓, BOM/섹션 오류 잠복, 쓰기 모델=저장소 루트 고정(제한 디렉터리 아웃) | MINOR(잠복) | 유닛 다수 |
| 14 | blackbox (로컬 진단 기록, opt-in) | blackbox | HMAC+SHA-256+심링크 차단+오버플로 정책 ✓, transcript 경로=루트 상대 제약, 120s 일회성 토큰 | MINOR | 유닛 |
| 15 | 보안 경계(/api/test·검색·상태변경) | dashboard+proxy | 게이트 체인(토큰/Host/Origin/CAP/6-min) 엄격 ✓, 단일 보호선, README 보장 거짓(546) | MINOR | 유닛 강함 |
| 16 | 실패 진단 | routeCli/cliBackend | 스테이지 진단 설계 좋음 ↔ `connection_test` 오도(잠복 MAJOR), 클래스화 일부 견고 | MINOR | 유닛 |
| 17 | 시작 스크립트/설치 | ps1/cmd/installLauncher | 인용·PS5.1 대응·exit 코드 정확 ✓, 드라이브 루트 변형·포트 범위 미검사 | MINOR | 유닛 |
| 18 | 성능/메모리 | proxy+bench | 백프레셔/바이트 캡/512MB ✓, 상한 근거는 실측이나 회귀 모니터 부재 | INFO | bench/시뮬 |

**요약**: 전체 18개 기능 중 **"실기 한 번도 안 된 기능"이 8개**(1·3·4·5·7·8·12·17 일부) — 평균 평가가 "유닛은 강하지만 실 계약은 미검증"인 것이 이 제품의 일관된 비판점이다. 유일한 MAJOR는 6번(admission)이며, 잠복 MAJOR는 3번(차기 codex)이다.

## D. 우선순위 (비판 관점의 조치 목록)
1. **A2 단일 소스화** — admission 규칙(1-64/0-256/0-600000, isascii+숫자)을 한 곳에 두고 런처·프록시·문서가 참조; 런처 경유 E2E 테스트 1건(M1 종결).
2. **A3 수정 또는 문서화** — 메트릭·핸드오프를 인스턴스 키 파일로 분리(instances/ 체계 확장).
3. **A5 실기 게이트** — P1(실기 thinking probe)·실 codex 0.168.0 확인을 다음 릴리스 조건으로; `connection_test`에 버전 검사 결합.
4. **A4 규율** — 기능 릴리스로 분리(버전 상향 + README 수치 갱신 + HISTORY 동기화 절차).
5. **A6** — 일반 로컬의 침묵 윈도우를 튜닝 가능하게 하거나 기본값 재측정.
6. **A1** — "세션 단위 수용"을 문서화하거나 전역 조정 설계(다중 세션 부하 재시뮬레이션).

### D-마감 (2026-11-15 조치 라운드 진행분)

- **A2(규칙 이원화/M1)** — **해결된 부분**: 런처 `_local_admission_env`가 프록시와 동일한 상하한을 미러링(1-64/0-256/0-600000, `isascii`), 프록시 `localAdmissionEnv`가 직접 실행 시 throw, README 문구 정정, `tests/test_local_admission_env.py` 신규 — "잘못된 값" 계열의 오도는 종결. **남음**: 두 구현이 여전히 별도 상수(`_LOCAL_ADMISSION_LIMITS` vs `localAdmissionEnv` 호출)이므로 서로 다른 파일(파이썬↔JS)의 미러링은 유지 중 — "한쪽만 고치는" 회귀 위험 자체는 남아 있음(규칙 테이블 고정 테스트로 양쪽을 각각 고정).
- **A5(잠복 MAJOR 2건)** — `connection_test` 버전 검사 → **수정됨**(`unsupported_version`). cli 세션 continue → **수정됨**(`local_cli_backend_unavailable` 중단). 실기(PS7, codex, P1)는 여전히 미실행.
- **A3(메트릭 last-writer-wins)** — 미수용(설계 재검토 과제로 유지). **A4(버전 규율)** — 이번 라운드는 커밋 없이 진행되어 적용 보류. **A6(30초 고정)** — 미수용(기본값 유지). **A1(세션 단위 수용)** — 미수용(다중 세션 재시뮬레이션 과제로 유지). **B-행들** — env 삭제 누락 2건(BOM/섹션 도달불가) → 수정됨(SECTION D 위 1·3열 참고, BOM·`doc.first_error`·정리 목록 5종 추가).

수정 후 전체 스위트: **Python 874 tests OK (skipped=10)**, **Node 811 pass (0 fail)**.
